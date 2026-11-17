"""Source snapshots: refresh (what Treasury publishes now) versus rebuild (an exact preserved snapshot).

refresh  Lists the monthly files with an explicit type, picks the national file, and asks the server
         for its size and Last-Modified. It returns a source record; it downloads nothing and
         changes nothing. After download, `snapshot_id` names the snapshot by kind, year, snapshot
         date and the first 12 hex digits of its SHA-256, so bytes that changed under the same file
         name get a new identifier and a new release. An existing release is never overwritten.

rebuild  Reproduces a preserved snapshot without contacting Treasury. It reads the release's parts
         manifest (or, for releases made before splitting existed, its sha256 file), fetches each
         part in order, checks its size and SHA-256, appends it, deletes the part, and finally
         checks the whole file's size and SHA-256 and that the manifest names the expected source
         file. Any mismatch stops the build; nothing is relabelled or repaired.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Callable
import urllib.request

from pipeline.guard import GuardedFile, require
from pipeline.release import asset_name


class SnapshotError(RuntimeError):
    """A preserved snapshot does not match its record. The build stops."""


def snapshot_id(kind: str, year: int, snapshot_date: str, sha256: str) -> str:
    return f"{kind}-fy{year}-{snapshot_date}-{sha256[:12]}"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_file(path: Path, size: int, sha256: str, what: str) -> None:
    actual = Path(path).stat().st_size
    if actual != size:
        raise SnapshotError(f"{what}: {actual} bytes, expected {size}")
    digest = file_digest(path)
    if digest != sha256:
        raise SnapshotError(f"{what}: SHA-256 {digest}, expected {sha256}")


def reassemble(manifest: dict, fetch_part: Callable[[dict], Path], out: Path, reserve_gib: float = 8,
               expected_source: str | None = None, delete_parts: bool = True) -> dict:
    """Rebuild the original file from its parts, one part on disk at a time."""
    if manifest.get("format") != "money-trail byte-split v1":
        raise SnapshotError(f"Unknown parts manifest format {manifest.get('format')!r}")
    if expected_source and manifest["source_name"] != expected_source:
        raise SnapshotError(f"Manifest is for {manifest['source_name']}, expected {expected_source}")
    parts = sorted(manifest["parts"], key=lambda p: p["index"])
    if [p["index"] for p in parts] != list(range(1, len(parts) + 1)):
        raise SnapshotError("Parts are not numbered 1..n without gaps")
    offset = 0
    for part in parts:
        if part["offset"] != offset:
            raise SnapshotError(f"Part {part['index']} starts at {part['offset']}, expected {offset}")
        offset += part["size"]
    if offset != manifest["size"]:
        raise SnapshotError(f"Parts add up to {offset} bytes, the original had {manifest['size']}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    require(out.parent, manifest["size"], reserve_gib)
    whole = hashlib.sha256()
    sink = GuardedFile(out, reserve_gib)
    try:
        for part in parts:
            path = Path(fetch_part(part))
            verify_file(path, part["size"], part["sha256"], f"part {part['index']} ({part['name']})")
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    whole.update(block)
                    sink.write(block)
            if delete_parts:
                path.unlink()
    finally:
        sink.close()
    if sink.written != manifest["size"] or whole.hexdigest() != manifest["sha256"]:
        raise SnapshotError(f"Reassembled {out.name} does not match: {sink.written} bytes, "
                            f"SHA-256 {whole.hexdigest()}; expected {manifest['size']}, {manifest['sha256']}")
    return {"source_name": manifest["source_name"], "size": manifest["size"], "sha256": manifest["sha256"],
            "parts": len(parts), "path": str(out)}


def parse_sha256_file(text: str) -> tuple[str, str]:
    """`sha256sum` output for one file: (digest, file name)."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise SnapshotError("Expected exactly one line in the sha256 file")
    digest, name = lines[0].split(maxsplit=1)
    # Older releases recorded the runner path (data/zip/<name>); only the file name identifies it.
    return digest.lower(), Path(name.strip().lstrip("*")).name


def gh_download(repository: str, tag: str, asset: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / asset
    if target.exists():
        raise SnapshotError(f"{target} already exists; refusing to reuse an unverified file")
    subprocess.run(["gh", "release", "download", tag, "--repo", repository, "--pattern", asset,
                    "--dir", str(directory)], check=True)
    if not target.is_file():
        raise SnapshotError(f"Asset {asset} was not downloaded from {tag}")
    return target


def rebuild(repository: str, tag: str, source_name: str, out_dir: Path, reserve_gib: float = 8,
            download: Callable[[str, Path], Path] | None = None, sha_asset: str = "sha256.txt") -> dict:
    """Fetch a preserved source file from release `tag` and prove it is byte-identical to what was
    preserved. Prefers a parts manifest; falls back to a single asset with a sha256 file."""
    out_dir = Path(out_dir)
    work = out_dir / "_parts"
    download = download or (lambda asset, d: gh_download(repository, tag, asset, d))
    stored = asset_name(source_name)
    try:
        manifest_path = download(f"{stored}.parts.json", work)
    except (subprocess.CalledProcessError, SnapshotError):
        manifest_path = None
    if manifest_path is not None:
        manifest = json.loads(manifest_path.read_text())
        result = reassemble(manifest, lambda part: download(part["name"], work), out_dir / source_name,
                            reserve_gib, expected_source=source_name)
        result["method"] = "reassembled from verified parts"
    else:
        sums = download(sha_asset, work)
        digest, name = parse_sha256_file(sums.read_text())
        if name != source_name:
            raise SnapshotError(f"sha256 file names {name}, expected {source_name}")
        path = download(stored, out_dir)
        if path.name != source_name:
            path = path.rename(out_dir / source_name)
        actual = file_digest(path)
        if actual != digest:
            raise SnapshotError(f"{source_name}: SHA-256 {actual}, preserved {digest}")
        result = {"source_name": source_name, "size": path.stat().st_size, "sha256": digest, "parts": 1,
                  "path": str(path), "method": "single asset checked against preserved sha256"}
    result.update({"release": tag, "repository": repository, "contacted_treasury": False})
    return result


def head(url: str) -> dict:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "money-trail/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return {"size": int(response.headers["Content-Length"]),
                "last_modified": response.headers.get("Last-Modified"), "final_url": response.geturl()}


def refresh(kind: str, year: int, delta: bool = False) -> dict:
    from pipeline import fetch
    record = fetch.pick(fetch.monthly_files(year, kind), "Delta" if delta else "Full", kind)
    record.update(head(record["url"]))
    record.update({"fiscal_year": year, "mode": "refresh",
                   "note": "Upstream state now. The snapshot id is fixed only after download, from the bytes' SHA-256."})
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("refresh", help="find the current national file and its size; downloads nothing")
    r.add_argument("--kind", required=True, choices=["contracts", "assistance"])
    r.add_argument("--year", type=int, required=True)
    r.add_argument("--delta", action="store_true")
    b = sub.add_parser("rebuild", help="reproduce a preserved source file from its release, verified")
    b.add_argument("--repository", required=True)
    b.add_argument("--tag", required=True)
    b.add_argument("--source-name", required=True)
    b.add_argument("--out-dir", required=True)
    b.add_argument("--reserve-gib", type=float, default=8)
    b.add_argument("--sha-asset", default="sha256.txt", help="for releases without a parts manifest")
    i = sub.add_parser("id", help="snapshot identifier for a downloaded file")
    i.add_argument("--kind", required=True)
    i.add_argument("--year", type=int, required=True)
    i.add_argument("--snapshot-date", required=True)
    i.add_argument("file")
    args = parser.parse_args()
    if args.command == "refresh":
        print(json.dumps(refresh(args.kind, args.year, args.delta)))
    elif args.command == "rebuild":
        print(json.dumps(rebuild(args.repository, args.tag, args.source_name, Path(args.out_dir), args.reserve_gib,
                                 sha_asset=args.sha_asset)))
    else:
        print(snapshot_id(args.kind, args.year, args.snapshot_date, file_digest(Path(args.file))))


if __name__ == "__main__":
    main()
