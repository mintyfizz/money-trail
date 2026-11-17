"""Describe what changed between monthly releases, without guessing at business meaning.

Two independent methods:

* `delta_summary` reads Treasury's monthly delta file. Its indicator marks C (correction) and D
  (deletion); a blank flag can be a new row or an update, so the two are reported together.
  Rows carry current values only, so no dollar change is inferred. A negative obligation is a
  de-obligation, never called a cancellation.
* `diff_snapshots` compares two typed snapshots by transaction key: added, removed, amount changed.
  An identical pair of snapshots produces no changes.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path

from pipeline.gold import STATES, cents, sql_list
from pipeline.guard import duckdb_connect


def _view(con, name: str, directory: str | Path) -> None:
    con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{Path(directory) / 'part-*.parquet'}')")


class DeltaQualityError(RuntimeError):
    """An added or updated delta row lacks a valid amount, date or fiscal year."""


def delta_summary(delta_dir: str | Path, years: list[int], reserve_gib: float = 8) -> dict:
    """Summarise one monthly delta file, claiming only what its rows support.

    A blank indicator does not prove a row is new: Treasury's generator also emits updated rows with
    a blank flag. So blank and C rows are reported together as "added or updated", with the C count
    shown separately. D rows are tombstones whose amount, date and place are blank, so they are
    counted on their own and never assigned to a state or year.
    """
    con = duckdb_connect(Path(delta_dir).parent / "_duckdb_tmp", reserve_gib)
    _view(con, "delta", delta_dir)
    year_list = sql_list(str(y) for y in years)
    # Only deletion tombstones may omit amount, date and fiscal year. Rows in the requested years
    # must be complete. Added or updated rows with no date at all cannot belong to any year; they
    # are listed by key, never dropped silently or counted in a year.
    invalid = con.execute(f"""
        SELECT count(*) FILTER (WHERE federal_action_obligation IS NULL),
               count(*) FILTER (WHERE action_date IS NULL)
        FROM delta WHERE correction_delete_ind <> 'D' AND action_date_fiscal_year IN ({year_list})""").fetchone()
    if any(invalid):
        raise DeltaQualityError(f"Added or updated delta rows in scope missing amount/date: {invalid}")
    undated = con.execute("""
        SELECT contract_transaction_unique_key, federal_action_obligation_raw, action_date_raw,
               action_date_fiscal_year, source_file, source_row
        FROM delta WHERE correction_delete_ind <> 'D'
          AND (action_date IS NULL OR trim(action_date_fiscal_year) = '' OR federal_action_obligation IS NULL)
        ORDER BY source_file, source_row""").fetchall()
    rows = con.execute(f"""
        SELECT action_date_fiscal_year AS fy,
               CASE WHEN upper(trim(primary_place_of_performance_state_code)) IN ({sql_list(STATES)})
                    THEN upper(trim(primary_place_of_performance_state_code)) ELSE 'OTHER' END AS place,
               count(*) AS n,
               count(*) FILTER (WHERE correction_delete_ind = 'C') AS flagged,
               sum(federal_action_obligation) AS amount,
               count(*) FILTER (WHERE federal_action_obligation < 0) AS deobligation_rows,
               sum(federal_action_obligation) FILTER (WHERE federal_action_obligation < 0) AS deobligated
        FROM delta
        WHERE correction_delete_ind <> 'D' AND action_date_fiscal_year IN ({year_list})
        GROUP BY 1, 2""").fetchall()
    totals = con.execute("""
        SELECT count(*) FILTER (WHERE correction_delete_ind <> 'D'),
               count(*) FILTER (WHERE correction_delete_ind = 'C'),
               count(*) FILTER (WHERE correction_delete_ind = 'D')
        FROM delta""").fetchone()
    out: dict = {
        "method": "monthly delta file",
        "meaning": ("Rows Treasury changed in its latest monthly release. 'Added or updated' cannot be split "
                    "into new and changed rows without the previous month's full snapshot."),
        "all_years": {"added_or_updated_rows": totals[0], "flagged_correction_rows": totals[1],
                      "deleted_rows": totals[2]},
        "incomplete_rows_outside_any_year": [
            {"key": k, "amount_raw": a, "action_date_raw": d, "fiscal_year_raw": fy, "source_file": f, "source_row": r}
            for k, a, d, fy, f, r in undated],
        "years": {},
    }
    for fy, place, n, flagged, amount, deob_rows, deob in rows:
        out["years"].setdefault(fy, {})[place] = {
            "added_or_updated_rows": n, "flagged_correction_rows": flagged,
            "obligations_on_these_rows_cents": cents(amount),
            "deobligation_rows": deob_rows, "deobligation_cents": cents(deob)}
    return out


def diff_snapshots(old_dir: str | Path, new_dir: str | Path, reserve_gib: float = 8) -> dict:
    """Compare two snapshots by transaction key, place by place, including rows that moved state.

    Scope: amount and place of performance only. Changes to agency, recipient or other fields are
    not detected, and the two snapshots must cover the same scope (same fiscal year and award type).

    A key whose amount or place changed is "changed". Each place's net change is the new amounts
    now placed there minus the old amounts that were placed there, so a $100 row moved from CA to TX
    shows -$100 for CA and +$100 for TX. Keys must be unique in each snapshot.
    """
    con = duckdb_connect(Path(new_dir).parent / "_duckdb_tmp", reserve_gib)
    _view(con, "old_raw", old_dir)
    _view(con, "new_raw", new_dir)
    for name in ("old_raw", "new_raw"):
        dupes = con.execute(f"SELECT count(*) - count(DISTINCT contract_transaction_unique_key) FROM {name}").fetchone()[0]
        if dupes:
            raise ValueError(f"{name} has {dupes} duplicate transaction keys; snapshots cannot be compared")
        nulls = con.execute(f"SELECT count(*) FROM {name} WHERE federal_action_obligation IS NULL").fetchone()[0]
        if nulls:
            raise ValueError(f"{name} has {nulls} rows without an amount; snapshots cannot be compared")
    place = f"""CASE WHEN upper(trim(primary_place_of_performance_state_code)) IN ({sql_list(STATES)})
                     THEN upper(trim(primary_place_of_performance_state_code)) ELSE 'OTHER' END"""
    for side in ("old", "new"):
        con.execute(f"""CREATE VIEW {side} AS SELECT contract_transaction_unique_key AS k, {place} AS place,
                        federal_action_obligation AS amount FROM {side}_raw""")
    con.execute("""
        CREATE TABLE joined AS
        SELECT coalesce(o.k, n.k) AS k, o.place AS old_place, n.place AS new_place,
               o.amount AS old_amount, n.amount AS new_amount,
               CASE WHEN o.k IS NULL THEN 'added' WHEN n.k IS NULL THEN 'removed'
                    WHEN o.amount IS DISTINCT FROM n.amount OR o.place IS DISTINCT FROM n.place THEN 'changed'
                    ELSE 'unchanged' END AS kind
        FROM old o FULL OUTER JOIN new n ON o.k = n.k""")
    totals = dict(con.execute("SELECT kind, count(*) FROM joined GROUP BY 1").fetchall())
    moved = con.execute("SELECT count(*) FROM joined WHERE kind = 'changed' AND old_place IS DISTINCT FROM new_place").fetchone()[0]
    rows = con.execute("""
        WITH effects AS (
          SELECT k, new_place AS place, kind, coalesce(new_amount, 0) AS delta FROM joined
            WHERE kind IN ('added', 'changed')
          UNION ALL
          SELECT k, old_place AS place, kind, -coalesce(old_amount, 0) AS delta FROM joined
            WHERE kind IN ('removed', 'changed'))
        SELECT place, kind, count(DISTINCT k), sum(delta) FROM effects GROUP BY 1, 2""").fetchall()
    places: dict = {}
    for place_code, kind, n, delta in rows:
        entry = places.setdefault(place_code, {"added": 0, "removed": 0, "changed": 0, "net_change_cents": 0})
        entry[kind] += n
        entry["net_change_cents"] += cents(delta)
    summary = {k: totals.get(k, 0) for k in ("added", "removed", "changed", "unchanged")}
    summary["moved_place"] = moved
    summary["net_change_cents"] = sum(p["net_change_cents"] for p in places.values())
    return {"method": "snapshot comparison by transaction key",
            "scope": "amount and place of performance only; agency and recipient changes are not detected",
            "totals": summary, "places": places}


def deleted_keys(delta_dir: str | Path, output: str | Path) -> int:
    """Write the transaction keys a delta file marks as deleted (its tombstone rows)."""
    con = duckdb_connect(Path(delta_dir).parent / "_duckdb_tmp")
    _view(con, "delta", delta_dir)
    con.execute(f"""COPY (SELECT DISTINCT contract_transaction_unique_key FROM delta
                         WHERE correction_delete_ind = 'D' ORDER BY 1)
                   TO '{output}' (FORMAT parquet, COMPRESSION zstd)""")
    return con.execute(f"SELECT count(*) FROM read_parquet('{output}')").fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("delta")
    d.add_argument("delta_dir")
    d.add_argument("output")
    d.add_argument("--years", type=int, nargs="+", required=True)
    k = sub.add_parser("deleted-keys")
    k.add_argument("delta_dir")
    k.add_argument("output")
    s = sub.add_parser("diff")
    s.add_argument("old_dir")
    s.add_argument("new_dir")
    s.add_argument("output")
    args = parser.parse_args()
    if args.command == "deleted-keys":
        print(json.dumps({"deleted_keys": deleted_keys(args.delta_dir, args.output)}))
        return
    result = delta_summary(args.delta_dir, args.years) if args.command == "delta" else \
        diff_snapshots(args.old_dir, args.new_dir)
    Path(args.output).write_text(json.dumps(result, indent=1))
    print(json.dumps(result.get("totals") or {"years": list(result["years"])}, indent=2))


if __name__ == "__main__":
    main()
