"""Find the national contracts archive files for a fiscal year, and the latest monthly delta file."""
from __future__ import annotations

import argparse
import json
import re

from pipeline.reconcile import post

LIST = "https://api.usaspending.gov/api/v2/bulk_download/list_monthly_files/"


def monthly_files(year: int, kind: str = "contracts") -> list[dict]:
    return post(LIST, {"agency": "all", "fiscal_year": year, "type": kind})["monthly_files"]


def pick(files: list[dict], label: str) -> dict:
    pattern = re.compile(rf"_All_Contracts_{label}_(\d{{8}})\.zip$")
    matches = [(pattern.search(f["file_name"]).group(1), f) for f in files if pattern.search(f["file_name"])]
    if not matches:
        raise SystemExit(f"No national contracts {label} file listed")
    date, chosen = max(matches, key=lambda m: m[0])
    return {"file_name": chosen["file_name"], "url": chosen["url"],
            "snapshot_date": f"{date[:4]}-{date[4:6]}-{date[6:]}"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--delta", action="store_true", help="return the latest delta file instead of the full file")
    args = parser.parse_args()
    print(json.dumps(pick(monthly_files(args.year), "Delta" if args.delta else "Full")))


if __name__ == "__main__":
    main()
