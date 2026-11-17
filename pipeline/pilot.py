"""Sequential per-agency download -> ZIP verification -> Parquet. Raw ZIPs stay local."""
import argparse
import json
from pathlib import Path

from ingest.archive import download, now, save_json, select_records
from pipeline.bronze import convert


def run(manifest, data_root, limit=1, kind="contracts", reserve_gib=8, max_output_gib=2, chunk_mib=8):
    catalog = json.loads(Path(manifest).read_text())
    records = select_records(catalog["files"], kind, limit)
    if any("_All_" in record["file_name"] for record in records):
        raise ValueError("Pilot workflow requires a per-agency manifest")
    root = Path(data_root)
    report = {"started_at": now(), "status": "in_progress", "catalog_complete": catalog.get("complete", False),
              "national_coverage_verified": False, "offload": "not_configured_raw_zip_retained", "files": []}
    report_path = root / "pilot-run.json"
    save_json(report_path, report)
    try:
        for record in records:
            item = {"file_name": record["file_name"], "phase": "download"}
            report["files"].append(item)
            save_json(report_path, report)
            path = download(record, root / "raw", reserve_gib, chunk_mib)
            item["phase"] = "processing"
            save_json(report_path, report)
            output = convert(path, root / "bronze", reserve_gib, max_output_gib)
            item.update({"phase": "complete", "source_sha256": output["source_sha256"],
                         "valid_rows": output["valid_rows"], "rejected_rows": output["rejected_rows"],
                         "parquet_directory": str(root / "bronze" / path.stem)})
            save_json(report_path, report)
        report["status"] = "complete_for_selected_files"
    except Exception as error:
        report["status"] = "interrupted"
        report["error"] = str(error)
        save_json(report_path, report)
        raise
    report["finished_at"] = now()
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
    args = parser.parse_args()
    print(json.dumps(run(args.manifest, args.data_root, args.limit, args.kind, args.reserve_gib,
                         args.max_output_gib, args.chunk_mib), indent=2))
