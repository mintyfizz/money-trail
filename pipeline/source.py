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
from contextlib import contextmanager
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


def release_assets(repository: str, tag: str) -> dict:
    """Name -> {id, size, digest} for every asset of a published release (REST, via gh)."""
    out = subprocess.run(["gh", "api", f"repos/{repository}/releases/tags/{tag}"], check=True,
                         capture_output=True, text=True).stdout
    return {a["name"]: {"name": a["name"], "id": a["id"], "size": a["size"], "digest": a.get("digest")}
            for a in json.loads(out)["assets"]}


@contextmanager
def gh_asset_stream(repository: str, asset: dict):
    """The asset's bytes as a stream; gh follows GitHub's redirect to storage."""
    process = subprocess.Popen(["gh", "api", "-H", "Accept: application/octet-stream",
                                f"repos/{repository}/releases/assets/{asset['id']}"], stdout=subprocess.PIPE)
    try:
        yield process.stdout
    finally:
        process.stdout.close()
        if process.wait() != 0:
            raise SnapshotError(f"Download of {asset['name']} failed (gh exit {process.returncode})")


def fetch_asset(asset: dict, directory: Path, opener, reserve_gib: float = 8) -> Path:
    """Download one release asset through the reserve-checked writer. The whole asset must fit above
    the reserve before the first byte; every write is checked again; more bytes than recorded, fewer
    bytes, or a different SHA-256 fail, and the unverified partial file is removed."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if not (asset.get("digest") or "").startswith("sha256:"):
        raise SnapshotError(f"{asset['name']} has no recorded SHA-256 digest; cannot verify it")
    target = directory / asset["name"]
    if target.exists():
        raise SnapshotError(f"{target} already exists; refusing to reuse an unverified file")
    require(directory, asset["size"], reserve_gib)
    digest, ok = hashlib.sha256(), False
    sink = GuardedFile(target, reserve_gib)
    try:
        with opener(asset) as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                if sink.written + len(block) > asset["size"]:
                    raise SnapshotError(f"{asset['name']} is longer than its recorded {asset['size']} bytes")
                digest.update(block)
                sink.write(block)
        if sink.written != asset["size"]:
            raise SnapshotError(f"{asset['name']}: {sink.written} bytes, recorded {asset['size']}")
        if f"sha256:{digest.hexdigest()}" != asset["digest"]:
            raise SnapshotError(f"{asset['name']}: SHA-256 {digest.hexdigest()}, recorded {asset['digest']}")
        ok = True
    finally:
        sink.close()
        if not ok and target.exists():
            target.unlink()  # our own unverified partial download on the runner, never source evidence
    return target


def rebuild(repository: str, tag: str, source_name: str, out_dir: Path, reserve_gib: float = 8,
            assets: dict | None = None, opener=None, sha_asset: str = "sha256.txt") -> dict:
    """Fetch a preserved source file from release `tag` and prove it is byte-identical to what was
    preserved. Prefers a parts manifest; falls back to a single asset with a sha256 file. Every
    download goes through the reserve-checked writer."""
    out_dir = Path(out_dir)
    work = out_dir / "_parts"
    assets = assets if assets is not None else release_assets(repository, tag)
    opener = opener or (lambda asset: gh_asset_stream(repository, asset))
    stored = asset_name(source_name)

    def get(name: str, directory: Path) -> Path:
        if name not in assets:
            raise SnapshotError(f"Release {tag} has no asset {name}")
        return fetch_asset(assets[name], directory, opener, reserve_gib)

    if f"{stored}.parts.json" in assets:
        manifest = json.loads(get(f"{stored}.parts.json", work).read_text())
        for part in manifest.get("parts", []):
            recorded = assets.get(part["name"])
            if recorded is None or recorded["size"] != part["size"] or recorded["digest"] != f"sha256:{part['sha256']}":
                raise SnapshotError(f"Release asset {part['name']} does not match the parts manifest")
        largest = max((p["size"] for p in manifest.get("parts", [])), default=0)
        require(out_dir, manifest["size"] + largest, reserve_gib)  # whole file plus one part on disk
        result = reassemble(manifest, lambda part: get(part["name"], work), out_dir / source_name,
                            reserve_gib, expected_source=source_name)
        result["method"] = "reassembled from verified parts"
    else:
        digest, name = parse_sha256_file(get(sha_asset, work).read_text())
        if name != source_name:
            raise SnapshotError(f"sha256 file names {name}, expected {source_name}")
        if stored not in assets or assets[stored]["digest"] != f"sha256:{digest}":
            raise SnapshotError(f"Release asset {stored} does not carry the preserved SHA-256 {digest}")
        path = get(stored, out_dir)
        if path.name != source_name:
            path = path.rename(out_dir / source_name)
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
    f = sub.add_parser("fetch", help="download release assets through the reserve-checked writer, verified")
    f.add_argument("--repository", required=True)
    f.add_argument("--tag", required=True)
    f.add_argument("--dir", required=True)
    f.add_argument("--reserve-gib", type=float, default=8)
    f.add_argument("assets", nargs="+")
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
    elif args.command == "fetch":
        assets = release_assets(args.repository, args.tag)
        for name in args.assets:
            if name not in assets:
                raise SnapshotError(f"Release {args.tag} has no asset {name}")
            print(fetch_asset(assets[name], Path(args.dir), lambda a: gh_asset_stream(args.repository, a),
                              args.reserve_gib))
    else:
        print(snapshot_id(args.kind, args.year, args.snapshot_date, file_digest(Path(args.file))))


if __name__ == "__main__":
    main()
