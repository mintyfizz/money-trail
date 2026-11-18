"""Stream a USAspending award archive into a typed transaction table, one Parquet file per CSV member.

Works for both kinds of archive (contracts and financial assistance); each kind declares its own
column contract in `pipeline.kinds`. The national archives are too large to keep as a full text
copy on a runner, so this step keeps only the columns the analysis needs. The original ZIP stays in
a release as the raw layer. Every row keeps its source file and row number, and the original
string of every money and date column, so a typed value can always be traced back.
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

from pipeline.guard import GuardedFile
from pipeline.kinds import CONTRACTS, KINDS, Kind

COLUMNS = CONTRACTS.columns  # kept for callers that predate assistance support
DELTA_COLUMN = "correction_delete_ind"
# Treasury's newer delta generator names the column differently; both are accepted, nothing else.
DELTA_ALIASES = ("correction_delete_ind", "correction_delete_indicator")
DELTA_VALUES = {"", "C", "D"}
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
    """Strict calendar dates: a parsed date must format back to exactly the source text.

    strptime alone normalises impossible dates (2025-02-30 becomes 2025-03-02); the round trip
    rejects them. Rejected dates become null and are counted.
    """
    stripped = pc.utf8_trim_whitespace(values)
    blank = pc.equal(stripped, "")
    cleaned = pc.if_else(blank, pa.scalar(None, pa.string()), stripped)
    parsed = pc.strptime(cleaned, format="%Y-%m-%d", unit="s", error_is_null=True)
    round_trip = pc.strftime(parsed, format="%Y-%m-%d")
    exact = pc.fill_null(pc.equal(round_trip, cleaned), False)
    valid = pc.and_(pc.is_valid(cleaned), exact)
    bad = pc.sum(pc.and_(pc.is_valid(cleaned), pc.invert(exact))).as_py() or 0
    dates = pc.if_else(valid, pc.cast(parsed, pa.date32()), pa.scalar(None, pa.date32()))
    return dates, bad


def type_batch(batch: pa.RecordBatch, source_file: str, first_row: int, delta: str | None,
               kind: Kind = CONTRACTS) -> tuple[pa.Table, dict]:
    table = pa.Table.from_batches([batch])
    bad_indicators = 0
    if delta:
        indicator = pc.utf8_upper(pc.utf8_trim_whitespace(table[delta].combine_chunks()))
        bad_indicators = pc.sum(pc.invert(pc.is_in(indicator, pa.array(sorted(DELTA_VALUES))))).as_py() or 0
        table = table.drop_columns([delta]).append_column(DELTA_COLUMN, indicator)
    columns = {name: table[name] for name in table.column_names}
    money_cols = [c for c in kind.money if c in columns]
    date_cols = [c for c in kind.dates if c in columns]
    bad_money = bad_dates = 0
    for name in money_cols:
        typed_money, bad = to_money(columns[name].combine_chunks())
        columns[f"{name}_raw"] = columns[name]
        columns[name] = typed_money
        bad_money += bad
    for name in date_cols:
        typed_date, bad = to_date(columns[name].combine_chunks())
        columns[f"{name}_raw"] = columns[name]
        columns[name] = typed_date
        bad_dates += bad
    columns["source_file"] = pa.array([source_file] * table.num_rows, pa.string())
    columns["source_row"] = pa.array(range(first_row, first_row + table.num_rows), pa.int64())
    order = [kind.key] + money_cols + date_cols
    order += [c for c in table.column_names if c not in order and c != DELTA_COLUMN]
    order += [f"{c}_raw" for c in money_cols + date_cols]
    order += ([DELTA_COLUMN] if delta else []) + ["source_file", "source_row"]
    typed = pa.table({name: columns[name] for name in order})
    keys = table[kind.key].combine_chunks()
    blank_keys = pc.sum(pc.equal(pc.utf8_trim_whitespace(keys), "")).as_py() or 0
    total = pc.sum(columns["federal_action_obligation"]).as_py()
    stats = {"rows": table.num_rows, "bad_money": bad_money, "bad_dates": bad_dates, "blank_keys": blank_keys,
             "bad_indicators": bad_indicators,
             "obligation_sum": str(total) if total is not None else "0"}
    return typed, stats


def extract(zip_path: str | Path, out_dir: str | Path, block_mib: int = 16, reserve_gib: float = 8,
            kind: str | Kind = "contracts") -> dict:
    """Write one typed Parquet file per CSV member. Never extracts the CSV to disk.

    Every byte written to Parquet, footer included, goes through a writer that checks the free-space
    reserve first; crossing it stops the run.
    """
    kind = KINDS[kind] if isinstance(kind, str) else kind
    zip_path, out_dir = Path(zip_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = {"source_zip": zip_path.name, "kind": kind.name, "members": [], "columns": list(kind.columns)}
    with zipfile.ZipFile(zip_path) as archive:
        members = sorted((i for i in archive.infolist() if i.filename.lower().endswith(".csv")),
                         key=lambda i: i.filename)
        if not members:
            raise ValueError(f"{zip_path.name} contains no CSV members")
        for index, info in enumerate(members):
            header = read_header(archive, info)
            present = [name for name in DELTA_ALIASES if name in header]
            if len(present) > 1:
                raise ValueError(f"{info.filename} has more than one correction/delete column: {present}")
            delta = present[0] if present else None
            missing = [c for c in kind.columns if c not in header]
            if missing:
                raise ValueError(f"{info.filename} is missing expected {kind.name} columns: {missing}")
            optional = [c for c in kind.optional if c in header and c not in kind.columns]
            wanted = list(kind.columns) + optional + ([delta] if delta else [])
            target = out_dir / f"part-{index:04d}.parquet"
            member = {"source_file": info.filename, "output": target.name, "rows": 0, "bad_money": 0,
                      "bad_dates": 0, "blank_keys": 0, "bad_indicators": 0, "obligation_sum": "0",
                      "delta_column": delta, "optional_columns": optional,
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
                    typed, stats = type_batch(batch, info.filename, row, delta, kind)
                    row += batch.num_rows
                    if writer is None:
                        sink = GuardedFile(target, reserve_gib)
                        writer = parquet.ParquetWriter(sink, typed.schema, compression="zstd")
                    writer.write_table(typed, row_group_size=256_000)
                    for key in ("rows", "bad_money", "bad_dates", "blank_keys", "bad_indicators"):
                        member[key] += stats[key]
                    value = pa.scalar(stats["obligation_sum"]).cast(MONEY).as_py()
                    total = value if total is None else total + value
            if writer is not None:
                writer.close()
                sink.close()
            member["obligation_sum"] = str(total) if total is not None else "0"
            member["parquet_bytes"] = target.stat().st_size if target.exists() else 0
            report["members"].append(member)
    # Optional columns are tracked per member: a column counts as present only if every member has it.
    by_member = [set(m["optional_columns"]) for m in report["members"]]
    report["optional_columns_present"] = sorted(set.intersection(*by_member))
    report["optional_columns_consistent"] = all(b == by_member[0] for b in by_member)
    report["rows"] = sum(m["rows"] for m in report["members"])
    report["bad_money"] = sum(m["bad_money"] for m in report["members"])
    report["bad_dates"] = sum(m["bad_dates"] for m in report["members"])
    report["blank_keys"] = sum(m["blank_keys"] for m in report["members"])
    report["bad_indicators"] = sum(m["bad_indicators"] for m in report["members"])
    report["parquet_bytes"] = sum(m["parquet_bytes"] for m in report["members"])
    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    (out_dir / "_extract.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path")
    parser.add_argument("out_dir")
    parser.add_argument("--kind", choices=sorted(KINDS), default="contracts")
    parser.add_argument("--reserve-gib", type=float, default=8)
    args = parser.parse_args()
    report = extract(args.zip_path, args.out_dir, reserve_gib=args.reserve_gib, kind=args.kind)
    print(json.dumps({k: v for k, v in report.items() if k != "members"}, indent=2))
    # Unparseable values are kept as null and counted, never turned into zero, and any of them
    # stops the run: one omitted obligation can dominate a total. Blank values are not errors.
    if report["blank_keys"] or report["bad_money"] or report["bad_dates"] or report["bad_indicators"]:
        raise SystemExit("Typed extraction found unparseable values; see _extract.json")


if __name__ == "__main__":
    main()
