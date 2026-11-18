"""Preserve files in a GitHub release: draft first, upload, verify every digest, then publish.

Nothing is published unless every required file is present with the expected size and SHA-256.
Published assets are never overwritten: a tag that already exists stops the run.

A source file at or above GitHub's per-asset limit is preserved as exact byte ranges of the
original (`<name>.part001`, ...) plus `<name>.parts.json`, which records the original name, size
and SHA-256 and every part's offset, size and SHA-256 in order. Parts are uploaded straight from
the original file, so splitting needs no extra disk. Concatenating the parts in order restores
identical bytes; `pipeline.source reassemble` does that and checks the whole-file digest.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
ASSET_LIMIT = 2 * 1024 ** 3  # GitHub's per-asset limit (strictly below 2 GiB)
PART_BYTES = 1900 * 1024 ** 2  # comfortably below the limit


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Slice(io.RawIOBase):
    """A read-only view of `length` bytes of a file starting at `offset`, for streaming uploads."""

    def __init__(self, path: Path, offset: int, length: int):
        self.stream = Path(path).open("rb")
        self.stream.seek(offset)
        self.remaining = length

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        size = self.remaining if size is None or size < 0 else min(size, self.remaining)
        data = self.stream.read(size)
        if not data:
            raise IOError("Source file ended before the expected part length")
        self.remaining -= len(data)
        return data

    def close(self) -> None:
        self.stream.close()
        super().close()


def split_manifest(path: Path, part_bytes: int = PART_BYTES) -> dict:
    """Whole-file and per-part SHA-256 in one sequential read; no bytes are written."""
    path = Path(path)
    size = path.stat().st_size
    if part_bytes <= 0 or part_bytes >= ASSET_LIMIT:
        raise ValueError("Part size must be positive and below the asset limit")
    whole, parts, offset, index = hashlib.sha256(), [], 0, 1
    with path.open("rb") as stream:
        while offset < size:
            length = min(part_bytes, size - offset)
            digest, left = hashlib.sha256(), length
            while left:
                block = stream.read(min(8 * 1024 * 1024, left))
                if not block:
                    raise IOError(f"{path.name} shrank while it was being read")
                digest.update(block)
                whole.update(block)
                left -= len(block)
            parts.append({"index": index, "name": f"{asset_name(path.name)}.part{index:03d}", "offset": offset,
                          "size": length, "sha256": digest.hexdigest()})
            offset += length
            index += 1
    if path.stat().st_size != size:
        raise IOError(f"{path.name} changed size while it was being read")
    return {"format": "money-trail byte-split v1", "source_name": path.name, "size": size,
            "sha256": whole.hexdigest(), "part_bytes": part_bytes, "parts": parts,
            "reassemble": "Concatenate the parts in index order; the result must have this size and SHA-256."}


def asset_name(name: str) -> str:
    """The name GitHub will store: it replaces characters outside letters, digits, '.', '-' and '_'
    (for example the parentheses in Treasury's 'FY(All)' file names) with '.'."""
    return re.sub(r"[^A-Za-z0-9._-]", ".", name)


def call(method: str, url: str, token: str, body=None, data=None, headers=None):
    head = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", **(headers or {})}
    payload = json.dumps(body).encode() if body is not None else data
    if body is not None:
        head["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=head, method=method)
    with urllib.request.urlopen(request, timeout=3600) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def tag_commit(repository: str, tag: str, token: str) -> str | None:
    """The commit a tag points at, peeling annotated tags; None if the tag does not exist."""
    try:
        ref = call("GET", f"{API}/repos/{repository}/git/ref/tags/{urllib.parse.quote(tag)}", token)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    obj = ref["object"]
    for _ in range(5):  # annotated tags can point at further tag objects
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            raise RuntimeError(f"Tag {tag} points at a {obj['type']}, not a commit")
        obj = call("GET", f"{API}/repos/{repository}/git/tags/{obj['sha']}", token)["object"]
    raise RuntimeError(f"Tag {tag} has too many levels of indirection")


def ensure_tag(repository: str, tag: str, target: str, token: str) -> None:
    """Make sure `tag` points at `target` before anything is published; create it if missing."""
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise RuntimeError("Target must be a full 40-character commit SHA")
    current = tag_commit(repository, tag, token)
    if current is None:
        call("POST", f"{API}/repos/{repository}/git/refs", token,
             body={"ref": f"refs/tags/{tag}", "sha": target})
        current = tag_commit(repository, tag, token)
    if current != target:
        raise RuntimeError(f"Tag {tag} points at {current}, not {target}; nothing published")


def preserve(repository: str, tag: str, title: str, notes: str, files: list[Path], token: str,
             target: str, split: list[Path] = (), manifest_dir: Path | None = None,
             part_bytes: int = PART_BYTES) -> dict:
    """Upload `files` whole and each file in `split` as byte-range parts plus a parts manifest
    (written into `manifest_dir` and uploaded with the rest)."""
    files, split = [Path(p) for p in files], [Path(p) for p in split]
    uploads = [(p, p.name, 0, None) for p in files]  # (path, source name, offset, length or None for whole)
    split_manifests = {}
    for path in split:
        if not path.is_file():
            raise FileNotFoundError(path)
        manifest = split_manifest(path, part_bytes)
        target_dir = Path(manifest_dir or path.parent)
        manifest_path = target_dir / f"{path.name}.parts.json"
        manifest_path.write_text(json.dumps(manifest, indent=1))
        split_manifests[path.name] = manifest
        files.append(manifest_path)
        uploads.append((manifest_path, manifest_path.name, 0, None))
        uploads += [(path, part["name"], part["offset"], part["size"]) for part in manifest["parts"]]
    for path in files:
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size >= ASSET_LIMIT:
            raise RuntimeError(f"{path.name} is {path.stat().st_size} bytes, over GitHub's per-asset limit; "
                               "split it or use other storage")
    try:
        call("GET", f"{API}/repos/{repository}/releases/tags/{urllib.parse.quote(tag)}", token)
        raise RuntimeError(f"Release {tag} already exists; published assets are never overwritten")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    ensure_tag(repository, tag, target, token)
    expected = {asset_name(p.name): {"source_name": p.name, "size": p.stat().st_size, "sha256": sha256(p)}
                for p in files}
    for source, manifest in split_manifests.items():
        for part in manifest["parts"]:
            expected[part["name"]] = {"source_name": source, "part": part["index"], "offset": part["offset"],
                                      "size": part["size"], "sha256": part["sha256"]}
    if len(expected) != len(uploads):
        raise RuntimeError("Two files map to the same release asset name")
    release = call("POST", f"{API}/repos/{repository}/releases", token,
                   body={"tag_name": tag, "target_commitish": target, "name": title, "body": notes,
                         "draft": True, "prerelease": True})
    upload_base = release["upload_url"].split("{")[0]
    for path, name, offset, length in uploads:
        size = path.stat().st_size if length is None else length
        stream = Slice(path, offset, size)
        try:
            call("POST", f"{upload_base}?name={urllib.parse.quote(asset_name(name))}", token, data=stream,
                 headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)})
        finally:
            stream.close()
    assets = call("GET", f"{API}/repos/{repository}/releases/{release['id']}/assets?per_page=100", token)
    remote = {a["name"]: a for a in assets}
    problems = []
    for name, want in expected.items():
        got = remote.get(name)
        if got is None:
            problems.append(f"{name}: missing")
        elif got["size"] != want["size"]:
            problems.append(f"{name}: size {got['size']} != {want['size']}")
        elif got.get("digest") != f"sha256:{want['sha256']}":
            problems.append(f"{name}: digest {got.get('digest')} != sha256:{want['sha256']}")
    extra = set(remote) - set(expected)
    if extra:
        problems.append(f"unexpected assets: {sorted(extra)}")
    if problems:
        raise RuntimeError("Release left as a draft; verification failed: " + "; ".join(problems))
    # Check the tag again immediately before publishing; a moved tag leaves the release a draft.
    if tag_commit(repository, tag, token) != target:
        raise RuntimeError(f"Release left as a draft; tag {tag} no longer points at {target}")
    published = call("PATCH", f"{API}/repos/{repository}/releases/{release['id']}", token, body={"draft": False})
    return {"tag": tag, "url": published["html_url"], "target": target,
            "tag_object": tag_commit(repository, tag, token), "assets": expected,
            "split": {name: {"size": m["size"], "sha256": m["sha256"], "parts": len(m["parts"])}
                      for name, m in split_manifests.items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--manifest", required=True, help="where to write the verified asset list")
    parser.add_argument("--target", required=True, help="the exact commit the release's tag must point at")
    parser.add_argument("--split", action="append", default=[],
                        help="a file to preserve as exact byte-range parts plus a parts manifest (repeatable)")
    parser.add_argument("--manifest-dir", help="where to write parts manifests (default: next to the file)")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()
    if not args.files and not args.split:
        parser.error("nothing to preserve")
    result = preserve(args.repository, args.tag, args.title, args.notes, [Path(f) for f in args.files],
                      os.environ["GH_TOKEN"], target=args.target, split=[Path(f) for f in args.split],
                      manifest_dir=Path(args.manifest_dir) if args.manifest_dir else None)
    Path(args.manifest).write_text(json.dumps(result, indent=1))
    print(json.dumps({"tag": result["tag"], "url": result["url"], "files": len(result["assets"])}))


if __name__ == "__main__":
    main()
