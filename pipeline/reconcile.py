"""Compare this project's per-state totals with USAspending's own published totals.

Both sides use the same definition: transactions with an action date in the fiscal year, place of
performance state, and the same award type codes. Each category of the versioned map is compared on
its own, using the API codes listed for it in categories.json (contracts, IDVs, grants, direct
payments, loans, other assistance). The official side sums Treasury's generated pragmatic
obligation, which is our measure. It is queried live, so its snapshot is newer than the archive
file; gaps are recorded, never hidden. Places outside the 50 states and DC, and the API's
blank-place bucket, are kept apart and never enter the comparison.
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
from pipeline.kinds import load_categories

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


def official_by_state(year: int, codes: list[str], with_outside: bool = False):
    body = {"scope": "place_of_performance", "geo_layer": "state", "subawards": False,
            "spending_level": "transactions",
            "filters": {"time_period": [fiscal_window(year)], "award_type_codes": codes}}
    results = post(API, body)["results"]
    states = {r["shape_code"]: r for r in results if r.get("shape_code") in STATES}
    if not with_outside:
        return states
    # Territories and the blank-place bucket: reported beside the comparison, never inside it.
    outside = {(r.get("shape_code") or "(blank)"): official_cents(r) for r in results
               if r.get("shape_code") not in STATES}
    return states, outside, len(results)


def ours_by_state(summary: dict, category: str) -> dict:
    """Integer cents per state for one category. A state in the summary with no rows of this
    category is a known zero; a state absent from the summary is missing."""
    return {code: sum(c["amount_cents"] for c in state.get("categories", []) if c["category"] == category)
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


def official_by_agency(year: int, codes: list[str] | None = None, max_pages: int = 20) -> dict:
    if codes is not None and not codes:
        raise ValueError("No award type codes given; refusing to default to contracts")
    """Published totals by awarding agency for the given codes (default: contracts A-D), place of
    performance in the 50 states and DC: the same 51 places the state comparison uses. Every page
    is read, so no agency is cut off by the page size."""
    places = [{"country": "USA", "state": code} for code in sorted(STATES)]
    totals = {}
    for page in range(1, max_pages + 1):
        body = {"spending_level": "transactions", "limit": 100, "page": page,
                "filters": {"time_period": [fiscal_window(year)], "award_type_codes": CONTRACT_CODES if codes is None else codes,
                            "place_of_performance_locations": places}}
        response = post(CATEGORY, body)
        for r in response["results"]:
            totals[r["name"]] = totals.get(r["name"], 0) + int((Decimal(str(r["amount"])) * 100).quantize(Decimal("1")))
        if not (response.get("page_metadata") or {}).get("hasNext"):
            return totals
    raise RuntimeError(f"Agency totals still paging after {max_pages} pages; refusing a partial list")


def ours_by_agency(summary: dict, categories: list[str]) -> dict:
    """Integer cents per awarding agency name in the 50 states and DC, from the full matrix."""
    columns = summary["matrix"]["columns"]
    totals = {}
    for row in summary["matrix"]["rows"]:
        r = dict(zip(columns, row))
        if r["place_kind"] == "state" and r["category"] in categories:
            totals[r["agency"]] = totals.get(r["agency"], 0) + r["amount_cents"]
    return totals


def compare_agencies(ours: dict, official: dict) -> list[dict]:
    """Diagnostic only: which agencies carry the state-level gap, with identical 51-place filters."""
    rows = []
    for name in sorted(set(ours) | set(official), key=lambda n: -(official.get(n) or ours.get(n) or 0)):
        mine, theirs = ours.get(name), official.get(name)
        gap = None if mine is None or theirs is None else mine - theirs
        rows.append({"agency": name, "ours_cents": mine, "official_cents": theirs, "gap_cents": gap,
                     "gap_pct": float(round(Decimal(100 * gap) / theirs, 4)) if gap is not None and theirs else None})
    return rows


def reconcile(summary: dict, categories: dict | None = None) -> dict:
    year, kind = summary["fiscal_year"], summary.get("kind", "contracts")
    categories = categories or load_categories()
    names = [c for c, v in categories["categories"].items() if v["kind"] == kind]
    result = {
        "fiscal_year": year, "kind": kind,
        "archive_snapshot": summary["snapshot_date"],
        "category_map_version": categories["version"],
        "official_queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "definition": {"time_period": fiscal_window(year), "scope": "place_of_performance", "geo_layer": "state",
                       "spending_level": "transactions",
                       "measure": ("sum of federal_action_obligation" if kind == "contracts" else
                                   "generated pragmatic obligation: loan subsidy cost for loans, "
                                   "federal_action_obligation otherwise"),
                       "note": "State-layer totals are domestic only; they are not the national all-location total."},
        "categories": names, "population": {}, "official_outside_states": {},
    }
    unanswered = []
    for name in names:
        official, outside, returned = official_by_state(year, categories["api_codes"][name], with_outside=True)
        result["official_outside_states"][name] = outside
        if returned == 0:
            # Observed for this year and query only (FY2025 assistance type -1 returned nothing on
            # 18 Nov 2026). Not assumed for other years: every category is queried every time.
            unanswered.append(name)
            result[name] = {"status": "no official figure returned",
                            "reason": "The geography endpoint returned no rows for these codes and this year.",
                            "official_rows_returned": 0,
                            "total_ours_cents": sum(ours_by_state(summary, name).values())}
            continue
        result[name] = compare(ours_by_state(summary, name), official)
        result[name]["official_rows_returned"] = returned
        if not result["population"]:
            result["population"] = {code: r.get("population") for code, r in official.items() if r.get("population")}
    agency_categories = ["contracts"] if kind == "contracts" else [n for n in names if n not in unanswered]
    codes = [code for n in agency_categories for code in categories["api_codes"][n]]
    if not codes:
        # Never fall back to another kind's codes: with nothing comparable, say so.
        result.update(agency_categories=[], agencies=[], agency_status="unavailable",
                      agency_scope_note="No category returned an official state figure; agency comparison not run")
        return result
    ours = (ours_by_agency(summary, agency_categories) if "matrix" in summary
            else summary.get("agency_contracts_in_states", {}))
    result["agency_categories"] = agency_categories
    result["agency_status"] = "compared"
    result["agency_scope_note"] = ("Agency comparison covers these categories only"
                                   + ("; left out because no official state figure was returned: "
                                      + ", ".join(categories["categories"][n]["label"] for n in unanswered)
                                      if unanswered else ""))
    result["agencies"] = compare_agencies(ours, official_by_agency(year, codes))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary")
    parser.add_argument("output")
    args = parser.parse_args()
    result = reconcile(json.loads(Path(args.summary).read_text()))
    Path(args.output).write_text(json.dumps(result, indent=1))
    for part in result["categories"]:
        r = result[part]
        print(part, {k: r.get(k) for k in ("status", "total_ours_cents", "total_official_cents", "total_gap_pct",
                                            "states_within_1_pct", "max_abs_gap_pct")})


if __name__ == "__main__":
    main()
