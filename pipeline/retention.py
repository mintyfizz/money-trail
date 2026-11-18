"""Release validated ZIPs while pinning one small real agency archive for regression checks."""
from datetime import date
import json
from pathlib import Path

from ingest.archive import GIB, MIB, exclusive_lock, now, sha256, verified_receipt
from pipeline.bronze import Budget, LimitedWriter, check_existing, sync_directory


def durable_json(path, value, reserve_gib):
    path = Path(path)
    payload = (json.dumps(value, indent=2) + "\n").encode()
    temporary = path.with_suffix(path.suffix + ".pending")
    # A pending metadata write requires inspection instead of silent replacement.
    with LimitedWriter(temporary, Budget(path.parent, len(payload), int(reserve_gib * GIB))) as stream:
        stream.write(payload)
    temporary.replace(path)
    sync_directory(path.parent)


def check_fixture(raw_root):
    raw_root = Path(raw_root)
    fixture = json.loads((raw_root / "fixture.json").read_text())
    name = fixture["file_name"]
    if Path(name).name != name:
        raise RuntimeError("Unsafe fixture path")
    path = raw_root / name
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Pinned test ZIP is missing; retain all other ZIPs")
    receipt = verified_receipt(path)
    if receipt["sha256"] != fixture["sha256"] or receipt["bytes"] != fixture["bytes"]:
        raise RuntimeError("Pinned test ZIP differs from its registry")
    return fixture


def check_identity(result, record):
    receipt = result["source_receipt"]
    if (result["source_file"] != record["file_name"] or result["source_url"] != record["url"]
            or result["source_bytes"] != record["http"]["bytes"]
            or receipt.get("etag") != record["http"].get("etag")
            or result.get("snapshot_date") != record.get("updated_date")):
        raise RuntimeError("Preserved Parquet source identity differs from selected catalog")


def reuse_deleted(record, root):
    """A completed deletion is reusable without redownloading a replaced monthly ZIP."""
    root = Path(root)
    name = record["file_name"]
    if Path(name).name != name:
        raise ValueError("Unsafe archive filename")
    raw = root / "raw" / name
    directory = root / "bronze" / Path(name).stem
    marker = directory / "retention.json"
    if raw.exists() or not marker.exists():
        return None
    state = json.loads(marker.read_text())
    if state.get("status") not in ("delete_verified", "deleted"):
        raise RuntimeError("Raw ZIP missing without a verified deletion record")
    if sha256(directory / "manifest.json") != state["manifest_sha256"]:
        raise RuntimeError("Preservation manifest changed after deletion validation")
    result = check_existing(directory, state["source_sha256"])
    check_identity(result, record)
    check_fixture(root / "raw")
    return result


def release(path, directory, reserve_gib=8, fixture_max_mib=64):
    path, directory = Path(path), Path(directory)
    with exclusive_lock(path.parent / (path.name + ".lock")), exclusive_lock(directory.parent / (directory.name + ".lock")):
        receipt = verified_receipt(path)
        result = check_existing(directory, receipt["sha256"])
        if path.is_symlink() or result["source_file"] != path.name or result["source_bytes"] != receipt["bytes"]:
            raise RuntimeError("Raw path differs from validated source")
        if not result.get("snapshot_date"):
            return {"status": "retained", "reason": "missing_snapshot_date"}
        date.fromisoformat(result["snapshot_date"])
        if result.get("unpreserved_members"):
            return {"status": "retained", "reason": "non_csv_members_not_preserved"}
        registry = path.parent / "fixture.json"
        if not registry.exists():
            if receipt["bytes"] > fixture_max_mib * MIB or result["valid_rows"] == 0:
                return {"status": "retained", "reason": "awaiting_small_nonempty_agency_fixture"}
            fixture = {"file_name": path.name, "sha256": receipt["sha256"], "bytes": receipt["bytes"],
                       "snapshot_date": result["snapshot_date"], "pinned_at": now(),
                       "policy": "Never automatically delete this real agency ZIP; local regression fixture"}
            durable_json(registry, fixture, reserve_gib)
        fixture = check_fixture(path.parent)
        if fixture["file_name"] == path.name:
            return {"status": "retained", "reason": "permanent_test_fixture"}
        # All data files have been fsynced, read back, and re-hashed. Persist the evidence
        # before unlink, so a crash between unlink and status update is safely resumable.
        state = {"status": "delete_verified", "source_file": path.name,
                 "source_sha256": receipt["sha256"], "source_bytes": receipt["bytes"],
                 "manifest_sha256": sha256(directory / "manifest.json"), "verified_at": now(),
                 "fixture_file": fixture["file_name"], "csv_rows": result["csv_rows"],
                 "valid_rows": result["valid_rows"], "rejected_rows": result["rejected_rows"]}
        durable_json(directory / "retention.json", state, reserve_gib)
        path.unlink()
        sync_directory(path.parent)
        state.update({"status": "deleted", "deleted_at": now()})
        durable_json(directory / "retention.json", state, reserve_gib)
        return state
