"""Combine per-scope summaries (one kind, one fiscal year each) into the site's data files.

Inputs are found recursively: every `summary.json` produced by `pipeline.gold` (with its
`reconcile.json` and, for contracts with deleted-key diagnostics, `changes.json` in the same
folder). Older single-kind files named `fyYYYY.json` are read too when they carry a full matrix.

Output, per fiscal year, `fyYYYY.json`:
- `kinds`: for contracts and assistance, either the loaded summary's provenance, checks and
  reconciliation, or `{"loaded": false}` with the reason. Nothing missing is shown as zero.
- `agencies`, `categories` and `cells`: the full agency x place x category matrix of every loaded
  kind, the only source for any figure that a category or agency selection changes.
- `states`: names, population and the per-kind lists that cannot be re-cut by selection
  (recipients), labelled with the scope they describe.
and `index.json` with the coverage of every year and kind.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from pipeline.gold import STATES
from pipeline.kinds import load_categories

KINDS = ("contracts", "assistance")
NOT_LOADED = "Not loaded yet for this year"


def find_summaries(inputs: Path) -> list[tuple[dict, Path]]:
    found = []
    for path in sorted(inputs.rglob("*.json")):
        if path.name not in ("summary.json",) and not (path.name.startswith("fy") and path.name[2:6].isdigit()):
            continue
        data = json.loads(path.read_text())
        if "matrix" not in data or "fiscal_year" not in data:
            continue  # not a summary, or an old summary without the full matrix
        found.append((data, path.parent))
    return found


def build(inputs: str | Path, output: str | Path, population: dict | None = None) -> dict:
    inputs, output = Path(inputs), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    categories = load_categories()
    by_year: dict[int, dict[str, tuple[dict, Path]]] = {}
    for summary, folder in find_summaries(inputs):
        kind = summary.get("kind", "contracts")
        key = (summary["fiscal_year"], kind)
        if kind in by_year.get(summary["fiscal_year"], {}):
            raise SystemExit(f"Two summaries for FY{key[0]} {kind}; keep exactly one per scope")
        by_year.setdefault(summary["fiscal_year"], {})[kind] = (summary, folder)
    if not by_year:
        raise SystemExit("No summaries with a full agency matrix found")

    index_years = []
    for year in sorted(by_year):
        scopes = by_year[year]
        agencies, agency_index, cells, kinds, states = [], {}, [], {}, {}
        category_ids = list(categories["categories"])
        for code, name in STATES.items():
            states[code] = {"name": name, "population": (population or {}).get(code), "by_kind": {},
                            "reconciliation": {}, "changes": None}
        for kind in KINDS:
            if kind not in scopes:
                kinds[kind] = {"loaded": False, "reason": NOT_LOADED}
                continue
            summary, folder = scopes[kind]
            rec_path, changes_path = folder / "reconcile.json", folder / "changes.json"
            rec = json.loads(rec_path.read_text()) if rec_path.exists() else None
            changes = json.loads(changes_path.read_text()) if changes_path.exists() else None
            if summary.get("category_map_version") != categories["version"]:
                raise SystemExit(f"FY{year} {kind} was built with category map "
                                 f"{summary.get('category_map_version')}, site uses {categories['version']}")
            columns = summary["matrix"]["columns"]
            for row in summary["matrix"]["rows"]:
                r = dict(zip(columns, row))
                if r["agency_id"] not in agency_index:
                    agency_index[r["agency_id"]] = len(agencies)
                    agencies.append({"id": r["agency_id"], "name": r["agency"]})
                cells.append([agency_index[r["agency_id"]], r["place_kind"], r["place"],
                              category_ids.index(r["category"]), r["amount_cents"], r["transactions"],
                              r["rows_without_amount"], r["face_value_cents"]])
            if rec:
                for name in rec.get("categories", []):
                    part = rec[name]
                    for row in part.get("rows", []):
                        states[row["state"]]["reconciliation"][name] = row
                for code, value in (rec.get("population") or {}).items():
                    if code in states and not states[code]["population"]:
                        states[code]["population"] = value
            for code, s in summary["states"].items():
                states[code]["by_kind"][kind] = {
                    "transactions": s["transactions"], "awards": s["awards"],
                    "recipients": s["recipients"], "recipients_not_ranked": s.get("recipients_not_ranked"),
                    "deobligations_cents": s["deobligations_cents"]}
            year_changes = (changes or {}).get("years", {}).get(str(year), {})
            if changes:
                for code, c in year_changes.items():
                    if code in states:
                        states[code]["changes"] = c
            kinds[kind] = {
                "loaded": True, "snapshot_date": summary["snapshot_date"], "source_zip": summary["source_zip"],
                "generated_at": summary["generated_at"], "measure": summary["measure"], "scope": summary["scope"],
                "national": {k: v for k, v in summary["national"].items() if k != "agencies"},
                "checks": summary["checks"],
                "unrecognised_state_fips": summary.get("unrecognised_state_fips", []),
                "deleted_key_matches": summary.get("deleted_key_matches"),
                "deleted_key_batches": summary.get("deleted_key_batches") if kind == "contracts" else None,
                "full_snapshot": summary.get("full_snapshot"),
                "reconciliation": None if rec is None else {
                    "official_queried_at": rec["official_queried_at"], "definition": rec["definition"],
                    "categories": {n: {k: v for k, v in rec[n].items() if k != "rows"} for n in rec["categories"]},
                    "official_outside_states": rec.get("official_outside_states"),
                    "agency_status": rec.get("agency_status", "compared"),
                    "agency_scope_note": rec.get("agency_scope_note"),
                    "agencies": [a for a in rec.get("agencies", []) if a["official_cents"] or a["ours_cents"]]},
                "changes": None if changes is None else {
                    "method": changes.get("method"), "meaning": changes.get("meaning"),
                    "unknown_year": changes.get("unknown_year_summary"),
                    "unknown_year_rows": changes.get("unknown_year_rows", []),
                    "source_file": changes.get("source_file"), "snapshot_date": changes.get("snapshot_date"),
                    "outside_states": year_changes.get("OTHER")},
            }
        year_file = {"fiscal_year": year, "categories": categories["categories"], "category_ids": category_ids,
                     "category_map_version": categories["version"], "kinds": kinds, "agencies": agencies,
                     "cell_columns": ["agency", "place_kind", "place", "category", "amount_cents", "transactions",
                                      "rows_without_amount", "face_value_cents"],
                     "cells": cells, "states": states}
        (output / f"fy{year}.json").write_text(json.dumps(year_file, separators=(",", ":")))
        index_years.append({"fiscal_year": year, "kinds": {
            k: ({"loaded": True, "snapshot_date": v["snapshot_date"], "amount_cents": v["national"]["amount_cents"],
                 "reconciliation": None if not v["reconciliation"] else
                 {c: p["status"] for c, p in v["reconciliation"]["categories"].items()}}
                if v["loaded"] else v) for k, v in kinds.items()}})
    index = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "category_map_version": categories["version"], "years": index_years,
             "not_offered": "FY2008-2020 are a later stage and are not loaded."}
    (output / "index.json").write_text(json.dumps(index, indent=1))
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs")
    parser.add_argument("output")
    args = parser.parse_args()
    print(json.dumps(build(args.inputs, args.output), indent=1))


if __name__ == "__main__":
    main()
