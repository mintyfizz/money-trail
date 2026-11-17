"""Discover, download and profile official USAspending archives without extracting CSVs.

Requires Python 3.11+ and curl. TLS certificate verification remains enabled.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
from itertools import islice
import json
from pathlib import Path
import shutil
import subprocess
import time
from urllib.parse import urlparse
import zipfile

API = "https://api.usaspending.gov/api/v2/bulk_download/list_monthly_files/"
GIB = 1024**3


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def curl(*args, timeout=120):
    return subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--location",
         "--proto", "=https", "--proto-redir", "=https", "--retry", "2", "--retry-all-errors",
         "--retry-max-time", "60",
         "--connect-timeout", "20", "--max-time", str(timeout), *map(str, args)],
        check=True, capture_output=True,
    ).stdout


def archive_url(url):
    parts = urlparse(url)
    if (parts.scheme != "https" or parts.hostname != "files.usaspending.gov"
            or parts.username or parts.password or parts.port not in (None, 443)
            or not parts.path.startswith("/award_data_archive/")
            or not parts.path.endswith(".zip")):
        raise ValueError("Expected an official USAspending award archive URL")
    return url


def head(url):
    raw = curl("--head", archive_url(url)).decode()
    blocks = [block for block in raw.replace("\r\n", "\n").split("\n\n")
              if block.startswith("HTTP/")]
    headers = dict(line.split(":", 1) for line in blocks[-1].splitlines()[1:] if ":" in line)
    headers = {key.lower(): value.strip() for key, value in headers.items()}
    size = int(headers["content-length"])
    if size <= 0:
        raise ValueError("Archive has no positive content length")
    return {"bytes": size, "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified")}


def discover(years, agency="all", output=None):
    records = []
    for year in years:
        for kind in ("contracts", "assistance"):
            payload = {"agency": agency, "fiscal_year": year, "type": kind}
            result = json.loads(curl("-H", "Content-Type: application/json",
                                     "--data", json.dumps(payload), API))
            matches = [r for r in result["monthly_files"]
                       if r["fiscal_year"] == year and r["type"] == kind
                       and "_Full_" in r["file_name"]]
            if len(matches) != 1:
                raise ValueError(f"Expected exactly one full file: {payload}, got {len(matches)}")
            record = matches[0]
            record["http"] = head(record["url"])
            record["discovered_at"] = now()
            record["delta_files"] = [r for r in result["monthly_files"] if "_Delta_" in r["file_name"]]
            records.append(record)
            if output:
                save_json(output, {"api": API, "retrieved_at": now(), "agency": agency,
                                   "complete": False, "files": records})
            print(f"FY{year} {kind}: {record['http']['bytes']:,} bytes", flush=True)
    return {"api": API, "retrieved_at": now(), "agency": agency, "complete": True, "files": records}


def ensure_space(directory, additional_bytes, reserve_bytes):
    free = shutil.disk_usage(directory).free
    if free - additional_bytes < reserve_bytes:
        raise RuntimeError(f"Insufficient disk space: {free / GIB:.2f} GiB free; "
                           f"need {additional_bytes / GIB:.2f} GiB plus {reserve_bytes / GIB:.2f} GiB reserve")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(record, directory, reserve_gib=8):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    url = archive_url(record["url"])
    name = record["file_name"]
    if Path(name).name != name or name != Path(urlparse(url).path).name:
        raise ValueError("Unsafe or inconsistent archive filename")
    target = directory / name
    receipt_path = directory / (name + ".receipt.json")
    expected = record["http"]
    if target.exists():
        if not receipt_path.exists():
            raise RuntimeError(f"Existing file has no download receipt: {target}")
        receipt = json.loads(receipt_path.read_text())
        if (target.stat().st_size != expected["bytes"]
                or receipt["url"] != url or receipt["etag"] != expected.get("etag")
                or sha256(target) != receipt["sha256"]):
            raise RuntimeError(f"Existing file does not match manifest/receipt: {target}")
        return target
    if head(url) != expected:
        raise RuntimeError("Source metadata changed; rediscover before downloading")
    ensure_space(directory, expected["bytes"], int(reserve_gib * GIB))
    partial = target.with_suffix(".zip.partial")
    if partial.exists():
        raise RuntimeError(f"Incomplete previous transfer retained at {partial}; inspect before retry")
    print(f"Downloading {name}", flush=True)
    started = time.monotonic()
    arguments = ["--output", partial, "--max-filesize", expected["bytes"]]
    if expected.get("etag"):
        arguments += ["-H", "If-Match: " + expected["etag"]]
    curl(*arguments, url, timeout=1200)
    if partial.stat().st_size != expected["bytes"]:
        raise RuntimeError("Downloaded size differs from manifest; partial file retained")
    with zipfile.ZipFile(partial) as archive:
        if not archive.infolist() or not any(i.filename.lower().endswith(".csv") for i in archive.infolist()):
            raise RuntimeError("Archive does not contain CSV data")
    digest = sha256(partial)
    partial.replace(target)
    receipt = {"url": url, **expected, "sha256": digest,
               "retrieved_at": now(), "elapsed_seconds": round(time.monotonic() - started, 2),
               "zip_crc_status": "pending complete profile read"}
    save_json(receipt_path, receipt)
    print(f"Saved {name}: SHA-256 {digest}", flush=True)
    return target


def profile_csv(stream, filename, max_rows=None):
    csv.field_size_limit(16 * 1024 * 1024)
    reader = csv.reader(io.TextIOWrapper(stream, encoding="utf-8-sig", newline=""), strict=True)
    header = next(reader)
    if len(header) != len(set(header)):
        raise ValueError(f"Duplicate column names in {filename}")
    columns = {name: index for index, name in enumerate(header)}
    tracked = [name for name in ("action_date_fiscal_year", "assistance_type_code", "award_type_code",
                "correction_delete_ind", "correction_delete_indicator_code") if name in columns]
    counters = {name: Counter() for name in tracked}
    checks = [name for name in ("contract_transaction_unique_key", "assistance_transaction_unique_key",
              "recipient_uei", "primary_place_of_performance_state_code",
              "prime_award_transaction_place_of_performance_county_fips_code") if name in columns]
    missing = Counter({name: 0 for name in checks})
    rows = valid = width_errors = invalid_amounts = empty_amounts = negative = 0
    amount_total = Decimal("0")
    amount_by_type = Counter()
    invalid_examples = []
    money_index = columns.get("federal_action_obligation")
    type_index = columns.get("assistance_type_code", columns.get("award_type_code"))
    records = reader if max_rows is None else islice(reader, max_rows)
    for rows, row in enumerate(records, 1):
        if len(row) != len(header):
            width_errors += 1
            if len(invalid_examples) < 20:
                invalid_examples.append({"record": rows, "columns": len(row)})
            continue
        valid += 1
        for name in tracked:
            counters[name][row[columns[name]]] += 1
        for name in checks:
            if not row[columns[name]].strip():
                missing[name] += 1
        if money_index is not None:
            raw = row[money_index].strip()
            if not raw:
                empty_amounts += 1
            else:
                try:
                    amount = Decimal(raw)
                    if not amount.is_finite():
                        raise InvalidOperation
                    amount_total += amount
                    if type_index is not None:
                        amount_by_type[row[type_index]] += amount
                    negative += amount < 0
                except InvalidOperation:
                    invalid_amounts += 1
        if rows % 1000000 == 0:
            print(f"{filename}: {rows:,} records read", flush=True)
    return {"member": filename, "rows": rows, "record_limit": max_rows, "valid_width_rows": valid,
            "columns": len(header), "header": header, "width_errors": width_errors,
            "width_error_examples": invalid_examples, "missing_counts": dict(missing),
            "counts": {key: dict(value) for key, value in counters.items()},
            "federal_action_obligation": {"sum": str(amount_total) if money_index is not None else None,
              "negative_rows": negative, "missing_rows": empty_amounts, "invalid_rows": invalid_amounts,
              "sum_by_type": {key: str(value) for key, value in amount_by_type.items()},
              "meaning": "Raw field sum over valid-width parsed records, not an official reconciled total. Assistance files include non-grants and loans."}}


def profile(path):
    path = Path(path)
    started = time.monotonic()
    members = []
    with zipfile.ZipFile(path) as archive:
        csvs = [info for info in archive.infolist() if info.filename.lower().endswith(".csv")]
        if not csvs:
            raise ValueError("No CSV members")
        uncompressed = sum(info.file_size for info in archive.infolist())
        for info in archive.infolist():
            if info.is_dir():
                continue
            with archive.open(info) as stream:
                if info in csvs:
                    result = profile_csv(stream, info.filename)
                    result["uncompressed_bytes"] = info.file_size
                    result["compressed_bytes"] = info.compress_size
                    members.append(result)
                else:
                    while stream.read(1024 * 1024):
                        pass
    return {"file_name": path.name, "profiled_at": now(), "compressed_bytes": path.stat().st_size,
            "uncompressed_bytes": uncompressed, "sha256": sha256(path),
            "zip_crc_status": "all members read to EOF; CRC validated by zipfile",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "rows": sum(member["rows"] for member in members), "members": members,
            "not_checked": ["transaction-key uniqueness", "reconciliation to official totals",
                            "business-rule validity beyond the reported checks"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    discover_cmd = commands.add_parser("discover")
    discover_cmd.add_argument("--years", type=int, nargs="+", default=[2025])
    discover_cmd.add_argument("--agency", default="all")
    discover_cmd.add_argument("--output", type=Path, required=True)
    download_cmd = commands.add_parser("download")
    download_cmd.add_argument("--manifest", type=Path, required=True)
    download_cmd.add_argument("--directory", type=Path, default=Path("data/raw"))
    download_cmd.add_argument("--reserve-gib", type=float, default=8)
    download_cmd.add_argument("--kind", choices=["contracts", "assistance"])
    profile_cmd = commands.add_parser("profile")
    profile_cmd.add_argument("archive", type=Path)
    profile_cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "discover":
        agency = args.agency if args.agency == "all" else int(args.agency)
        save_json(args.output, discover(args.years, agency, args.output))
    elif args.command == "download":
        records = json.loads(args.manifest.read_text())["files"]
        selected = [r for r in records if not args.kind or r["type"] == args.kind]
        if not selected:
            raise ValueError("No matching archives in manifest")
        args.directory.mkdir(parents=True, exist_ok=True)
        required = sum(r["http"]["bytes"] for r in selected if not (args.directory / r["file_name"]).exists())
        ensure_space(args.directory, required, int(args.reserve_gib * GIB))
        for record in selected:
            download(record, args.directory, args.reserve_gib)
    else:
        result = profile(args.archive)
        save_json(args.output, result)
        print(json.dumps({k: v for k, v in result.items() if k != "members"}, indent=2))


if __name__ == "__main__":
    main()
