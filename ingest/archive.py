"""Discover, download and profile official USAspending archives without extracting CSVs.

Requires Python 3.11+ and curl. TLS certificate verification remains enabled.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
from itertools import islice
import json
import fcntl
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse
import zipfile

API = "https://api.usaspending.gov/api/v2/bulk_download/list_monthly_files/"
AGENCIES_API = "https://api.usaspending.gov/api/v2/bulk_download/list_agencies/"
GIB = 1024**3
MIB = 1024**2


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def curl(*args, timeout=30):
    return subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--location",
         "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "3",
         "--connect-timeout", "10", "--max-time", str(timeout), *map(str, args)],
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


def parse_headers(raw):
    blocks = [block for block in raw.replace("\r\n", "\n").split("\n\n")
              if block.startswith("HTTP/")]
    if not blocks:
        raise RuntimeError("No HTTP response headers received")
    status = int(blocks[-1].splitlines()[0].split()[1])
    headers = dict(line.split(":", 1) for line in blocks[-1].splitlines()[1:] if ":" in line)
    headers = {key.lower(): value.strip() for key, value in headers.items()}
    return status, headers


def head(url):
    status, headers = parse_headers(curl("--head", "-H", "Accept-Encoding: identity", archive_url(url)).decode())
    if status != 200:
        raise RuntimeError(f"Unexpected HEAD status: {status}")
    size = int(headers["content-length"])
    if size <= 0:
        raise ValueError("Archive has no positive content length")
    return {"bytes": size, "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified")}


def list_agencies():
    response = json.loads(curl("-H", "Content-Type: application/json", "--data",
                               json.dumps({"type": "award_agencies", "agency": 0}), AGENCIES_API))
    unique = {}
    for group in response["agencies"].values():
        for agency in group:
            unique[int(agency["toptier_agency_id"])] = agency
    if not unique:
        raise ValueError("Source returned no selectable agencies")
    return [unique[key] for key in sorted(unique)]


def discover(years, agency=None, output=None, agency_ids=None, kinds=("contracts", "assistance")):
    """Sequential, checkpointed discovery. No national/agency mixtures or coverage claim."""
    catalog = {"api": API, "retrieved_at": now(), "complete": False,
               "national_coverage_verified": False, "files": [], "checks": []}
    def checkpoint():
        if output:
            save_json(output, catalog)
    checkpoint()
    try:
        if agency is not None and agency_ids is not None:
            raise ValueError("Select an agency, agency IDs, or automatic per-agency discovery")
        if agency is not None:
            agencies = [{"toptier_agency_id": agency}]
        elif agency_ids is not None:
            agencies = [{"toptier_agency_id": key} for key in sorted(set(agency_ids))]
        else:
            agencies = list_agencies()
        catalog["agencies"] = agencies
        for selected in agencies:
            selected_id = selected["toptier_agency_id"]
            for year in years:
                for kind in kinds:
                    payload = {"agency": selected_id, "fiscal_year": year, "type": kind}
                    result = json.loads(curl("-H", "Content-Type: application/json",
                                             "--data", json.dumps(payload), API))
                    matches = [r for r in result["monthly_files"]
                               if r["fiscal_year"] == year and r["type"] == kind
                               and "_Full_" in r["file_name"]]
                    if len(matches) > 1:
                        raise ValueError(f"Ambiguous full archive for {payload}")
                    if not matches:
                        catalog["checks"].append({**payload, "status": "no_archive_returned"})
                        checkpoint()
                        continue
                    record = matches[0]
                    if selected_id != "all" and (record["agency_name"] == "All" or "_All_" in record["file_name"]):
                        raise ValueError("National archive returned for an agency request; refusing overlap")
                    record["agency_id"] = selected_id
                    record["http"] = head(record["url"])
                    record["discovered_at"] = now()
                    record["delta_files"] = [r for r in result["monthly_files"] if "_Delta_" in r["file_name"]]
                    if any(r["url"] == record["url"] for r in catalog["files"]):
                        raise ValueError("Same archive returned for multiple requested slices")
                    catalog["files"].append(record)
                    catalog["checks"].append({**payload, "status": "available"})
                    checkpoint()
                    print(f"{record['agency_name']} FY{year} {kind}: {record['http']['bytes']:,} bytes", flush=True)
        catalog["files"].sort(key=lambda item: item["http"]["bytes"])
        catalog["complete"] = True
    except Exception as error:
        catalog["error"] = str(error)
        checkpoint()
        raise
    checkpoint()
    return catalog


def ensure_space(directory, additional_bytes, reserve_bytes):
    if not math.isfinite(reserve_bytes) or reserve_bytes < 0 or additional_bytes < 0:
        raise ValueError("Space requirements must be finite and nonnegative")
    free = shutil.disk_usage(directory).free
    if free - additional_bytes < reserve_bytes:
        raise RuntimeError(f"Insufficient disk space: {free / GIB:.2f} GiB free; "
                           f"need {additional_bytes / GIB:.2f} GiB plus {reserve_bytes / GIB:.2f} GiB reserve")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def exclusive_lock(path):
    with Path(path).open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Another process is using {path}") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def verify_zip(path):
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [info.filename for info in members if not info.is_dir()]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP member names")
        if not any(name.lower().endswith(".csv") for name in names):
            raise ValueError("Archive does not contain CSV data")
        bad = archive.testzip()
        if bad:
            raise zipfile.BadZipFile(f"CRC failure: {bad}")
        return {"members": len(names), "uncompressed_bytes": sum(info.file_size for info in members)}


def verified_receipt(path):
    path = Path(path)
    if path.suffix != ".zip":
        raise ValueError("Only a completed .zip may be processed")
    receipt = json.loads(Path(str(path) + ".receipt.json").read_text())
    if (receipt.get("zip_crc_status") != "verified" or path.stat().st_size != receipt["bytes"]
            or sha256(path) != receipt["sha256"]):
        raise RuntimeError("Raw ZIP does not match a verified integrity receipt")
    return receipt


def fetch_range(url, start, end, etag, body, headers):
    """One bounded attempt. Never append a server response directly to the checkpoint."""
    Path(body).write_bytes(b"")
    Path(headers).write_bytes(b"")
    return subprocess.run([
        "curl", "--fail", "--silent", "--show-error", "--location", "--max-redirs", "3",
        "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "10", "--max-time", "120",
        "-H", "Accept-Encoding: identity", "-H", "If-Match: " + etag,
        "--range", f"{start}-{end}", "--max-filesize", str(end - start + 1),
        "--dump-header", str(headers), "--output", str(body), archive_url(url),
    ], capture_output=True)


class DownloadInterrupted(RuntimeError):
    pass


def download(record, directory, reserve_gib=8, chunk_mib=8):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if not math.isfinite(reserve_gib) or reserve_gib < 0 or not math.isfinite(chunk_mib) or chunk_mib <= 0:
        raise ValueError("Invalid disk reserve or chunk size")
    url = archive_url(record["url"])
    name = record["file_name"]
    if Path(name).name != name or name != Path(urlparse(url).path).name:
        raise ValueError("Unsafe or inconsistent archive filename")
    with exclusive_lock(directory / (name + ".lock")):
        return _download(record, directory, reserve_gib, max(1, int(chunk_mib * MIB)))


def _download(record, directory, reserve_gib, chunk_bytes):
    name, url, expected = record["file_name"], record["url"], record["http"]
    target = directory / name
    receipt_path = directory / (name + ".receipt.json")
    if target.exists():
        if not receipt_path.exists():
            raise RuntimeError(f"Existing file has no download receipt: {target}")
        receipt = json.loads(receipt_path.read_text())
        if (target.stat().st_size != expected["bytes"]
                or receipt["url"] != url or receipt["etag"] != expected.get("etag")
                or sha256(target) != receipt["sha256"]):
            raise RuntimeError(f"Existing file does not match manifest/receipt: {target}")
        if receipt.get("zip_crc_status") != "verified":
            receipt["zip"] = verify_zip(target)
            receipt["zip_crc_status"] = "verified"
            save_json(receipt_path, receipt)
        return target
    if head(url) != expected:
        raise RuntimeError("Source metadata changed; rediscover before downloading")
    etag = expected.get("etag")
    if not etag or not re.fullmatch(r'"[^"\r\n]+"', etag):
        raise RuntimeError("Safe resume requires a strong quoted ETag; refusing weak or missing identity")
    partial = directory / (name + ".part")
    state_path = directory / (name + ".part.json")
    body_path = directory / (name + ".part.chunk")
    headers_path = directory / (name + ".part.headers")
    identity = {"url": url, **expected}
    if partial.exists() != state_path.exists():
        raise RuntimeError("Partial file and resume metadata must both exist; old .partial files are not adopted")
    if partial.exists():
        state = json.loads(state_path.read_text())
        if (state["identity"] != identity or partial.stat().st_size != state["committed_bytes"]
                or sha256(partial) != state["prefix_sha256"]):
            raise RuntimeError("Partial bytes or source identity differ from the saved checkpoint")
    else:
        partial.touch(exist_ok=False)
        save_json(state_path, {"identity": identity, "committed_bytes": 0, "prefix_sha256": sha256(partial)})
    if partial.stat().st_size > expected["bytes"]:
        raise RuntimeError("Partial file exceeds expected archive size")
    digest = hashlib.sha256()
    with partial.open("rb") as stream:
        while block := stream.read(MIB):
            digest.update(block)
    started = time.monotonic()
    while (start := partial.stat().st_size) < expected["bytes"]:
        end = min(start + chunk_bytes, expected["bytes"]) - 1
        ensure_space(directory, 2 * (end - start + 1) + MIB, int(reserve_gib * GIB))
        print(f"{name}: requesting bytes {start:,}–{end:,}", flush=True)
        response = fetch_range(url, start, end, etag, body_path, headers_path)
        try:
            status, headers = parse_headers(headers_path.read_text())
        except RuntimeError:
            raise DownloadInterrupted(f"No usable response; {start:,} checkpointed bytes retained. "
                                      + response.stderr.decode(errors="replace").strip()) from None
        if (status != 206 or headers.get("content-range") != f"bytes {start}-{end}/{expected['bytes']}"
                or headers.get("etag") != etag or headers.get("content-encoding", "identity") != "identity"
                or ("content-length" in headers and int(headers["content-length"]) != end - start + 1)):
            raise RuntimeError(f"Unsafe range response (HTTP {status}); no bytes appended")
        received = body_path.stat().st_size
        if response.returncode not in (0, 18, 28, 56) or not 0 < received <= end - start + 1:
            raise DownloadInterrupted(f"Transfer failed; {start:,} checkpointed bytes retained")
        ensure_space(directory, received + MIB, int(reserve_gib * GIB))
        with partial.open("ab") as output, body_path.open("rb") as source:
            while block := source.read(MIB):
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        save_json(state_path, {"identity": identity, "committed_bytes": start + received,
                               "prefix_sha256": digest.hexdigest()})
        body_path.unlink()
        headers_path.unlink()
        if response.returncode or received != end - start + 1:
            raise DownloadInterrupted(f"Connection interrupted; {start + received:,} bytes saved. Rerun to resume.")
    zip_info = verify_zip(partial)
    receipt = {"url": url, **expected, "sha256": digest.hexdigest(),
               "retrieved_at": now(), "elapsed_seconds": round(time.monotonic() - started, 2),
               "zip_crc_status": "verified", "zip": zip_info}
    save_json(receipt_path, receipt)
    partial.replace(target)
    state_path.unlink()
    print(f"Verified {name}: SHA-256 {digest.hexdigest()}", flush=True)
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


def select_records(records, kind=None, limit=None):
    selected = [record for record in records if not kind or record["type"] == kind]
    if not selected:
        raise ValueError("No matching archives in manifest")
    if len({record["url"] for record in selected}) != len(selected):
        raise ValueError("Duplicate source archives in manifest")
    national = ["_All_" in record["file_name"] for record in selected]
    if any(national) and not all(national):
        raise ValueError("National and per-agency inputs overlap; use separate manifests")
    selected.sort(key=lambda record: record["http"]["bytes"])
    if limit is not None:
        if limit < 1:
            raise ValueError("File limit must be positive")
        selected = selected[:limit]
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    discover_cmd = commands.add_parser("discover")
    discover_cmd.add_argument("--years", type=int, nargs="+", default=[2025])
    agency_selection = discover_cmd.add_mutually_exclusive_group()
    agency_selection.add_argument("--agency", help="Agency database ID, or explicitly 'all' for national files")
    agency_selection.add_argument("--agency-ids", type=int, nargs="+", help="A bounded list of agency database IDs")
    discover_cmd.add_argument("--kind", choices=["contracts", "assistance"])
    discover_cmd.add_argument("--output", type=Path, required=True)
    download_cmd = commands.add_parser("download")
    download_cmd.add_argument("--manifest", type=Path, required=True)
    download_cmd.add_argument("--directory", type=Path, default=Path("data/raw"))
    download_cmd.add_argument("--reserve-gib", type=float, default=8)
    download_cmd.add_argument("--kind", choices=["contracts", "assistance"])
    download_cmd.add_argument("--chunk-mib", type=float, default=8)
    download_cmd.add_argument("--limit", type=int, help="Download at most this many files, smallest first")
    profile_cmd = commands.add_parser("profile")
    profile_cmd.add_argument("archive", type=Path)
    profile_cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "discover":
        agency = args.agency if args.agency in (None, "all") else int(args.agency)
        kinds = [args.kind] if args.kind else ("contracts", "assistance")
        discover(args.years, agency, args.output, args.agency_ids, kinds)
    elif args.command == "download":
        records = json.loads(args.manifest.read_text())["files"]
        selected = select_records(records, args.kind, args.limit)
        args.directory.mkdir(parents=True, exist_ok=True)
        for record in selected:
            download(record, args.directory, args.reserve_gib, args.chunk_mib)
    else:
        result = profile(args.archive)
        save_json(args.output, result)
        print(json.dumps({k: v for k, v in result.items() if k != "members"}, indent=2))


if __name__ == "__main__":
    main()
