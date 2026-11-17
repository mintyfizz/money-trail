"""Preserve verified ZIP CSV fields as text Parquet, with independent read-back checks."""
from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import io
import json
import math
import os
from pathlib import Path
import time
import zipfile

import pyarrow as pa
import pyarrow.csv as arrow_csv
import pyarrow.parquet as parquet

from ingest.archive import GIB, MIB, ensure_space, exclusive_lock, now, sha256, verified_receipt

PROVENANCE = ("_mt_source_zip", "_mt_source_file", "_mt_source_row_number")
FORMAT_VERSION = 2
MAX_RECORD_CHARS = 16 * MIB


def digest_record(digest, value):
    digest.update((json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
            os.fsync(self.stream.fileno())

    def close(self):
        if not self.closed:
            try:
                super().close()
            finally:
                self.stream.close()


class CapturedLines:
    """Keep only the current logical CSV record, including its original line endings."""
    def __init__(self, stream):
        self.stream = stream
        self.lines = []
        self.characters = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline(MAX_RECORD_CHARS + 1)
        if not line:
            raise StopIteration
        self.characters += len(line)
        if self.characters > MAX_RECORD_CHARS:
            raise ValueError("CSV record exceeds the safety limit; retain ZIP for inspection")
        self.lines.append(line)
        return line

    def take(self):
        text = "".join(self.lines)
        self.lines.clear()
        self.characters = 0
        return text


def without_terminator(text):
    if text.endswith("\r\n"):
        return text[:-2]
    return text[:-1] if text.endswith(("\r", "\n")) else text


def independent_rows(archive, info, header, member, rejects, expected_digest, rejection_digest, zip_name):
    """A separate strict CSV parser counts records and supplies stable logical row numbers."""
    csv.field_size_limit(MAX_RECORD_CHARS)
    with archive.open(info) as stream, io.TextIOWrapper(stream, encoding="utf-8-sig", newline="") as text:
        captured = CapturedLines(text)
        reader = csv.reader(captured, strict=True)
        if next(reader) != header:
            raise ValueError("CSV header changed during audit")
        captured.take()
        for number, row in enumerate(reader, start=2):
            raw = captured.take()
            member["csv_rows"] += 1
            if len(row) != len(header):
                rejection = {"source_zip": zip_name, "source_member": info.filename,
                             "source_row_number": number, "expected_columns": len(header),
                             "actual_columns": len(row), "raw_record": raw,
                             "reason": "column_count_mismatch"}
                rejects.write((json.dumps(rejection, ensure_ascii=False) + "\n").encode())
                digest_record(rejection_digest, [len(header), len(row), without_terminator(raw)])
                member["rejected_rows"] += 1
            else:
                digest_record(expected_digest, row + [zip_name, info.filename, number])
                yield number


def verify_readback(directory, artifacts, schema, expected_digest, expected_rows):
    actual_digest = hashlib.sha256()
    count = 0
    for artifact in artifacts:
        with parquet.ParquetFile(directory / artifact["file"]) as source:
            if not source.schema_arrow.equals(schema, check_metadata=True):
                raise RuntimeError("Parquet schema differs from preserved source schema")
            for batch in source.iter_batches(batch_size=1024, use_threads=False):
                for row in zip(*(column.to_pylist() for column in batch.columns)):
                    digest_record(actual_digest, row)
                    count += 1
    if count != expected_rows or actual_digest.hexdigest() != expected_digest:
        raise RuntimeError("Parquet read-back differs from independent CSV values or row count")
    return actual_digest.hexdigest()


def check_existing(directory, raw_digest):
    directory = Path(directory)
    result = json.loads((directory / "manifest.json").read_text())
    if (result.get("status") != "complete" or result["source_sha256"] != raw_digest
            or result.get("format_version") != FORMAT_VERSION
            or result.get("verification", {}).get("status") != "passed"):
        raise RuntimeError("Existing output lacks current independent validation; retain/rebuild from ZIP")
    names = [artifact["file"] for artifact in result["artifacts"]]
    if len(names) != len(set(names)) or not names:
        raise RuntimeError("Invalid artifact inventory")
    for artifact in result["artifacts"]:
        path = directory / artifact["file"]
        if (Path(artifact["file"]).name != artifact["file"] or path.is_symlink()
                or path.stat().st_size != artifact["bytes"] or sha256(path) != artifact["sha256"]):
            raise RuntimeError("Existing Parquet/reject artifact differs from its manifest")
    if (result["valid_rows"] + result["rejected_rows"] != result["csv_rows"]
            or sum(a.get("rows", 0) for a in result["artifacts"]) != result["valid_rows"]
            or any(m["valid_rows"] + m["rejected_rows"] != m["csv_rows"] for m in result["members"])
            or any(sum(m[key] for m in result["members"]) != result[key]
                   for key in ("valid_rows", "rejected_rows", "csv_rows"))):
        raise RuntimeError("Independent CSV row reconciliation failed")
    return result


def verify_rejects(path, members, zip_name):
    states = {m["file"]: {"count": 0, "last_row": 1, "digest": hashlib.sha256()} for m in members}
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            state = states[row["source_member"]]
            if (row["source_zip"] != zip_name or row["source_row_number"] <= state["last_row"]
                    or row["expected_columns"] == row["actual_columns"]):
                raise RuntimeError("Rejection ledger provenance is invalid")
            state["last_row"] = row["source_row_number"]
            state["count"] += 1
            digest_record(state["digest"], [row["expected_columns"], row["actual_columns"],
                                             without_terminator(row["raw_record"])])
    for member in members:
        state = states[member["file"]]
        if state["count"] != member["rejected_rows"] or state["digest"].hexdigest() != member["rejected_records_sha256"]:
            raise RuntimeError("Rejection ledger differs from independent CSV records")


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
        staging.mkdir()
        return _convert(path, final, staging, receipt, reserve_gib, max_output_gib, block_mib, part_mib)


def _convert(path, final, staging, receipt, reserve_gib, max_output_gib, block_mib, part_mib):
    started = time.monotonic()
    budget = Budget(staging, int(max_output_gib * GIB), int(reserve_gib * GIB))
    snapshot = receipt.get("snapshot_date")
    if snapshot is not None:
        snapshot = date.fromisoformat(snapshot).isoformat()
    result = {"format_version": FORMAT_VERSION, "status": "incomplete", "source_file": path.name,
              "source_url": receipt["url"], "source_sha256": receipt["sha256"], "source_bytes": receipt["bytes"],
              "snapshot_date": snapshot, "source_receipt": receipt, "created_at": now(),
              "pyarrow_version": pa.__version__,
              "schema_policy": "Source fields preserved as UTF-8 strings; CSV quoting is decoded, no cleaning or null replacement",
              "provenance_columns": list(PROVENANCE),
              "row_number_policy": "1-based logical CSV record including header at 1; embedded newlines do not increment it",
              "scope": "Raw preservation only; typing/filtering/modeling read this layer separately",
              "members": [], "artifacts": []}
    rejects_path = staging / "rejects.jsonl"
    with LimitedWriter(rejects_path, budget) as rejects, zipfile.ZipFile(path) as archive:
        csv_members = [item for item in archive.infolist() if item.filename.lower().endswith(".csv") and not item.is_dir()]
        result["unpreserved_members"] = [item.filename for item in archive.infolist()
                                          if not item.is_dir() and item not in csv_members]
        reference_header = None
        if not csv_members:
            raise ValueError("No CSV members")
        for member_index, info in enumerate(csv_members):
            with archive.open(info) as stream:
                header = next(csv.reader(io.TextIOWrapper(stream, encoding="utf-8-sig", newline=""), strict=True))
            if not header or any(not name for name in header) or len(header) != len(set(header)):
                raise ValueError("Missing or duplicate source column names")
            if set(header).intersection(PROVENANCE):
                raise ValueError("Source columns collide with provenance names; no columns overwritten")
            if reference_header is not None and header != reference_header:
                raise ValueError("Schema drift between CSV members; incomplete staging retained")
            reference_header = header
            member = {"file": info.filename, "source_columns": header, "columns": len(header),
                      "valid_rows": 0, "rejected_rows": 0, "csv_rows": 0}
            expected_digest, expected_rejects, arrow_rejects = (hashlib.sha256() for _ in range(3))
            arrow_rejected = 0

            def reject(row):
                nonlocal arrow_rejected
                digest_record(arrow_rejects, [row.expected_columns, row.actual_columns, row.text])
                arrow_rejected += 1
                return "skip"

            audit = independent_rows(archive, info, header, member, rejects, expected_digest, expected_rejects, path.name)
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
                schema = pa.schema(list(reader.schema) + [pa.field(PROVENANCE[0], pa.string()),
                                   pa.field(PROVENANCE[1], pa.string()), pa.field(PROVENANCE[2], pa.int64())],
                                   metadata={b"source_sha256": receipt["sha256"].encode(),
                                             b"source_member": info.filename.encode()})
                part = 0
                sink = writer = None
                rows_in_part = 0
                member_artifacts = []

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
                    entry = {"file": artifact.name, "bytes": artifact.stat().st_size,
                             "sha256": sha256(artifact), "rows": rows_in_part}
                    member_artifacts.append(entry)
                    result["artifacts"].append(entry)
                    rows_in_part = 0

                try:
                    for batch in reader:
                        numbers = []
                        for _ in range(batch.num_rows):
                            number = next(audit, None)
                            if number is None:
                                raise RuntimeError("Arrow has more rows than independent CSV parser")
                            numbers.append(number)
                        batch = pa.RecordBatch.from_arrays(list(batch.columns) + [
                            pa.array([path.name] * batch.num_rows), pa.array([info.filename] * batch.num_rows),
                            pa.array(numbers, type=pa.int64())], schema=schema)
                        if writer is not None and sink.tell() >= part_mib * MIB:
                            finish_part()
                            part += 1
                        if writer is None:
                            sink = LimitedWriter(staging / f"member-{member_index:04d}-part-{part:05d}.parquet", budget)
                            writer = parquet.ParquetWriter(sink, schema, compression="zstd", use_dictionary=True)
                        writer.write_batch(batch)
                        member["valid_rows"] += batch.num_rows
                        rows_in_part += batch.num_rows
                    if next(audit, None) is not None:
                        raise RuntimeError("Arrow has fewer rows than independent CSV parser")
                    if (arrow_rejected != member["rejected_rows"] or arrow_rejects.digest() != expected_rejects.digest()
                            or member["valid_rows"] + member["rejected_rows"] != member["csv_rows"]):
                        raise RuntimeError("CSV parsers disagree on records/rejections; ZIP must be retained")
                    if writer is None:
                        sink = LimitedWriter(staging / f"member-{member_index:04d}-part-{part:05d}.parquet", budget)
                        writer = parquet.ParquetWriter(sink, schema, compression="zstd")
                    finish_part()
                finally:
                    audit.close()
                    try:
                        if writer is not None:
                            writer.close()
                    finally:
                        if sink is not None:
                            sink.close()
            member["value_sha256"] = verify_readback(staging, member_artifacts, schema,
                                                     expected_digest.hexdigest(), member["valid_rows"])
            member["rejected_records_sha256"] = expected_rejects.hexdigest()
            result["members"].append(member)
    verify_rejects(rejects_path, result["members"], path.name)
    result["artifacts"].append({"file": rejects_path.name, "bytes": rejects_path.stat().st_size,
                                "sha256": sha256(rejects_path)})
    for key in ("valid_rows", "rejected_rows", "csv_rows"):
        result[key] = sum(member[key] for member in result["members"])
    result["verification"] = {"status": "passed", "method": "independent strict Python CSV + Arrow; full Parquet value/provenance read-back SHA-256; matching rejection records",
                              "verified_at": now()}
    result["status"] = "complete"
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["output_bytes_excluding_manifest"] = budget.written
    result["parquet_bytes"] = sum(a["bytes"] for a in result["artifacts"] if a["file"].endswith(".parquet"))
    result["parquet_to_zip_ratio"] = round(result["parquet_bytes"] / receipt["bytes"], 4)
    with LimitedWriter(staging / "manifest.json", budget) as output:
        output.write((json.dumps(result, indent=2) + "\n").encode())
    sync_directory(staging)
    staging.rename(final)
    sync_directory(final.parent)
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
