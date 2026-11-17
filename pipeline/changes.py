"""Describe what changed between monthly releases, without guessing at business meaning.

Two independent methods:

* `delta_summary` reads Treasury's monthly delta file. Its `correction_delete_ind` marks each row:
  blank = new transaction, C = correction of an existing transaction, D = deletion.
  A correction row carries the corrected values only, so it is counted, not turned into a
  dollar change. A negative obligation is a de-obligation, never called a cancellation.
* `diff_snapshots` compares two typed snapshots by transaction key: added, removed, amount changed.
  An identical pair of snapshots produces no changes.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path

import duckdb

from pipeline.gold import STATES, cents, sql_list


def _view(con, name: str, directory: str | Path) -> None:
    con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{Path(directory) / 'part-*.parquet'}')")


def delta_summary(delta_dir: str | Path, years: list[int]) -> dict:
    con = duckdb.connect()
    _view(con, "delta", delta_dir)
    year_list = sql_list(str(y) for y in years)
    rows = con.execute(f"""
        SELECT action_date_fiscal_year AS fy,
               CASE WHEN upper(trim(primary_place_of_performance_state_code)) IN ({sql_list(STATES)})
                    THEN upper(trim(primary_place_of_performance_state_code)) ELSE 'OTHER' END AS place,
               CASE upper(trim(correction_delete_ind)) WHEN 'D' THEN 'deleted' WHEN 'C' THEN 'corrected'
                    ELSE 'new' END AS kind,
               count(*) AS n,
               sum(coalesce(federal_action_obligation, 0)) AS amount,
               count(*) FILTER (WHERE federal_action_obligation < 0) AS deobligation_rows,
               sum(federal_action_obligation) FILTER (WHERE federal_action_obligation < 0) AS deobligated
        FROM delta
        WHERE action_date_fiscal_year IN ({year_list}) OR upper(trim(correction_delete_ind)) = 'D'
        GROUP BY 1, 2, 3""").fetchall()
    undated_deletions = con.execute(f"""
        SELECT count(*) FROM delta
        WHERE upper(trim(correction_delete_ind)) = 'D' AND action_date_fiscal_year NOT IN ({year_list})""").fetchone()[0]
    out: dict = {"method": "monthly delta file", "years": {}, "deletions_without_listed_year": undated_deletions}
    for fy, place, kind, n, amount, deob_rows, deob in rows:
        if fy not in [str(y) for y in years]:
            continue
        state = out["years"].setdefault(fy, {}).setdefault(place, {
            "new_rows": 0, "new_cents": 0, "corrected_rows": 0, "deleted_rows": 0,
            "deobligation_rows": 0, "deobligation_cents": 0})
        if kind == "new":
            state["new_rows"] += n
            state["new_cents"] += cents(amount)
        elif kind == "corrected":
            state["corrected_rows"] += n
        else:
            state["deleted_rows"] += n
        if kind != "deleted":
            state["deobligation_rows"] += deob_rows
            state["deobligation_cents"] += cents(deob)
    return out


def diff_snapshots(old_dir: str | Path, new_dir: str | Path) -> dict:
    con = duckdb.connect()
    _view(con, "old", old_dir)
    _view(con, "new", new_dir)
    rows = con.execute(f"""
        WITH joined AS (
          SELECT o.contract_transaction_unique_key AS old_key, n.contract_transaction_unique_key AS new_key,
                 o.federal_action_obligation AS old_amount, n.federal_action_obligation AS new_amount,
                 coalesce(upper(trim(n.primary_place_of_performance_state_code)),
                          upper(trim(o.primary_place_of_performance_state_code))) AS state
          FROM old o FULL OUTER JOIN new n
            ON o.contract_transaction_unique_key = n.contract_transaction_unique_key)
        SELECT CASE WHEN state IN ({sql_list(STATES)}) THEN state ELSE 'OTHER' END AS place,
               CASE WHEN old_key IS NULL THEN 'added'
                    WHEN new_key IS NULL THEN 'removed'
                    WHEN old_amount IS DISTINCT FROM new_amount THEN 'amount_changed'
                    ELSE 'unchanged' END AS kind,
               count(*),
               sum(coalesce(new_amount, 0) - coalesce(old_amount, 0))
        FROM joined GROUP BY 1, 2""").fetchall()
    places: dict = {}
    totals = {"added": 0, "removed": 0, "amount_changed": 0, "unchanged": 0, "net_change_cents": 0}
    for place, kind, n, change in rows:
        entry = places.setdefault(place, {"added": 0, "removed": 0, "amount_changed": 0, "unchanged": 0,
                                          "net_change_cents": 0})
        entry[kind] += n
        totals[kind] += n
        delta = cents(change if change is not None else Decimal(0))
        entry["net_change_cents"] += delta
        totals["net_change_cents"] += delta
    return {"method": "snapshot comparison by transaction key", "totals": totals, "places": places}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("delta")
    d.add_argument("delta_dir")
    d.add_argument("output")
    d.add_argument("--years", type=int, nargs="+", required=True)
    s = sub.add_parser("diff")
    s.add_argument("old_dir")
    s.add_argument("new_dir")
    s.add_argument("output")
    args = parser.parse_args()
    result = delta_summary(args.delta_dir, args.years) if args.command == "delta" else \
        diff_snapshots(args.old_dir, args.new_dir)
    Path(args.output).write_text(json.dumps(result, indent=1))
    print(json.dumps(result.get("totals") or {"years": list(result["years"])}, indent=2))


if __name__ == "__main__":
    main()
