"""Preserve files in a GitHub release: draft first, upload, verify every digest, then publish.

Nothing is published unless every required file is present with the expected size and SHA-256.
Published assets are never overwritten: a tag that already exists stops the run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
ASSET_LIMIT = 2 * 1024 ** 3  # GitHub's per-asset limit (strictly below 2 GiB)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def preserve(repository: str, tag: str, title: str, notes: str, files: list[Path], token: str) -> dict:
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
    expected = {asset_name(p.name): {"source_name": p.name, "size": p.stat().st_size, "sha256": sha256(p)}
                for p in files}
    if len(expected) != len(files):
        raise RuntimeError("Two files map to the same release asset name")
    release = call("POST", f"{API}/repos/{repository}/releases", token,
                   body={"tag_name": tag, "target_commitish": "main", "name": title, "body": notes,
                         "draft": True, "prerelease": True})
    upload_base = release["upload_url"].split("{")[0]
    for path in files:
        with path.open("rb") as stream:
            call("POST", f"{upload_base}?name={urllib.parse.quote(asset_name(path.name))}", token, data=stream,
                 headers={"Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)})
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
    published = call("PATCH", f"{API}/repos/{repository}/releases/{release['id']}", token, body={"draft": False})
    return {"tag": tag, "url": published["html_url"], "assets": expected}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--manifest", required=True, help="where to write the verified asset list")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()
    result = preserve(args.repository, args.tag, args.title, args.notes, [Path(f) for f in args.files],
                      os.environ["GH_TOKEN"])
    Path(args.manifest).write_text(json.dumps(result, indent=1))
    print(json.dumps({"tag": result["tag"], "url": result["url"], "files": len(result["assets"])}))


if __name__ == "__main__":
    main()
