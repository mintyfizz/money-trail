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

API = "https://api.usaspending.gov/api/v2/search/spending_by_geography/"
CONTRACT_CODES = ["A", "B", "C", "D"]
IDV_CODES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]


def fiscal_window(year: int) -> dict:
    return {"start_date": f"{year - 1}-10-01", "end_date": f"{year}-09-30"}


def post(url: str, body: dict, attempts: int = 4) -> dict:
    data = json.dumps(body).encode()
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                                      "User-Agent": "money-trail/1.0"})
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read())
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(5 * 3 ** attempt)
    raise RuntimeError("unreachable")


def official_by_state(year: int, codes: list[str]) -> dict:
    body = {"scope": "place_of_performance", "geo_layer": "state", "subawards": False,
            "filters": {"time_period": [fiscal_window(year)], "award_type_codes": codes}}
    results = post(API, body)["results"]
    return {r["shape_code"]: r for r in results if r.get("shape_code")}


def ours_by_state(summary: dict, codes: set[str]) -> dict:
    """Integer cents per state for the given award type groups."""
    return {code: sum(t["amount_cents"] for t in state["types"] if t["code"] in codes)
            for code, state in summary["states"].items()}


def official_cents(record: dict) -> int:
    # The API returns dollars as JSON numbers; quantize through Decimal to whole cents.
    return int((Decimal(str(record.get("aggregated_amount") or 0)) * 100).quantize(Decimal("1")))


def compare(ours: dict, official: dict) -> dict:
    rows = []
    for code in sorted(ours):
        mine = ours[code]
        theirs = official_cents(official.get(code, {}))
        gap = mine - theirs
        rows.append({"state": code, "ours_cents": mine, "official_cents": theirs, "gap_cents": gap,
                     "gap_pct": round(100 * gap / theirs, 3) if theirs else None})
    total_ours, total_official = sum(r["ours_cents"] for r in rows), sum(r["official_cents"] for r in rows)
    pcts = [abs(r["gap_pct"]) for r in rows if r["gap_pct"] is not None]
    return {
        "total_ours_cents": total_ours, "total_official_cents": total_official,
        "total_gap_cents": total_ours - total_official,
        "total_gap_pct": round(100 * (total_ours - total_official) / total_official, 4) if total_official else None,
        "states_within_0_1_pct": sum(p <= 0.1 for p in pcts),
        "states_within_1_pct": sum(p <= 1 for p in pcts),
        "max_abs_gap_pct": max(pcts) if pcts else None,
        "largest_gaps": sorted(rows, key=lambda r: abs(r["gap_cents"]), reverse=True)[:5],
        "rows": rows,
    }


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
                       "measure": "sum of federal_action_obligation"},
        "contracts": compare(ours_by_state(summary, set(CONTRACT_CODES)), contracts),
        "idvs": compare(ours_by_state(summary, {"IDV"}), idvs),
        "population": population,
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
        print(part, {k: r[k] for k in ("total_ours_cents", "total_official_cents", "total_gap_pct", "states_within_1_pct",
                                         "max_abs_gap_pct")})


if __name__ == "__main__":
    main()
