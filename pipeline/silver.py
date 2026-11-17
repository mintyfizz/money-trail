"""Stream a USAspending contracts ZIP into a typed transaction table, one Parquet file per CSV member.

The national archives are too large to keep as a full text copy on a runner, so this step keeps
only the columns the analysis needs. The original ZIP stays in a release as the raw layer.
Every row keeps its source file and row number, and the original obligation string, so a typed
value can always be traced back.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import time
import zipfile

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as arrow_csv
import pyarrow.parquet as parquet

COLUMNS = (
    "contract_transaction_unique_key", "contract_award_unique_key", "award_id_piid", "modification_number",
    "federal_action_obligation", "action_date", "action_date_fiscal_year",
    "awarding_agency_code", "awarding_agency_name", "awarding_sub_agency_name",
    "funding_agency_code", "funding_agency_name",
    "recipient_uei", "recipient_name", "recipient_parent_uei", "recipient_parent_name",
    "primary_place_of_performance_country_code", "primary_place_of_performance_state_code",
    "primary_place_of_performance_state_name", "prime_award_transaction_place_of_performance_county_fips_code",
    "primary_place_of_performance_county_name",
    "award_type_code", "award_type", "idv_type_code", "idv_type", "action_type_code",
    "naics_code", "naics_description", "product_or_service_code_description",
    "prime_award_base_transaction_description", "last_modified_date",
)
DELTA_COLUMN = "correction_delete_ind"
MONEY = pa.decimal128(23, 2)


def read_header(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> list[str]:
    with archive.open(info) as stream, io.TextIOWrapper(stream, encoding="utf-8-sig", newline="") as text:
        return next(csv.reader(text))


def to_money(values: pa.Array) -> tuple[pa.Array, int]:
    """Cast obligation strings to decimal; blanks become null. Returns (array, unparseable count)."""
    stripped = pc.utf8_trim_whitespace(values)
    blank = pc.equal(stripped, "")
    cleaned = pc.if_else(blank, pa.scalar(None, pa.string()), stripped)
    try:
        return pc.cast(cleaned, MONEY), 0
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
        parsed, bad = [], 0
        for value in cleaned.to_pylist():
            if value is None:
                parsed.append(None)
                continue
            try:
                parsed.append(pa.scalar(value).cast(MONEY).as_py())
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
                parsed.append(None)
                bad += 1
        return pa.array(parsed, MONEY), bad


def to_date(values: pa.Array) -> tuple[pa.Array, int]:
    stripped = pc.utf8_trim_whitespace(values)
    blank = pc.equal(stripped, "")
    cleaned = pc.if_else(blank, pa.scalar(None, pa.string()), stripped)
    parsed = pc.strptime(cleaned, format="%Y-%m-%d", unit="s", error_is_null=True)
    bad = pc.sum(pc.and_(pc.is_valid(cleaned), pc.is_null(parsed))).as_py() or 0
    return pc.cast(parsed, pa.date32()), bad


def type_batch(batch: pa.RecordBatch, source_file: str, first_row: int, delta: bool) -> tuple[pa.Table, dict]:
    table = pa.Table.from_batches([batch])
    money, bad_money = to_money(table["federal_action_obligation"].combine_chunks())
    action_date, bad_dates = to_date(table["action_date"].combine_chunks())
    columns = {name: table[name] for name in table.column_names}
    columns["federal_action_obligation_raw"] = columns.pop("federal_action_obligation")
    columns["federal_action_obligation"] = money
    columns["action_date"] = action_date
    columns["source_file"] = pa.array([source_file] * table.num_rows, pa.string())
    columns["source_row"] = pa.array(range(first_row, first_row + table.num_rows), pa.int64())
    order = ["contract_transaction_unique_key", "federal_action_obligation", "action_date"]
    order += [c for c in COLUMNS if c not in order and c != "federal_action_obligation"]
    order += ["federal_action_obligation_raw"] + ([DELTA_COLUMN] if delta else []) + ["source_file", "source_row"]
    typed = pa.table({name: columns[name] for name in order})
    keys = table["contract_transaction_unique_key"].combine_chunks()
    blank_keys = pc.sum(pc.equal(pc.utf8_trim_whitespace(keys), "")).as_py() or 0
    total = pc.sum(money).as_py()
    stats = {"rows": table.num_rows, "bad_money": bad_money, "bad_dates": bad_dates, "blank_keys": blank_keys,
             "obligation_sum": str(total) if total is not None else "0"}
    return typed, stats


def extract(zip_path: str | Path, out_dir: str | Path, block_mib: int = 16) -> dict:
    """Write one typed Parquet file per CSV member. Never extracts the CSV to disk."""
    zip_path, out_dir = Path(zip_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = {"source_zip": zip_path.name, "members": [], "columns": list(COLUMNS)}
    with zipfile.ZipFile(zip_path) as archive:
        members = sorted((i for i in archive.infolist() if i.filename.lower().endswith(".csv")),
                         key=lambda i: i.filename)
        if not members:
            raise ValueError(f"{zip_path.name} contains no CSV members")
        for index, info in enumerate(members):
            header = read_header(archive, info)
            delta = DELTA_COLUMN in header
            wanted = list(COLUMNS) + ([DELTA_COLUMN] if delta else [])
            missing = [c for c in wanted if c not in header]
            if missing:
                raise ValueError(f"{info.filename} is missing expected columns: {missing}")
            target = out_dir / f"part-{index:04d}.parquet"
            member = {"source_file": info.filename, "output": target.name, "rows": 0, "bad_money": 0,
                      "bad_dates": 0, "blank_keys": 0, "obligation_sum": "0", "delta": delta,
                      "uncompressed_bytes": info.file_size}
            total = None
            writer = None
            with archive.open(info) as stream:
                reader = arrow_csv.open_csv(
                    stream,
                    read_options=arrow_csv.ReadOptions(block_size=block_mib * 1024 * 1024, encoding="utf8"),
                    parse_options=arrow_csv.ParseOptions(newlines_in_values=True),
                    convert_options=arrow_csv.ConvertOptions(
                        include_columns=wanted, column_types={c: pa.string() for c in wanted},
                        strings_can_be_null=False))
                row = 2  # row 1 is the header; data rows are numbered as they appear in the file
                for batch in reader:
                    if batch.num_rows == 0:
                        continue
                    typed, stats = type_batch(batch, info.filename, row, delta)
                    row += batch.num_rows
                    if writer is None:
                        writer = parquet.ParquetWriter(target, typed.schema, compression="zstd")
                    writer.write_table(typed, row_group_size=256_000)
                    for key in ("rows", "bad_money", "bad_dates", "blank_keys"):
                        member[key] += stats[key]
                    value = pa.scalar(stats["obligation_sum"]).cast(MONEY).as_py()
                    total = value if total is None else total + value
            if writer is not None:
                writer.close()
            member["obligation_sum"] = str(total) if total is not None else "0"
            member["parquet_bytes"] = target.stat().st_size if target.exists() else 0
            report["members"].append(member)
    report["rows"] = sum(m["rows"] for m in report["members"])
    report["bad_money"] = sum(m["bad_money"] for m in report["members"])
    report["bad_dates"] = sum(m["bad_dates"] for m in report["members"])
    report["blank_keys"] = sum(m["blank_keys"] for m in report["members"])
    report["parquet_bytes"] = sum(m["parquet_bytes"] for m in report["members"])
    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    (out_dir / "_extract.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path")
    parser.add_argument("out_dir")
    args = parser.parse_args()
    report = extract(args.zip_path, args.out_dir)
    print(json.dumps({k: v for k, v in report.items() if k != "members"}, indent=2))
    # Unparseable values are kept as null and counted, never turned into zero. A handful are
    # reported and carried into the summary checks; more than 1 in 10,000 rows stops the run.
    bad = report["bad_money"] + report["bad_dates"]
    if report["blank_keys"] or bad > report["rows"] / 10_000:
        raise SystemExit("Typed extraction found too many unparseable values; see _extract.json")


if __name__ == "__main__":
    main()
