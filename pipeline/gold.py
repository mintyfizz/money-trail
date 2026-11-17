"""Aggregate typed contract transactions into the per-state summaries the dashboard reads.

Money is the sum of `federal_action_obligation` over transactions: net obligations, which can be
negative when an agency takes money back (a de-obligation). Place is the place of performance,
not the recipient's address. Lifetime award totals are never summed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import duckdb

from pipeline.guard import duckdb_connect
from decimal import Decimal

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
TERRITORIES = {"PR": "Puerto Rico", "GU": "Guam", "VI": "U.S. Virgin Islands", "AS": "American Samoa",
               "MP": "Northern Mariana Islands", "UM": "U.S. Minor Outlying Islands",
               "FM": "Micronesia", "MH": "Marshall Islands", "PW": "Palau"}
TERRITORY_COUNTRIES = {"PRI": "PR", "GUM": "GU", "VIR": "VI", "ASM": "AS", "MNP": "MP", "UMI": "UM",
                       "FSM": "FM", "MHL": "MH", "PLW": "PW"}
TYPE_LABELS = {"A": "BPA call", "B": "Purchase order", "C": "Delivery order", "D": "Definitive contract"}


def sql_list(values) -> str:
    return ", ".join("'" + v + "'" for v in values)


class QualityError(RuntimeError):
    """A blocking data-quality check failed; no summary is produced."""


def connect(silver_dir: str | Path, deleted_keys: str | Path | None = None,
            temp_dir: str | Path | None = None, reserve_gib: float = 8,
            apply_deleted_keys: bool = False) -> duckdb.DuckDBPyConnection:
    # Spill to disk is limited to the space above the reserve (at most 2 GiB).
    con = duckdb_connect(temp_dir or Path(silver_dir).parent / "_duckdb_tmp", reserve_gib)
    con.execute("SET preserve_insertion_order = false")
    pattern = str(Path(silver_dir) / "part-*.parquet")
    if deleted_keys:
        con.execute(f"CREATE TABLE deleted AS SELECT DISTINCT contract_transaction_unique_key AS k FROM read_parquet('{deleted_keys}')")
    else:
        con.execute("CREATE TABLE deleted (k VARCHAR)")
    con.execute(f"""
        CREATE VIEW source AS SELECT * FROM read_parquet('{pattern}')""")
    con.execute("""
        CREATE VIEW stale AS SELECT s.* FROM source s JOIN deleted d ON s.contract_transaction_unique_key = d.k""")
    territory_case = " ".join(f"WHEN country = '{c}' THEN '{s}'" for c, s in TERRITORY_COUNTRIES.items())
    con.execute(f"""
        CREATE VIEW tx AS
        SELECT *,
          CASE
            WHEN state IN ({sql_list(STATES)}) AND country IN ('USA', '') THEN 'state'
            WHEN state IN ({sql_list(TERRITORIES)}) OR country IN ({sql_list(TERRITORY_COUNTRIES)}) THEN 'territory'
            WHEN country NOT IN ('USA', '') THEN 'foreign'
            ELSE 'unknown'
          END AS place_kind,
          CASE WHEN state IN ({sql_list(TERRITORIES)}) THEN state {territory_case} ELSE NULL END AS territory,
          CASE
            WHEN award_type_code IN ('A', 'B', 'C', 'D') THEN award_type_code
            WHEN award_type_code = '' AND idv_type_code <> '' THEN 'IDV'
            ELSE 'OTHER'
          END AS type_group,
          NULLIF(recipient_uei, '') AS uei
        FROM (
          SELECT *,
            upper(trim(primary_place_of_performance_state_code)) AS state,
            upper(trim(primary_place_of_performance_country_code)) AS country,
            federal_action_obligation AS amount
          FROM source s
          {"WHERE NOT EXISTS (SELECT 1 FROM deleted d WHERE d.k = s.contract_transaction_unique_key)" if apply_deleted_keys else ""}
        )
    """)
    return con


def cents(value) -> int:
    """Exact integer cents from a DuckDB decimal. Never goes through binary floating point."""
    if value is None:
        return 0
    quantized = Decimal(value).quantize(Decimal("0.01"))
    return int(quantized * 100)


BLOCKING = ("duplicate_keys", "other_fiscal_year", "outside_fiscal_window", "null_obligations", "null_dates")


def summarise(silver_dir: str | Path, fiscal_year: int, snapshot_date: str, source_zip: str, top: int = 10,
              deleted_keys: str | Path | None = None, stale_out: str | Path | None = None,
              reserve_gib: float = 8, apply_deleted_keys: bool = False) -> dict:
    """Summarise the snapshot as published. Keys listed as deleted in the same release's delta file
    are matched and reported for diagnosis, but only removed when `apply_deleted_keys` is set:
    removing them was tested against USAspending's published totals and did not reconcile."""
    con = connect(silver_dir, deleted_keys, reserve_gib=reserve_gib, apply_deleted_keys=apply_deleted_keys)
    start, end = f"{fiscal_year - 1}-10-01", f"{fiscal_year}-09-30"
    checks = con.execute(f"""
        SELECT count(*) AS rows,
               count(*) - count(DISTINCT contract_transaction_unique_key) AS duplicate_keys,
               count(*) FILTER (WHERE action_date_fiscal_year <> '{fiscal_year}') AS other_fiscal_year,
               count(*) FILTER (WHERE action_date < DATE '{start}' OR action_date > DATE '{end}') AS outside_fiscal_window,
               count(*) FILTER (WHERE federal_action_obligation IS NULL) AS null_obligations,
               count(*) FILTER (WHERE action_date IS NULL) AS null_dates,
               count(*) FILTER (WHERE amount < 0) AS negative_rows
        FROM tx""").fetchone()
    checks = dict(zip(("rows", "duplicate_keys", "other_fiscal_year", "outside_fiscal_window", "null_obligations",
                       "null_dates", "negative_rows"), checks))
    failed = {k: checks[k] for k in BLOCKING if checks[k]}
    if failed:
        raise QualityError(f"Blocking quality checks failed: {failed}")
    stale = con.execute("SELECT count(*), sum(federal_action_obligation) FROM stale").fetchone()
    full = con.execute("SELECT count(*), sum(federal_action_obligation) FROM source").fetchone()
    deleted_listed = con.execute("SELECT count(*) FROM deleted").fetchone()[0]
    if stale_out:
        # Audit trail: every row removed, with where it came from, so the exclusion can be checked.
        con.execute(f"""COPY (SELECT contract_transaction_unique_key, contract_award_unique_key, federal_action_obligation,
                                     federal_action_obligation_raw, action_date_raw, last_modified_date,
                                     primary_place_of_performance_state_code, source_file, source_row
                              FROM stale ORDER BY source_file, source_row)
                       TO '{stale_out}' (FORMAT parquet, COMPRESSION zstd)""")
    national = con.execute("""
        SELECT sum(amount), count(*), count(DISTINCT contract_award_unique_key) FROM tx""").fetchone()

    places = con.execute("""
        SELECT place_kind, sum(amount) FROM tx GROUP BY 1""").fetchall()
    territories = con.execute("""
        SELECT territory, sum(amount), count(*) FROM tx WHERE place_kind = 'territory'
        GROUP BY 1 ORDER BY 2 DESC""").fetchall()

    state_rows = con.execute("""
        SELECT state, sum(amount), count(*), count(DISTINCT contract_award_unique_key),
               sum(amount) FILTER (WHERE amount < 0)
        FROM tx WHERE place_kind = 'state' GROUP BY 1""").fetchall()

    agencies = con.execute(f"""
        SELECT state, agency, amount FROM (
          SELECT state, awarding_agency_name AS agency, sum(amount) AS amount,
                 row_number() OVER (PARTITION BY state ORDER BY sum(amount) DESC) AS n
          FROM tx WHERE place_kind = 'state' GROUP BY 1, 2)
        WHERE n <= {top} ORDER BY state, amount DESC""").fetchall()

    types = con.execute("""
        SELECT state, type_group, sum(amount), count(*)
        FROM tx WHERE place_kind = 'state' GROUP BY 1, 2 ORDER BY 1, 3 DESC""").fetchall()

    recipients = con.execute(f"""
        SELECT state, uei, name, parent, amount, awards, example FROM (
          SELECT state, uei,
                 mode(recipient_name) AS name,
                 mode(NULLIF(recipient_parent_name, '')) AS parent,
                 sum(amount) AS amount,
                 count(DISTINCT contract_award_unique_key) AS awards,
                 arg_max(prime_award_base_transaction_description, amount) AS example,
                 row_number() OVER (PARTITION BY state ORDER BY sum(amount) DESC) AS n
          FROM tx WHERE place_kind = 'state' GROUP BY state, uei)
        WHERE n <= {top} ORDER BY state, amount DESC""").fetchall()

    agency_contracts = con.execute("""
        SELECT awarding_agency_name, sum(amount) FROM tx
        WHERE place_kind = 'state' AND type_group IN ('A', 'B', 'C', 'D') GROUP BY 1""").fetchall()
    national_agencies = con.execute(f"""
        SELECT awarding_agency_name, sum(amount) FROM tx GROUP BY 1 ORDER BY 2 DESC LIMIT {top}""").fetchall()

    states = {}
    ranked = sorted(state_rows, key=lambda r: Decimal(r[1] or 0), reverse=True)
    for rank, (code, amount, transactions, awards, deobligated) in enumerate(ranked, 1):
        states[code] = {"name": STATES[code], "net_obligations_cents": cents(amount), "transactions": transactions,
                        "awards": awards, "deobligations_cents": cents(deobligated), "rank": rank,
                        "agencies": [], "types": [], "recipients": []}
    for code, agency, amount in agencies:
        total = states[code]["net_obligations_cents"]
        amount_cents = cents(amount)
        states[code]["agencies"].append({"name": agency or "Not reported", "amount_cents": amount_cents,
                                         "share": round(amount_cents / total, 4) if total else None})
    for code, group, amount, count in types:
        label = TYPE_LABELS.get(group, "Indefinite-delivery vehicle (IDV)" if group == "IDV" else "Other or not reported")
        states[code]["types"].append({"code": group, "name": label, "amount_cents": cents(amount), "transactions": count})
    for code, uei, name, parent, amount, awards, example in recipients:
        states[code]["recipients"].append({
            "uei": uei, "name": name if uei else "Recipients without an ID (combined)",
            "parent": parent if uei else None, "amount_cents": cents(amount), "awards": awards,
            "example": (example or "").strip()[:160] or None})

    return {
        "fiscal_year": fiscal_year,
        "snapshot_date": snapshot_date,
        "source_zip": source_zip,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "measure": "Net obligations by place of performance (sum of federal_action_obligation over transactions)",
        "scope": "Prime contract transactions, including indefinite-delivery vehicles",
        "national": {"net_obligations_cents": cents(national[0]), "transactions": national[1], "awards": national[2],
                     "agencies": [{"name": n or "Not reported", "amount_cents": cents(a)} for n, a in national_agencies]},
        "outside_map": {
            "territories": [{"code": c, "name": TERRITORIES.get(c, c), "amount_cents": cents(a), "transactions": n}
                            for c, a, n in territories],
            **{kind + "_cents": cents(amount) for kind, amount in places if kind != "state"},
        },
        "checks": checks,
        "agency_contracts_in_states": {n or "Not reported": cents(a) for n, a in agency_contracts},
        "deleted_key_matches": {"rows": stale[0], "cents": cents(stale[1]),
                                "deleted_keys_listed": deleted_listed, "applied": apply_deleted_keys,
                                "meaning": ("Rows whose keys the same release's delta file lists as deleted. "
                                            "Reported for diagnosis; not removed unless applied.")},
        "full_snapshot": {"rows": full[0], "net_obligations_cents": cents(full[1]),
                          "note": "The source file as published, before stale keys are removed"},
        "states": dict(sorted(states.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("silver_dir")
    parser.add_argument("output")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--snapshot-date", required=True)
    parser.add_argument("--source-zip", required=True)
    parser.add_argument("--deleted-keys", help="Parquet of transaction keys the delta file deletes")
    parser.add_argument("--stale-out", help="write the rows matching deleted keys here for audit")
    parser.add_argument("--apply-deleted-keys", action="store_true",
                        help="remove matched rows (experimental; did not reconcile for FY2025)")
    parser.add_argument("--reserve-gib", type=float, default=8)
    args = parser.parse_args()
    result = summarise(args.silver_dir, args.year, args.snapshot_date, args.source_zip,
                       deleted_keys=args.deleted_keys, stale_out=args.stale_out, reserve_gib=args.reserve_gib,
                       apply_deleted_keys=args.apply_deleted_keys)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=1))
    print(json.dumps({"national_cents": result["national"]["net_obligations_cents"], "states": len(result["states"]),
                      "checks": result["checks"], "deleted_key_matches": result["deleted_key_matches"],
                      "full_snapshot": result["full_snapshot"],
                      "outside_map": {k: v for k, v in result["outside_map"].items()
                                                                  if k != "territories"}}, indent=2))


if __name__ == "__main__":
    main()
