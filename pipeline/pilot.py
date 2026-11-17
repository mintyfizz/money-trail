"""Sequential agency download -> verified raw Parquet -> guarded ZIP deletion (one fixture retained)."""
import argparse
import json
from pathlib import Path
import shutil

from ingest.archive import download, exclusive_lock, now, save_json, select_records
from pipeline.bronze import convert
from pipeline.retention import check_identity, release, reuse_deleted


def run(manifest, data_root, limit=1, kind="contracts", reserve_gib=8, max_output_gib=2, chunk_mib=8,
        keep_zips=False):
    catalog = json.loads(Path(manifest).read_text())
    records = select_records(catalog["files"], kind, limit)
    if any("_All_" in record["file_name"] for record in records):
        raise ValueError("Pilot workflow requires a per-agency manifest")
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(root / "pilot.lock"):
        return _run(catalog, records, root, reserve_gib, max_output_gib, chunk_mib, keep_zips)


def _run(catalog, records, root, reserve_gib, max_output_gib, chunk_mib, keep_zips):
    report = {"started_at": now(), "status": "in_progress", "catalog_complete": catalog.get("complete", False),
              "national_coverage_verified": False,
              "raw_zip_policy": "keep_all" if keep_zips else "delete_after_independent_validation_except_pinned_fixture",
              "files": []}
    report_path = root / "pilot-run.json"
    save_json(report_path, report)
    try:
        for record in records:
            item = {"file_name": record["file_name"], "phase": "download"}
            report["files"].append(item)
            save_json(report_path, report)
            path = root / "raw" / record["file_name"]
            directory = root / "bronze" / path.stem
            output = reuse_deleted(record, root)
            if output is None:
                path = download(record, root / "raw", reserve_gib, chunk_mib)
                item["phase"] = "processing"
                save_json(report_path, report)
                output = convert(path, root / "bronze", reserve_gib, max_output_gib)
                check_identity(output, record)
                item["phase"] = "validating_retention"
                save_json(report_path, report)
                retention = ({"status": "retained", "reason": "keep_zips_option"} if keep_zips
                             else release(path, directory, reserve_gib))
            else:
                retention = {"status": "already_deleted", "reason": "verified_preserved_output_reused"}
            item.update({"phase": "complete", "source_sha256": output["source_sha256"],
                         "snapshot_date": output["snapshot_date"], "source_bytes": output["source_bytes"],
                         "csv_rows": output["csv_rows"], "valid_rows": output["valid_rows"],
                         "rejected_rows": output["rejected_rows"], "parquet_bytes": output["parquet_bytes"],
                         "parquet_to_zip_ratio": output["parquet_to_zip_ratio"],
                         "conversion_seconds": output["elapsed_seconds"], "retention": retention,
                         "free_disk_bytes_after": shutil.disk_usage(root).free,
                         "parquet_directory": str(directory)})
            save_json(report_path, report)
        report["status"] = "complete_for_selected_files"
    except Exception as error:
        report["status"] = "interrupted"
        report["error"] = str(error)
        save_json(report_path, report)
        raise
    report["finished_at"] = now()
    report["measurement_scope"] = "Selected agencies only; no full-year storage extrapolation is validated"
    save_json(report_path, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--limit", type=int, default=1, help="Smallest files first; default is one pilot agency")
    parser.add_argument("--kind", choices=["contracts", "assistance"], default="contracts")
    parser.add_argument("--reserve-gib", type=float, default=8)
    parser.add_argument("--max-output-gib", type=float, default=2)
    parser.add_argument("--chunk-mib", type=float, default=8)
    parser.add_argument("--keep-zips", action="store_true", help="Retain newly processed ZIPs (does not restore previously deleted ZIPs)")
    args = parser.parse_args()
    print(json.dumps(run(args.manifest, args.data_root, args.limit, args.kind, args.reserve_gib,
                         args.max_output_gib, args.chunk_mib, args.keep_zips), indent=2))
