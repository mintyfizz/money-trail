"""Byte-split preservation of oversized source files, reassembly, and rebuild versus refresh."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pipeline import release, source


class FakeGitHub:
    """Stores the bytes actually uploaded and reports their real size and digest back."""

    def __init__(self, target):
        self.target, self.assets, self.published = target, {}, False

    def __call__(self, method, url, token, body=None, data=None, headers=None):
        if method == "GET" and "/git/ref/tags/" in url:
            return {"object": {"type": "commit", "sha": self.target}}
        if method == "GET" and "/releases/tags/" in url:
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if method == "POST" and url.endswith("/releases"):
            return {"id": 1, "upload_url": "https://uploads.example/releases/1/assets{?name}"}
        if method == "POST" and "uploads" in url:
            name = url.split("name=")[1]
            chunks = []
            while True:
                block = data.read(7)  # small reads, like http.client's blocked sends
                if not block:
                    break
                chunks.append(block)
            payload = b"".join(chunks)
            assert len(payload) == int(headers["Content-Length"])
            self.assets[name] = payload
            return {}
        if method == "GET" and "/assets" in url:
            return [{"name": n, "size": len(b), "digest": "sha256:" + hashlib.sha256(b).hexdigest()}
                    for n, b in self.assets.items()]
        if method == "PATCH":
            self.published = True
            return {"html_url": "https://example/release"}
        raise AssertionError((method, url))


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original = self.root / "FY2021_All_Assistance_Full_20261106.zip"
        self.data = bytes(range(256)) * 3 + b"tail"  # 772 bytes
        self.original.write_bytes(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_records_exact_ordered_ranges(self):
        m = release.split_manifest(self.original, part_bytes=300)
        self.assertEqual([(p["offset"], p["size"]) for p in m["parts"]], [(0, 300), (300, 300), (600, 172)])
        self.assertEqual(m["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(m["parts"][1]["sha256"], hashlib.sha256(self.data[300:600]).hexdigest())
        self.assertEqual(m["parts"][0]["name"], "FY2021_All_Assistance_Full_20261106.zip.part001")
        with self.assertRaises(ValueError):
            release.split_manifest(self.original, part_bytes=release.ASSET_LIMIT)

    def test_split_upload_preserves_identical_bytes_without_temporary_chunks(self):
        other = self.root / "extract.json"
        other.write_text("{}")
        fake = FakeGitHub("1" * 40)
        manifests = self.root / "manifests"
        manifests.mkdir()
        before = set(self.root.iterdir())
        with mock.patch.object(release, "call", fake):
            result = release.preserve("o/r", "t", "T", "N", [other], "tok", target="1" * 40,
                                      split=[self.original], manifest_dir=manifests, part_bytes=300)
        self.assertTrue(fake.published)
        self.assertEqual(set(self.root.iterdir()), before)  # nothing written beside the source
        names = sorted(n for n in fake.assets if ".part" in n and not n.endswith(".json"))
        self.assertEqual(len(names), 3)
        self.assertEqual(b"".join(fake.assets[n] for n in names), self.data)
        stored = json.loads(fake.assets["FY2021_All_Assistance_Full_20261106.zip.parts.json"])
        self.assertEqual(stored["size"], len(self.data))
        self.assertEqual(result["split"]["FY2021_All_Assistance_Full_20261106.zip"]["parts"], 3)
        self.assertNotIn("FY2021_All_Assistance_Full_20261106.zip", fake.assets)  # never uploaded whole

    def test_split_release_stays_draft_if_a_part_is_corrupted(self):
        fake = FakeGitHub("1" * 40)
        original_call = fake.__call__

        def corrupting(method, url, token, body=None, data=None, headers=None):
            out = original_call(method, url, token, body, data, headers)
            if method == "POST" and url.endswith("part002"):
                fake.assets["FY2021_All_Assistance_Full_20261106.zip.part002"] = b"x" * 300
            return out
        with mock.patch.object(release, "call", corrupting):
            with self.assertRaisesRegex(RuntimeError, "part002"):
                release.preserve("o/r", "t", "T", "N", [], "tok", target="1" * 40, split=[self.original],
                                 manifest_dir=self.root, part_bytes=300)
        self.assertFalse(fake.published)

    def parts_on_disk(self, manifest):
        directory = self.root / "parts"
        directory.mkdir(exist_ok=True)
        for p in manifest["parts"]:
            (directory / p["name"]).write_bytes(self.data[p["offset"]:p["offset"] + p["size"]])
        return directory

    def test_reassemble_restores_bytes_one_part_at_a_time(self):
        m = release.split_manifest(self.original, part_bytes=300)
        directory = self.parts_on_disk(m)
        seen = []

        def fetch(part):
            seen.append(len(list(directory.iterdir())))
            return directory / part["name"]
        result = source.reassemble(m, fetch, self.root / "out" / "x.zip", reserve_gib=0,
                                   expected_source=self.original.name)
        self.assertEqual((self.root / "out" / "x.zip").read_bytes(), self.data)
        self.assertEqual(result["sha256"], m["sha256"])
        self.assertEqual(seen, [3, 2, 1])  # each part is removed once appended
        self.assertEqual(list(directory.iterdir()), [])

    def test_reassemble_refuses_tampered_missing_or_foreign_parts(self):
        m = release.split_manifest(self.original, part_bytes=300)
        directory = self.parts_on_disk(m)
        (directory / m["parts"][1]["name"]).write_bytes(b"y" * 300)
        with self.assertRaisesRegex(source.SnapshotError, "part 2"):
            source.reassemble(m, lambda p: directory / p["name"], self.root / "a.zip", reserve_gib=0,
                              delete_parts=False)
        gap = dict(m, parts=[m["parts"][0], m["parts"][2]])
        with self.assertRaises(source.SnapshotError):
            source.reassemble(gap, lambda p: directory / p["name"], self.root / "b.zip", reserve_gib=0)
        with self.assertRaisesRegex(source.SnapshotError, "expected FY2025"):
            source.reassemble(m, lambda p: directory / p["name"], self.root / "c.zip", reserve_gib=0,
                              expected_source="FY2025_All_Assistance_Full_20261106.zip")

    def test_rebuild_uses_preserved_bytes_and_never_treasury(self):
        m = release.split_manifest(self.original, part_bytes=300)
        store = {f"{self.original.name}.parts.json": json.dumps(m).encode()}
        for p in m["parts"]:
            store[p["name"]] = self.data[p["offset"]:p["offset"] + p["size"]]

        def download(asset, directory):
            directory.mkdir(parents=True, exist_ok=True)
            if asset not in store:
                raise source.SnapshotError("not in release")
            (directory / asset).write_bytes(store[asset])
            return directory / asset
        with mock.patch.object(source, "head", side_effect=AssertionError("contacted Treasury")):
            result = source.rebuild("o/r", "t", self.original.name, self.root / "rebuilt", reserve_gib=0,
                                    download=download)
        self.assertEqual((self.root / "rebuilt" / self.original.name).read_bytes(), self.data)
        self.assertFalse(result["contacted_treasury"])
        self.assertEqual(result["method"], "reassembled from verified parts")

    def test_rebuild_of_an_unsplit_release_checks_the_preserved_digest(self):
        name = "FY2025_All_Contracts_Full_20261106.zip"
        digest = hashlib.sha256(self.data).hexdigest()
        store = {"sha256.txt": f"{digest}  {name}\n".encode(), name: self.data}

        def download(asset, directory):
            directory.mkdir(parents=True, exist_ok=True)
            if asset not in store:
                raise source.SnapshotError("not in release")
            (directory / asset).write_bytes(store[asset])
            return directory / asset
        result = source.rebuild("o/r", "t", name, self.root / "r1", reserve_gib=0, download=download)
        self.assertEqual(result["sha256"], digest)
        store[name] = self.data + b"!"
        with self.assertRaisesRegex(source.SnapshotError, "preserved"):
            source.rebuild("o/r", "t", name, self.root / "r2", reserve_gib=0, download=download)

    def test_changed_bytes_get_a_new_snapshot_id(self):
        a = source.snapshot_id("assistance", 2021, "2026-11-06", hashlib.sha256(b"a").hexdigest())
        b = source.snapshot_id("assistance", 2021, "2026-11-06", hashlib.sha256(b"b").hexdigest())
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("assistance-fy2021-2026-11-06-"))


if __name__ == "__main__":
    unittest.main()
