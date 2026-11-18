"""Find the national archive file (contracts or assistance) for a fiscal year, or the latest monthly delta."""
from __future__ import annotations

import argparse
import json
import re

from pipeline.reconcile import post

LIST = "https://api.usaspending.gov/api/v2/bulk_download/list_monthly_files/"


KINDS = {"contracts": "Contracts", "assistance": "Assistance"}


def monthly_files(year: int, kind: str = "contracts") -> list[dict]:
    # `type` is documented as optional but the endpoint answers HTTP 400 without it: always sent.
    if kind not in KINDS:
        raise ValueError(f"Unknown kind {kind!r}")
    return post(LIST, {"agency": "all", "fiscal_year": year, "type": kind})["monthly_files"]


def pick(files: list[dict], label: str, kind: str = "contracts") -> dict:
    pattern = re.compile(rf"_All_{KINDS[kind]}_{label}_(\d{{8}})\.zip$")
    matches = [(pattern.search(f["file_name"]).group(1), f) for f in files if pattern.search(f["file_name"])]
    if not matches:
        raise SystemExit(f"No national {kind} {label} file listed")
    date, chosen = max(matches, key=lambda m: m[0])
    return {"kind": kind, "file_name": chosen["file_name"], "url": chosen["url"],
            "snapshot_date": f"{date[:4]}-{date[4:6]}-{date[6:]}"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--kind", choices=sorted(KINDS), default="contracts")
    parser.add_argument("--delta", action="store_true", help="return the latest delta file instead of the full file")
    args = parser.parse_args()
    print(json.dumps(pick(monthly_files(args.year, args.kind), "Delta" if args.delta else "Full", args.kind)))


if __name__ == "__main__":
    main()
