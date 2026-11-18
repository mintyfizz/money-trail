"""Validate one build request: a single kind and fiscal year, from a fresh download or a preserved snapshot.

The national build workflow reads `.github/run-request.json` from its run branch. One request is
one scope, so heavy work stays sequential. Example:

  {"kind": "assistance", "year": 2025, "source": "refresh"}
  {"kind": "assistance", "year": 2021, "source": "rebuild",
   "snapshot_tag": "source-assistance-fy2021-2026-11-06-0123456789ab",
   "source_name": "FY2021_All_Assistance_Full_20261106.zip"}
  {"kind": "contracts", "year": 2025, "source": "rebuild", "snapshot_tag": "build-fy2025-contracts-2026-11-06-47b4a4d",
   "source_name": "FY2025_All_Contracts_Full_20261106.zip",
   "deleted_keys_tag": "build-delta-contracts-2026-11-06-47b4a4d"}
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

YEARS = range(2021, 2026)  # FY2008-2020 are a later stage, not offered here
FIELDS = {"kind", "year", "source", "snapshot_tag", "source_name", "deleted_keys_tag", "note"}


def validate(request: dict) -> dict:
    unknown = set(request) - FIELDS
    if unknown:
        raise ValueError(f"Unknown request fields {sorted(unknown)}")
    kind, year, mode = request.get("kind"), request.get("year"), request.get("source")
    if kind not in ("contracts", "assistance"):
        raise ValueError("kind must be contracts or assistance")
    if not isinstance(year, int) or year not in YEARS:
        raise ValueError(f"year must be one of {list(YEARS)}")
    if mode not in ("refresh", "rebuild"):
        raise ValueError("source must be refresh (download from Treasury) or rebuild (a preserved snapshot)")
    tag_pattern = r"[A-Za-z0-9._-]+"
    if mode == "rebuild":
        for field in ("snapshot_tag", "source_name"):
            if not request.get(field):
                raise ValueError(f"rebuild needs {field}")
        if not re.fullmatch(tag_pattern, request["snapshot_tag"]):
            raise ValueError("snapshot_tag has unexpected characters")
        expected = re.compile(rf"FY{year}_All_{kind.capitalize()}_Full_\d{{8}}\.zip")
        if not expected.fullmatch(request["source_name"]):
            raise ValueError(f"source_name must be the national {kind} file for FY{year}")
    elif request.get("snapshot_tag") or request.get("source_name"):
        raise ValueError("refresh finds its own source; snapshot_tag and source_name belong to rebuild")
    if request.get("deleted_keys_tag"):
        if kind != "contracts":
            raise ValueError("deleted-key diagnostics exist for contracts only")
        if not re.fullmatch(tag_pattern, request["deleted_keys_tag"]):
            raise ValueError("deleted_keys_tag has unexpected characters")
    return {"kind": kind, "year": year, "source": mode, "snapshot_tag": request.get("snapshot_tag", ""),
            "source_name": request.get("source_name", ""), "deleted_keys_tag": request.get("deleted_keys_tag", "")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("request")
    parser.add_argument("--github-output", help="append key=value lines here")
    args = parser.parse_args()
    checked = validate(json.loads(Path(args.request).read_text()))
    print(json.dumps(checked))
    if args.github_output:
        with open(args.github_output, "a") as out:
            for key, value in checked.items():
                out.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
