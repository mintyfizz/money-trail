"""Compare this project's per-state totals with USAspending's own published totals.

Both sides use the same definition: transactions with an action date in the fiscal year, place of
performance state, and the same award type codes. Contracts (A-D) and indefinite-delivery
vehicles are compared separately. The official side is queried live, so its snapshot is newer than
the archive file; gaps are recorded, never hidden.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.request

from pipeline.gold import STATES

API = "https://api.usaspending.gov/api/v2/search/spending_by_geography/"
CONTRACT_CODES = ["A", "B", "C", "D"]
IDV_CODES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]


def fiscal_window(year: int) -> dict:
    # Inclusive bounds on the transaction action date, set explicitly rather than relying on defaults.
    return {"start_date": f"{year - 1}-10-01", "end_date": f"{year}-09-30", "date_type": "action_date"}


def post(url: str, body: dict, attempts: int = 4) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                                      "User-Agent": "money-trail/1.0"})
            with urllib.request.urlopen(request, timeout=120) as response:
                # parse_float=Decimal keeps the API's dollar amounts exact; a float would already have
                # lost precision before any conversion to cents.
                return json.loads(response.read(), parse_float=Decimal)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(5 * 3 ** attempt)
    raise RuntimeError("unreachable")


def official_by_state(year: int, codes: list[str]) -> dict:
    body = {"scope": "place_of_performance", "geo_layer": "state", "subawards": False,
            "spending_level": "transactions",
            "filters": {"time_period": [fiscal_window(year)], "award_type_codes": codes}}
    results = post(API, body)["results"]
    return {r["shape_code"]: r for r in results if r.get("shape_code")}


def ours_by_state(summary: dict, codes: set[str]) -> dict:
    """Integer cents per state for the given award type groups. A state in the summary with no rows
    of these types is a known zero; a state absent from the summary is missing."""
    return {code: sum(t["amount_cents"] for t in state["types"] if t["code"] in codes)
            for code, state in summary["states"].items()}


# Pass rule, fixed before results are seen: every one of the 51 places present on both sides,
# national gap within 0.1 percent, and no single place beyond 1 percent.
TOTAL_TOLERANCE_PCT = Decimal("0.1")
STATE_TOLERANCE_PCT = Decimal("1")


def official_cents(record: dict) -> int | None:
    value = record.get("aggregated_amount")
    if value is None:
        return None
    return int((Decimal(str(value)) * 100).quantize(Decimal("1")))


def compare(ours: dict, official: dict) -> dict:
    """Compare every one of the 50 states and DC. A place missing on either side is reported, not skipped."""
    rows, missing_ours, missing_official = [], [], []
    for code in sorted(STATES):
        mine = ours.get(code)
        theirs = official_cents(official[code]) if code in official else None
        if mine is None:
            missing_ours.append(code)
        if theirs is None:
            missing_official.append(code)
        gap = None if mine is None or theirs is None else mine - theirs
        rows.append({"state": code, "ours_cents": mine, "official_cents": theirs, "gap_cents": gap,
                     "gap_pct": float(round(Decimal(100 * gap) / theirs, 4)) if gap is not None and theirs else None})
    complete = [r for r in rows if r["gap_cents"] is not None]
    total_ours = sum(r["ours_cents"] for r in complete)
    total_official = sum(r["official_cents"] for r in complete)
    total_pct = Decimal(100 * (total_ours - total_official)) / total_official if total_official else None
    pcts = [abs(Decimal(r["gap_cents"] * 100) / r["official_cents"]) for r in complete if r["official_cents"]]
    # A place where the official figure is zero but ours is not cannot be expressed as a percentage;
    # it fails the per-place rule.
    unmatched_zero = [r["state"] for r in complete if not r["official_cents"] and r["ours_cents"]]
    total_ok = (total_ours == total_official) if not total_official else abs(total_pct) <= TOTAL_TOLERANCE_PCT
    if missing_ours or missing_official:
        status = "incomplete"
    elif total_ok and not unmatched_zero and all(p <= STATE_TOLERANCE_PCT for p in pcts):
        status = "passed"
    else:
        status = "gaps"
    return {
        "status": status,
        "rule": f"all 51 present; national gap <= {TOTAL_TOLERANCE_PCT}%; every state <= {STATE_TOLERANCE_PCT}%",
        "missing_ours": missing_ours, "missing_official": missing_official,
        "total_ours_cents": total_ours, "total_official_cents": total_official,
        "total_gap_cents": total_ours - total_official,
        "total_gap_pct": float(round(total_pct, 4)) if total_pct is not None else None,
        "states_within_0_1_pct": sum(p <= Decimal("0.1") for p in pcts),
        "states_within_1_pct": sum(p <= 1 for p in pcts),
        "max_abs_gap_pct": float(round(max(pcts), 4)) if pcts else None,
        "largest_gaps": sorted(complete, key=lambda r: abs(r["gap_cents"]), reverse=True)[:5],
        "rows": rows,
    }


CATEGORY = "https://api.usaspending.gov/api/v2/search/spending_by_category/awarding_agency/"


def official_by_agency(year: int) -> dict:
    """Published contract totals (A-D) by awarding agency, place of performance in the 50 states and
    DC: the same 51 places the state comparison uses."""
    places = [{"country": "USA", "state": code} for code in sorted(STATES)]
    body = {"spending_level": "transactions", "limit": 100,
            "filters": {"time_period": [fiscal_window(year)], "award_type_codes": CONTRACT_CODES,
                        "place_of_performance_locations": places}}
    results = post(CATEGORY, body)["results"]
    return {r["name"]: int((Decimal(str(r["amount"])) * 100).quantize(Decimal("1"))) for r in results}


def compare_agencies(ours: dict, official: dict) -> list[dict]:
    """Diagnostic only: which agencies carry the state-level gap, with identical 51-place filters."""
    rows = []
    for name in sorted(set(ours) | set(official), key=lambda n: -(official.get(n) or ours.get(n) or 0)):
        mine, theirs = ours.get(name), official.get(name)
        gap = None if mine is None or theirs is None else mine - theirs
        rows.append({"agency": name, "ours_cents": mine, "official_cents": theirs, "gap_cents": gap,
                     "gap_pct": float(round(Decimal(100 * gap) / theirs, 4)) if gap is not None and theirs else None})
    return rows


def reconcile(summary: dict) -> dict:
    year = summary["fiscal_year"]
    contracts = official_by_state(year, CONTRACT_CODES)
    idvs = official_by_state(year, IDV_CODES)
    population = {code: r.get("population") for code, r in contracts.items() if r.get("population")}
    return {
        "fiscal_year": year,
        "archive_snapshot": summary["snapshot_date"],
        "official_queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "definition": {"time_period": fiscal_window(year), "scope": "place_of_performance", "geo_layer": "state",
                       "spending_level": "transactions", "measure": "sum of federal_action_obligation",
                       "note": "State-layer totals are domestic only; they are not the national all-location total."},
        "contracts": compare(ours_by_state(summary, set(CONTRACT_CODES)), contracts),
        "idvs": compare(ours_by_state(summary, {"IDV"}), idvs),
        "population": population,
        "agencies": compare_agencies(summary.get("agency_contracts_in_states", {}), official_by_agency(year)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary")
    parser.add_argument("output")
    args = parser.parse_args()
    result = reconcile(json.loads(Path(args.summary).read_text()))
    Path(args.output).write_text(json.dumps(result, indent=1))
    for part in ("contracts", "idvs"):
        r = result[part]
        print(part, {k: r[k] for k in ("status", "total_ours_cents", "total_official_cents", "total_gap_pct", "states_within_1_pct",
                                         "max_abs_gap_pct")})


if __name__ == "__main__":
    main()
