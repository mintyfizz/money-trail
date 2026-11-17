"""Aggregate typed transactions of one kind (contracts or assistance) into the summaries the dashboard reads.

The measure is a sum over transactions, never over lifetime award totals:
- contracts, and every assistance category except loans: `federal_action_obligation`, net obligations,
  which can be negative when an agency takes money back (a de-obligation);
- loans: `original_loan_subsidy_cost`, the government's expected cost. Face value is reported beside it
  and never added to anything.
This is USAspending's own pragmatic rule. Place is the place of performance, not the recipient's address.
Assistance place comes from the state FIPS code. Freely associated states (FM, MH, PW) count as foreign.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import duckdb

from pipeline.guard import duckdb_connect, write_arrow_parquet
from pipeline.kinds import (FREELY_ASSOCIATED, KINDS, STATE_FIPS, Kind, UnmappedCodeError,
                            load_categories)

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
               "MP": "Northern Mariana Islands", "UM": "U.S. Minor Outlying Islands"}
TERRITORY_COUNTRIES = {"PRI": "PR", "GUM": "GU", "VIR": "VI", "ASM": "AS", "MNP": "MP", "UMI": "UM"}
FREELY_ASSOCIATED_NAMES = {"FM": "Micronesia", "MH": "Marshall Islands", "PW": "Palau"}
FREELY_ASSOCIATED_COUNTRIES = {"FSM": "FM", "MHL": "MH", "PLW": "PW"}
TYPE_LABELS = {"A": "BPA call", "B": "Purchase order", "C": "Delivery order", "D": "Definitive contract",
               "IDV": "Indefinite-delivery vehicle (IDV)"}
# Assistance records that are not one identifiable recipient: 1 = aggregate, 3 = redacted for privacy.
NOT_ONE_RECIPIENT = ("1", "3")

assert set(FREELY_ASSOCIATED) == set(FREELY_ASSOCIATED_NAMES)


def sql_list(values) -> str:
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


def sql_map(expr: str, mapping: dict, default: str = "NULL") -> str:
    whens = " ".join(f"WHEN {sql_list([k])} THEN {sql_list([v])}" for k, v in mapping.items())
    return f"CASE {expr} {whens} ELSE {default} END"


class QualityError(RuntimeError):
    """A blocking data-quality check failed; no summary is produced."""


def resolve_kind(kind: str | Kind) -> Kind:
    return KINDS[kind] if isinstance(kind, str) else kind


def connect(silver_dir: str | Path, deleted_keys: str | Path | None = None,
            temp_dir: str | Path | None = None, reserve_gib: float = 8,
            apply_deleted_keys: bool = False, kind: str | Kind = "contracts",
            categories: dict | None = None) -> duckdb.DuckDBPyConnection:
    """Views: `source` (as typed), `norm` (every row with place, category and measure), `stale` (rows
    whose key the delta deletes) and `tx` (what is summed: `norm`, minus `stale` only if applied)."""
    kind = resolve_kind(kind)
    categories = categories or load_categories()
    # Spill to disk is limited to the space above the reserve (at most 2 GiB).
    con = duckdb_connect(temp_dir or Path(silver_dir).parent / "_duckdb_tmp", reserve_gib)
    con.execute("SET preserve_insertion_order = false")
    pattern = str(Path(silver_dir) / "part-*.parquet")
    if deleted_keys:
        con.execute(f"CREATE TABLE deleted AS SELECT DISTINCT {kind.key} AS k FROM read_parquet('{deleted_keys}')")
    else:
        con.execute("CREATE TABLE deleted (k VARCHAR)")
    # union_by_name: a column some members lack (an optional one) reads as NULL there, never as zero.
    con.execute(f"CREATE VIEW source AS SELECT * FROM read_parquet('{pattern}', union_by_name = true)")
    present = {r[0] for r in con.execute("DESCRIBE source").fetchall()}

    codes = categories["codes"][kind.name]
    if kind.name == "contracts":
        state = "upper(trim(coalesce(primary_place_of_performance_state_code, '')))"
        code = ("CASE WHEN trim(coalesce(award_type_code, '')) <> '' THEN upper(trim(award_type_code)) "
                "WHEN trim(coalesce(idv_type_code, '')) <> '' THEN 'IDV' ELSE '' END")
        fips, record_type, face, pragmatic = "NULL::VARCHAR", "NULL::VARCHAR", "NULL::DECIMAL(23,2)", None
        loan_measure = None
    else:
        raw_fips = "trim(coalesce(prime_award_transaction_place_of_performance_state_fips_code, ''))"
        fips = f"CASE WHEN {raw_fips} = '' THEN '' ELSE lpad({raw_fips}, 2, '0') END"
        state = sql_map("fips", STATE_FIPS, default="CASE WHEN fips = '' THEN '' ELSE NULL END")
        code = "trim(coalesce(assistance_type_code, ''))"
        record_type = "trim(coalesce(record_type_code, ''))"
        face = "face_value_of_loan"
        pragmatic = "generated_pragmatic_obligations" if "generated_pragmatic_obligations" in present else None
        loan_measure = "original_loan_subsidy_cost"
    measure = (f"CASE WHEN category = 'loans' THEN {loan_measure} ELSE federal_action_obligation END"
               if loan_measure else "federal_action_obligation")
    fa_countries = sql_list(FREELY_ASSOCIATED_COUNTRIES)
    con.execute(f"""
        CREATE VIEW norm AS
        SELECT *,
          CASE
            WHEN state IN ({sql_list(FREELY_ASSOCIATED)}) OR country IN ({fa_countries}) THEN 'foreign'
            WHEN state IN ({sql_list(STATES)}) AND country IN ('USA', '') THEN 'state'
            WHEN state IN ({sql_list(TERRITORIES)}) OR country IN ({sql_list(TERRITORY_COUNTRIES)}) THEN 'territory'
            WHEN country NOT IN ('USA', '') THEN 'foreign'
            ELSE 'unknown'
          END AS place_kind,
          CASE WHEN state IN ({sql_list(TERRITORIES)}) THEN state
               ELSE {sql_map("country", TERRITORY_COUNTRIES)} END AS territory,
          CASE WHEN state IN ({sql_list(FREELY_ASSOCIATED)}) THEN state
               ELSE {sql_map("country", FREELY_ASSOCIATED_COUNTRIES)} END AS freely_associated,
          NULLIF(trim(coalesce(recipient_uei, '')), '') AS uei,
          coalesce(NULLIF(trim(coalesce(awarding_agency_code, '')), ''),
                   'name:' || coalesce(awarding_agency_name, '')) AS agency_id
        FROM (
          SELECT *, {measure} AS amount,
                 CASE WHEN category = 'loans' THEN {face} END AS face_value,
                 {pragmatic or "NULL::DECIMAL(23,2)"} AS pragmatic_amount
          FROM (
            SELECT *, {state} AS state, {sql_map("type_code", codes)} AS category
            FROM (
              SELECT *, {fips} AS fips, {code} AS type_code, {record_type} AS record_type,
                     upper(trim(coalesce(primary_place_of_performance_country_code, ''))) AS country
              FROM source)))""")
    con.execute(f"CREATE VIEW stale AS SELECT n.* FROM norm n JOIN deleted d ON n.{kind.key} = d.k")
    con.execute(f"""
        CREATE VIEW tx AS SELECT * FROM norm n
        {f"WHERE NOT EXISTS (SELECT 1 FROM deleted d WHERE d.k = n.{kind.key})" if apply_deleted_keys else ""}""")
    return con


def has_pragmatic(con, kind: Kind) -> bool:
    columns = {r[0] for r in con.execute("DESCRIBE source").fetchall()}
    return kind.name == "assistance" and "generated_pragmatic_obligations" in columns


def cents(value) -> int:
    """Exact integer cents from a DuckDB decimal. Never goes through binary floating point."""
    if value is None:
        return 0
    quantized = Decimal(value).quantize(Decimal("0.01"))
    return int(quantized * 100)


BLOCKING = {
    "contracts": ("duplicate_keys", "other_fiscal_year", "outside_fiscal_window", "null_obligations", "null_dates"),
    # USAspending treats a missing assistance amount as zero; it is counted and disclosed, not blocked.
    "assistance": ("duplicate_keys", "other_fiscal_year", "outside_fiscal_window", "null_dates"),
}


def place_of(kind_, state, territory):
    return state if kind_ == "state" else (territory if kind_ == "territory" else kind_)


def summarise(silver_dir: str | Path, fiscal_year: int, snapshot_date: str, source_zip: str, top: int = 10,
              deleted_keys: str | Path | None = None, stale_out: str | Path | None = None,
              reserve_gib: float = 8, apply_deleted_keys: bool = False, kind: str | Kind = "contracts",
              categories: dict | None = None) -> dict:
    """Summarise the snapshot as published. Keys listed as deleted in the same release's delta file
    are matched and reported for diagnosis, but only removed when `apply_deleted_keys` is set:
    removing them was tested against USAspending's published totals and did not reconcile."""
    kind = resolve_kind(kind)
    categories = categories or load_categories()
    con = connect(silver_dir, deleted_keys, reserve_gib=reserve_gib, apply_deleted_keys=apply_deleted_keys,
                  kind=kind, categories=categories)
    key, award_key = kind.key, kind.award_key

    unmapped = con.execute("""
        SELECT type_code, count(*) FROM tx WHERE category IS NULL GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    if unmapped:
        raise UnmappedCodeError(f"Unrecognised {kind.name} award type codes {dict(unmapped)}; add them to "
                                "categories.json with a cited source")

    pragmatic = has_pragmatic(con, kind)
    start, end = f"{fiscal_year - 1}-10-01", f"{fiscal_year}-09-30"
    names = ("rows", "duplicate_keys", "other_fiscal_year", "outside_fiscal_window", "null_obligations",
             "null_dates", "negative_rows", "rows_without_amount", "unrecognised_state_fips_rows",
             "pragmatic_null_rows", "pragmatic_mismatch_rows", "pragmatic_mismatch_cents")
    values = con.execute(f"""
        SELECT count(*),
               count(*) - count(DISTINCT {key}),
               count(*) FILTER (WHERE action_date_fiscal_year <> '{fiscal_year}'),
               count(*) FILTER (WHERE action_date < DATE '{start}' OR action_date > DATE '{end}'),
               count(*) FILTER (WHERE federal_action_obligation IS NULL),
               count(*) FILTER (WHERE action_date IS NULL),
               count(*) FILTER (WHERE amount < 0),
               count(*) FILTER (WHERE amount IS NULL),
               count(*) FILTER (WHERE fips <> '' AND state IS NULL),
               count(*) FILTER (WHERE {"true" if pragmatic else "false"} AND pragmatic_amount IS NULL),
               count(*) FILTER (WHERE {"true" if pragmatic else "false"} AND pragmatic_amount IS NOT NULL
                                  AND coalesce(amount, 0) <> pragmatic_amount),
               sum(pragmatic_amount - coalesce(amount, 0)) FILTER (WHERE {"true" if pragmatic else "false"}
                                  AND pragmatic_amount IS NOT NULL AND coalesce(amount, 0) <> pragmatic_amount)
        FROM tx""").fetchone()
    checks = dict(zip(names, values))
    if kind.name == "contracts":
        # Fields that do not exist for contracts are left out rather than reported as zero.
        for name in ("rows_without_amount", "unrecognised_state_fips_rows", "pragmatic_null_rows",
                     "pragmatic_mismatch_rows", "pragmatic_mismatch_cents"):
            checks.pop(name)
    elif not pragmatic:
        # Column absent from the source: not checked, which is different from checked and equal.
        checks.update(pragmatic_null_rows=None, pragmatic_mismatch_rows=None, pragmatic_mismatch_cents=None)
    else:
        checks["pragmatic_mismatch_cents"] = cents(checks["pragmatic_mismatch_cents"])
    failed = {k: checks[k] for k in BLOCKING[kind.name] if checks[k]}
    if failed:
        raise QualityError(f"Blocking quality checks failed: {failed}")

    stale = con.execute("SELECT count(*), sum(amount) FROM stale").fetchone()
    full = con.execute("SELECT count(*), sum(amount) FROM norm").fetchone()
    deleted_listed = con.execute("SELECT count(*) FROM deleted").fetchone()[0]
    # Diagnostic: group matched rows by the exact last-modified time of their surviving twin (same
    # key apart from the final segment, same modification, date and amount, not itself deleted).
    # Each deleted row is counted once. Rows with no twin, and rows whose candidate twins carry
    # different times, are kept apart from the batches.
    in_state = ("s.category = 'contracts' AND s.place_kind = 'state'" if kind.name == "contracts"
                else "s.place_kind = 'state'")
    batches = con.execute(f"""
        WITH m AS (
          SELECT s.{key} AS k, s.amount, regexp_replace(s.{key}, '_[^_]*$', '') AS ak,
                 s.modification_number, s.action_date, ({in_state}) AS in_state
          FROM stale s),
        twins AS (
          SELECT m.k, any_value(m.amount) AS amount, any_value(m.in_state) AS in_state,
                 count(t.{key}) AS candidates,
                 CASE WHEN count(DISTINCT t.last_modified_date) > 1 THEN 'ambiguous'
                      ELSE min(t.last_modified_date) END AS twin_modified
          FROM m LEFT JOIN norm t
            ON regexp_replace(t.{key}, '_[^_]*$', '') = m.ak
           AND t.modification_number = m.modification_number AND t.action_date = m.action_date
           AND t.amount = m.amount AND t.{key} <> m.k
           AND NOT EXISTS (SELECT 1 FROM deleted d WHERE d.k = t.{key})
          GROUP BY m.k)
        SELECT twin_modified, count(*), sum(amount),
               count(*) FILTER (WHERE in_state), sum(amount) FILTER (WHERE in_state),
               count(*) FILTER (WHERE candidates > 1)
        FROM twins GROUP BY 1 ORDER BY abs(sum(amount)) DESC NULLS LAST""").fetchall()
    if stale_out:
        # Audit trail: every matched row, with where it came from, so any exclusion can be checked.
        audit = con.execute(f"""SELECT {key}, {award_key}, amount, federal_action_obligation_raw, action_date_raw,
                                       last_modified_date, place_kind, state, category, source_file, source_row
                                FROM stale ORDER BY source_file, source_row""").to_arrow_table()
        write_arrow_parquet(audit, stale_out, reserve_gib)

    national = con.execute(f"""
        SELECT sum(amount), sum(federal_action_obligation), count(*), count(DISTINCT {award_key}) FROM tx""").fetchone()
    places = con.execute("SELECT place_kind, sum(amount), count(*) FROM tx GROUP BY 1").fetchall()
    territories = con.execute("""
        SELECT territory, sum(amount), count(*) FROM tx WHERE place_kind = 'territory'
        GROUP BY 1 ORDER BY 2 DESC NULLS LAST""").fetchall()
    associated = con.execute("""
        SELECT freely_associated, sum(amount), count(*) FROM tx
        WHERE place_kind = 'foreign' AND freely_associated IS NOT NULL GROUP BY 1 ORDER BY 2 DESC NULLS LAST""").fetchall()
    unknown_fips = con.execute("""
        SELECT fips, count(*), sum(amount) FROM tx WHERE fips <> '' AND state IS NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 50""").fetchall()

    state_rows = con.execute(f"""
        SELECT state, sum(amount), sum(federal_action_obligation), count(*), count(DISTINCT {award_key}),
               sum(amount) FILTER (WHERE amount < 0)
        FROM tx WHERE place_kind = 'state' GROUP BY 1""").fetchall()

    # Full agency x place x category matrix: the only source for agency filters and per-agency views.
    matrix = con.execute("""
        SELECT agency_id, mode(awarding_agency_name), place_kind,
               CASE place_kind WHEN 'state' THEN state WHEN 'territory' THEN territory ELSE place_kind END,
               category, sum(amount), count(*), count(*) FILTER (WHERE amount IS NULL), sum(face_value)
        FROM tx GROUP BY 1, 3, 4, 5 ORDER BY 1, 4, 5""").fetchall()

    agencies = con.execute(f"""
        SELECT state, agency, amount FROM (
          SELECT state, mode(awarding_agency_name) AS agency, sum(amount) AS amount,
                 row_number() OVER (PARTITION BY state ORDER BY sum(amount) DESC NULLS LAST) AS n
          FROM tx WHERE place_kind = 'state' GROUP BY state, agency_id)
        WHERE n <= {top} ORDER BY state, amount DESC""").fetchall()

    label = ("NULL" if kind.name == "contracts" else "mode(NULLIF(assistance_type_description, ''))")
    types = con.execute(f"""
        SELECT state, type_code, category, {label}, sum(amount), count(*)
        FROM tx WHERE place_kind = 'state' GROUP BY 1, 2, 3 ORDER BY 1, 5 DESC NULLS LAST""").fetchall()

    state_categories = con.execute("""
        SELECT state, category, sum(amount), count(*), count(*) FILTER (WHERE amount IS NULL), sum(face_value)
        FROM tx WHERE place_kind = 'state' GROUP BY 1, 2 ORDER BY 1, 3 DESC NULLS LAST""").fetchall()
    national_categories = con.execute(f"""
        SELECT category, sum(amount), sum(federal_action_obligation), count(*),
               count(*) FILTER (WHERE amount IS NULL), sum(face_value), count(DISTINCT {award_key}),
               sum(amount) FILTER (WHERE place_kind = 'state')
        FROM tx GROUP BY 1 ORDER BY 2 DESC NULLS LAST""").fetchall()

    # Only identifiable recipients are ranked: no aggregate or redacted records, no rows without an ID.
    ranked = f"uei IS NOT NULL AND coalesce(record_type, '') NOT IN ({sql_list(NOT_ONE_RECIPIENT)})"
    recipients = con.execute(f"""
        SELECT state, uei, name, parent, amount, awards, example FROM (
          SELECT state, uei,
                 mode(recipient_name) AS name,
                 mode(NULLIF(recipient_parent_name, '')) AS parent,
                 sum(amount) AS amount,
                 count(DISTINCT {award_key}) AS awards,
                 arg_max(prime_award_base_transaction_description, amount) AS example,
                 row_number() OVER (PARTITION BY state ORDER BY sum(amount) DESC NULLS LAST) AS n
          FROM tx WHERE place_kind = 'state' AND {ranked} GROUP BY state, uei)
        WHERE n <= {top} ORDER BY state, amount DESC""").fetchall()
    unranked = con.execute(f"""
        SELECT state, sum(amount), count(*),
               count(*) FILTER (WHERE record_type = '1'), count(*) FILTER (WHERE record_type = '3'),
               count(*) FILTER (WHERE uei IS NULL)
        FROM tx WHERE place_kind = 'state' AND NOT ({ranked}) GROUP BY 1""").fetchall()

    agency_contracts = con.execute("""
        SELECT mode(awarding_agency_name), sum(amount) FROM tx
        WHERE place_kind = 'state' AND category = 'contracts' GROUP BY agency_id""").fetchall()
    national_agencies = con.execute(f"""
        SELECT mode(awarding_agency_name), sum(amount) FROM tx GROUP BY agency_id
        ORDER BY 2 DESC NULLS LAST LIMIT {top}""").fetchall()

    labels = {c: v["label"] for c, v in categories["categories"].items()}
    states = {}
    ordered = sorted(state_rows, key=lambda r: Decimal(r[1] or 0), reverse=True)
    for rank, (code, amount, obligations, transactions, awards, negative) in enumerate(ordered, 1):
        states[code] = {"name": STATES[code], "amount_cents": cents(amount),
                        "net_obligations_cents": cents(obligations), "transactions": transactions,
                        "awards": awards, "deobligations_cents": cents(negative), "rank": rank,
                        "categories": [], "agencies": [], "types": [], "recipients": [],
                        "recipients_not_ranked": None}
    for code, category, amount, count, without, face in state_categories:
        entry = {"category": category, "name": labels[category], "amount_cents": cents(amount),
                 "transactions": count, "rows_without_amount": without}
        if category == "loans":
            entry["face_value_cents"] = cents(face)
        states[code]["categories"].append(entry)
    for code, agency, amount in agencies:
        total = states[code]["amount_cents"]
        amount_cents = cents(amount)
        states[code]["agencies"].append({"name": agency or "Not reported", "amount_cents": amount_cents,
                                         "share": round(amount_cents / total, 4) if total else None})
    for code, type_code, category, description, amount, count in types:
        name = TYPE_LABELS.get(type_code) if kind.name == "contracts" else description
        states[code]["types"].append({"code": type_code, "category": category, "name": name or labels[category],
                                      "amount_cents": cents(amount), "transactions": count})
    for code, uei, name, parent, amount, awards, example in recipients:
        states[code]["recipients"].append({
            "uei": uei, "name": name, "parent": parent, "amount_cents": cents(amount), "awards": awards,
            "example": (example or "").strip()[:160] or None})
    for code, amount, count, aggregate, redacted, without_id in unranked:
        states[code]["recipients_not_ranked"] = {
            "amount_cents": cents(amount), "transactions": count, "aggregate_records": aggregate,
            "redacted_records": redacted, "without_recipient_id": without_id,
            "meaning": "Not ranked: aggregate or privacy-redacted records, or rows without a recipient ID"}

    matrix_rows = [[agency_id, name or "Not reported", place_kind, place, category, cents(amount), count, without,
                    cents(face) if category == "loans" else None]
                   for agency_id, name, place_kind, place, category, amount, count, without, face in matrix]
    national_cents = cents(national[0])
    gate_matrix(matrix_rows, states, national_cents)

    outside = {f"{k}_cents": cents(a) for k, a, _ in places if k != "state"}
    outside.update({f"{k}_transactions": n for k, _, n in places if k != "state"})
    return {
        "kind": kind.name,
        "fiscal_year": fiscal_year,
        "snapshot_date": snapshot_date,
        "source_zip": source_zip,
        "category_map_version": categories["version"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "measure": ("Net obligations by place of performance (sum of federal_action_obligation over transactions)"
                    if kind.name == "contracts" else
                    "Spending by place of performance: sum of federal_action_obligation over transactions, "
                    "except loans, which use original_loan_subsidy_cost. Loan face value is shown separately."),
        "scope": ("Prime contract transactions, including indefinite-delivery vehicles" if kind.name == "contracts"
                  else "Prime financial assistance transactions, awarding agency"),
        "national": {"amount_cents": national_cents, "net_obligations_cents": cents(national[1]),
                     "transactions": national[2], "awards": national[3],
                     "categories": [{"category": c, "name": labels[c], "amount_cents": cents(a),
                                     "net_obligations_cents": cents(o), "transactions": n, "rows_without_amount": w,
                                     "awards": aw, "in_states_cents": cents(s),
                                     **({"face_value_cents": cents(f)} if c == "loans" else {})}
                                    for c, a, o, n, w, f, aw, s in national_categories],
                     "agencies": [{"name": n or "Not reported", "amount_cents": cents(a)} for n, a in national_agencies]},
        "outside_map": {
            "territories": [{"code": c, "name": TERRITORIES.get(c, c), "amount_cents": cents(a), "transactions": n}
                            for c, a, n in territories],
            "freely_associated_states": [
                {"code": c, "name": FREELY_ASSOCIATED_NAMES[c], "amount_cents": cents(a), "transactions": n}
                for c, a, n in associated],
            "freely_associated_note": "Counted as foreign, as Treasury classifies them",
            **outside,
        },
        "checks": checks,
        "unrecognised_state_fips": [{"fips": f, "rows": n, "amount_cents": cents(a)} for f, n, a in unknown_fips],
        "agency_contracts_in_states": {n or "Not reported": cents(a) for n, a in agency_contracts},
        "deleted_key_matches": {"rows": stale[0], "cents": cents(stale[1]),
                                "deleted_keys_listed": deleted_listed, "applied": apply_deleted_keys,
                                "meaning": ("Rows whose keys the same release's delta file lists as deleted. "
                                            "Reported for diagnosis; not removed unless applied.")},
        "deleted_key_batches": [
            {"twin_last_modified": b if b and b != "ambiguous" else None,
             "match": "no twin" if b is None else ("twins at different times" if b == "ambiguous" else "twin"),
             "rows": n, "cents": cents(c), "in_state_rows": sn, "in_state_cents": cents(sc),
             **({"state_contract_rows": sn, "state_contract_cents": cents(sc)} if kind.name == "contracts" else {}),
             "rows_with_several_twin_candidates": multi}
            for b, n, c, sn, sc, multi in batches],
        "full_snapshot": {"rows": full[0], "amount_cents": cents(full[1]),
                          **({"net_obligations_cents": cents(full[1])} if kind.name == "contracts" else {}),
                          "note": "The source file as published. Totals above equal this unless deleted keys were applied."},
        "matrix": {"columns": ["agency_id", "agency", "place_kind", "place", "category", "amount_cents",
                               "transactions", "rows_without_amount", "face_value_cents"],
                   "rows": matrix_rows},
        "states": dict(sorted(states.items())),
    }


def gate_matrix(rows: list, states: dict, national_cents: int) -> None:
    """The matrix must add up exactly: per state, per category within a state, and nationally."""
    by_state, by_state_category, total = {}, {}, 0
    for _, _, place_kind, place, category, amount, *_ in rows:
        total += amount
        if place_kind == "state":
            by_state[place] = by_state.get(place, 0) + amount
            by_state_category[(place, category)] = by_state_category.get((place, category), 0) + amount
    problems = []
    if total != national_cents:
        problems.append(f"national {total} != {national_cents}")
    for code, state in states.items():
        if by_state.get(code, 0) != state["amount_cents"]:
            problems.append(f"{code} {by_state.get(code, 0)} != {state['amount_cents']}")
        for entry in state["categories"]:
            if by_state_category.get((code, entry["category"]), 0) != entry["amount_cents"]:
                problems.append(f"{code}/{entry['category']}")
    if set(by_state) - set(states):
        problems.append(f"matrix states without totals {sorted(set(by_state) - set(states))}")
    if problems:
        raise QualityError(f"Agency matrix does not add up: {problems[:10]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("silver_dir")
    parser.add_argument("output")
    parser.add_argument("--kind", choices=sorted(KINDS), default="contracts")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--snapshot-date", required=True)
    parser.add_argument("--source-zip", required=True)
    parser.add_argument("--deleted-keys", help="Parquet of transaction keys the delta file deletes")
    parser.add_argument("--stale-out", help="write the rows matching deleted keys here for audit")
    parser.add_argument("--apply-deleted-keys", action="store_true",
                        help="remove matched rows (experimental; did not reconcile for FY2025)")
    parser.add_argument("--reserve-gib", type=float, default=8)
    args = parser.parse_args()
    result = summarise(args.silver_dir, args.year, args.snapshot_date, args.source_zip, kind=args.kind,
                       deleted_keys=args.deleted_keys, stale_out=args.stale_out, reserve_gib=args.reserve_gib,
                       apply_deleted_keys=args.apply_deleted_keys)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=1))
    print(json.dumps({"kind": result["kind"], "national_cents": result["national"]["amount_cents"],
                      "states": len(result["states"]), "checks": result["checks"],
                      "categories": {c["category"]: c["amount_cents"] for c in result["national"]["categories"]},
                      "deleted_key_matches": result["deleted_key_matches"], "full_snapshot": result["full_snapshot"],
                      "outside_map": {k: v for k, v in result["outside_map"].items() if k.endswith("_cents")}},
                     indent=2))


if __name__ == "__main__":
    main()
