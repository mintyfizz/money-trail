"""Combine per-year summaries, reconciliation results and change records into the site's data files."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


def build(inputs: str | Path, output: str | Path) -> dict:
    inputs, output = Path(inputs), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    changes_path = next(inputs.rglob("changes.json"), None)
    delta = json.loads(changes_path.read_text()) if changes_path else None
    years = []
    for path in sorted(inputs.rglob("fy*.json")):
        summary = json.loads(path.read_text())
        year = summary["fiscal_year"]
        rec_path = next(inputs.rglob(f"reconcile-fy{year}.json"), None)
        rec = json.loads(rec_path.read_text()) if rec_path else None
        population = (rec or {}).get("population", {})
        contract_rows = {r["state"]: r for r in (rec or {}).get("contracts", {}).get("rows", [])}
        idv_rows = {r["state"]: r for r in (rec or {}).get("idvs", {}).get("rows", [])}
        year_changes = (delta or {}).get("years", {}).get(str(year), {})
        for code, state in summary["states"].items():
            state["population"] = population.get(code)
            state["reconciliation"] = {"contracts": contract_rows.get(code), "idvs": idv_rows.get(code)}
            state["changes"] = year_changes.get(code)
        summary["reconciliation"] = None if rec is None else {
            "official_queried_at": rec["official_queried_at"], "definition": rec["definition"],
            **{part: {k: v for k, v in rec[part].items() if k != "rows"} for part in ("contracts", "idvs")},
            "agencies": [a for a in rec.get("agencies", []) if a["official_cents"] or a["ours_cents"]][:12]}
        summary["changes"] = None if delta is None else {
            "method": delta["method"], "meaning": delta.get("meaning"), "all_years": delta.get("all_years"),
            "unknown_year": delta.get("unknown_year_summary"),
            "unknown_year_rows": delta.get("unknown_year_rows", []),
            "source_file": delta.get("source_file"),
            "snapshot_date": delta.get("snapshot_date"), "outside_states": year_changes.get("OTHER")}
        (output / f"fy{year}.json").write_text(json.dumps(summary, separators=(",", ":")))
        years.append({"fiscal_year": year, "snapshot_date": summary["snapshot_date"],
                      "net_obligations_cents": summary["national"]["net_obligations_cents"],
                      "reconciliation_status": (rec or {}).get("contracts", {}).get("status", "not run")})
    if not years:
        raise SystemExit("No year summaries found")
    index = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "years": sorted(years, key=lambda y: y["fiscal_year"])}
    (output / "index.json").write_text(json.dumps(index, indent=1))
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs")
    parser.add_argument("output")
    args = parser.parse_args()
    print(json.dumps(build(args.inputs, args.output), indent=1))


if __name__ == "__main__":
    main()
