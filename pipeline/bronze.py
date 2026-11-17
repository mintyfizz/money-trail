"""Convert a verified local ZIP into bounded, lossless-text Parquet batches.

No CSV extraction, network reads, type inference, or source deletion. Curated
financial types and business-rule validation belong to the next modeling step.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
from pathlib import Path
import time
import zipfile

import pyarrow as pa
import pyarrow.csv as arrow_csv
import pyarrow.parquet as parquet

from ingest.archive import GIB, MIB, ensure_space, exclusive_lock, now, sha256, verified_receipt


class Budget:
    def __init__(self, directory, limit, reserve):
        self.directory, self.limit, self.reserve = directory, limit, reserve
        self.written = 0

    def check(self, size):
        if self.written + size > self.limit:
            raise RuntimeError("Parquet/reject output cap reached; incomplete staging retained")
        ensure_space(self.directory, size, self.reserve)


class LimitedWriter(io.BufferedIOBase):
    """Guard every write, including Arrow metadata/footer writes, against disk limits."""
    def __init__(self, path, budget):
        self.stream = Path(path).open("xb")
        self.budget = budget

    def writable(self):
        return True

    def tell(self):
        return self.stream.tell()

    def write(self, data):
        self.budget.check(len(data))
        count = self.stream.write(data)
        self.budget.written += count
        return count

    def flush(self):
        if not self.stream.closed:
            self.stream.flush()

    def close(self):
        if not self.closed:
            try:
                super().close()
            finally:
                self.stream.close()


def check_existing(directory, raw_digest):
    result = json.loads((directory / "manifest.json").read_text())
    if result.get("status") != "complete" or result["source_sha256"] != raw_digest:
        raise RuntimeError("Existing output does not match this verified source")
    for artifact in result["artifacts"]:
        path = directory / artifact["file"]
        if (Path(artifact["file"]).name != artifact["file"]
                or path.stat().st_size != artifact["bytes"] or sha256(path) != artifact["sha256"]):
            raise RuntimeError("Existing Parquet/reject artifact differs from its manifest")
    return result


def convert(path, output_root, reserve_gib=8, max_output_gib=2, block_mib=4, part_mib=128):
    values = (reserve_gib, max_output_gib, block_mib, part_mib)
    if not all(math.isfinite(value) for value in values) or reserve_gib < 0 or min(values[1:]) <= 0:
        raise ValueError("Invalid storage or batch limits")
    path, root = Path(path), Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(root / (path.stem + ".lock")):
        receipt = verified_receipt(path)
        final = root / path.stem
        if final.exists():
            return check_existing(final, receipt["sha256"])
        staging = root / ("." + path.stem + ".staging")
        staging.mkdir()  # A previous failed run requires inspection, never blind overwrite.
        return _convert(path, final, staging, receipt, reserve_gib, max_output_gib, block_mib, part_mib)


def _convert(path, final, staging, receipt, reserve_gib, max_output_gib, block_mib, part_mib):
    started = time.monotonic()
    budget = Budget(staging, int(max_output_gib * GIB), int(reserve_gib * GIB))
    result = {"status": "incomplete", "source_file": path.name, "source_url": receipt["url"],
              "source_sha256": receipt["sha256"], "created_at": now(), "pyarrow_version": pa.__version__,
              "schema_policy": "All source columns preserved as strings; no inferred types or null replacement",
              "scope": "Bronze preservation only; no business-rule filtering, reconciliation or deduplication",
              "members": [], "artifacts": []}
    rejects_path = staging / "rejects.jsonl"
    with LimitedWriter(rejects_path, budget) as rejects, zipfile.ZipFile(path) as archive:
        csv_members = [item for item in archive.infolist() if item.filename.lower().endswith(".csv")]
        reference_header = None
        if not csv_members:
            raise ValueError("No CSV members")
        for member_index, info in enumerate(csv_members):
            with archive.open(info) as stream:
                header = next(csv.reader(io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")))
            if not header or any(not name for name in header) or len(header) != len(set(header)):
                raise ValueError("Missing or duplicate source column names")
            if reference_header is not None and header != reference_header:
                raise ValueError("Schema drift between CSV members; incomplete staging retained")
            reference_header = header
            member = {"file": info.filename, "columns": len(header), "valid_rows": 0, "rejected_rows": 0}

            def reject(row):
                rejection = {"source_member": info.filename, "source_row_number": row.number,
                             "expected_columns": row.expected_columns, "actual_columns": row.actual_columns,
                             "raw_record": row.text, "reason": "column_count_mismatch"}
                rejects.write((json.dumps(rejection, ensure_ascii=False) + "\n").encode())
                member["rejected_rows"] += 1
                return "skip"

            with archive.open(info) as stream, arrow_csv.open_csv(
                stream,
                read_options=arrow_csv.ReadOptions(block_size=max(1, int(block_mib * MIB)), use_threads=False),
                parse_options=arrow_csv.ParseOptions(newlines_in_values=True, ignore_empty_lines=False,
                                                     invalid_row_handler=reject),
                convert_options=arrow_csv.ConvertOptions(column_types={name: pa.string() for name in header},
                                                         strings_can_be_null=False),
            ) as reader:
                if reader.schema.names != header:
                    raise ValueError("CSV header differs between parsers")
                schema = reader.schema.with_metadata({b"source_sha256": receipt["sha256"].encode(),
                                                      b"source_member": info.filename.encode()})
                part = 0
                # Each output part is closed/verified before the next; one batch is kept in memory.
                sink = writer = None
                rows_in_part = 0

                def finish_part():
                    nonlocal sink, writer, rows_in_part
                    if writer is None:
                        return
                    writer.close()
                    writer = None
                    sink.close()
                    sink = None
                    artifact = staging / f"member-{member_index:04d}-part-{part:05d}.parquet"
                    if parquet.read_metadata(artifact).num_rows != rows_in_part:
                        raise RuntimeError("Parquet row count differs from written batches")
                    result["artifacts"].append({"file": artifact.name, "bytes": artifact.stat().st_size,
                                                "sha256": sha256(artifact), "rows": rows_in_part})
                    rows_in_part = 0

                try:
                    for batch in reader:
                        if writer is not None and sink.tell() >= part_mib * MIB:
                            finish_part()
                            part += 1
                        if writer is None:
                            sink = LimitedWriter(staging / f"member-{member_index:04d}-part-{part:05d}.parquet", budget)
                            writer = parquet.ParquetWriter(sink, schema, compression="zstd", use_dictionary=True)
                        writer.write_batch(batch.replace_schema_metadata(schema.metadata))
                        member["valid_rows"] += batch.num_rows
                        rows_in_part += batch.num_rows
                    # A header-only CSV gets an explicit zero-row Parquet file retaining its schema.
                    if writer is None:
                        sink = LimitedWriter(staging / f"member-{member_index:04d}-part-{part:05d}.parquet", budget)
                        writer = parquet.ParquetWriter(sink, schema, compression="zstd")
                    finish_part()
                finally:
                    try:
                        if writer is not None:
                            writer.close()
                    finally:
                        if sink is not None:
                            sink.close()
            member["parsed_records"] = member["valid_rows"] + member["rejected_rows"]
            result["members"].append(member)
    result["artifacts"].append({"file": rejects_path.name, "bytes": rejects_path.stat().st_size,
                                "sha256": sha256(rejects_path)})
    result["valid_rows"] = sum(member["valid_rows"] for member in result["members"])
    result["rejected_rows"] = sum(member["rejected_rows"] for member in result["members"])
    result["status"] = "complete"
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["output_bytes_excluding_manifest"] = budget.written
    with LimitedWriter(staging / "manifest.json", budget) as output:
        output.write((json.dumps(result, indent=2) + "\n").encode())
    staging.rename(final)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("data/bronze"))
    parser.add_argument("--reserve-gib", type=float, default=8)
    parser.add_argument("--max-output-gib", type=float, default=2)
    parser.add_argument("--block-mib", type=float, default=4)
    args = parser.parse_args()
    result = convert(args.archive, args.output_root, args.reserve_gib, args.max_output_gib, args.block_mib)
    print(json.dumps({key: value for key, value in result.items() if key not in ("members", "artifacts")}, indent=2))


if __name__ == "__main__":
    main()
