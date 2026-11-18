"""Byte-split preservation of oversized source files, reassembly, and rebuild versus refresh."""
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pipeline import guard, release, source


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

    @staticmethod
    def release_of(store):
        assets = {n: {"name": n, "id": i, "size": len(b), "digest": "sha256:" + hashlib.sha256(b).hexdigest()}
                  for i, (n, b) in enumerate(store.items())}

        @contextmanager
        def opener(asset):
            yield io.BytesIO(store[asset["name"]])
        return assets, opener

    def split_store(self):
        m = release.split_manifest(self.original, part_bytes=300)
        store = {f"{self.original.name}.parts.json": json.dumps(m).encode()}
        for p in m["parts"]:
            store[p["name"]] = self.data[p["offset"]:p["offset"] + p["size"]]
        return store

    def test_rebuild_uses_preserved_bytes_and_never_treasury(self):
        assets, opener = self.release_of(self.split_store())
        from ingest import archive
        with mock.patch.object(archive, "head", side_effect=AssertionError("contacted Treasury")), \
             mock.patch.object(archive, "download", side_effect=AssertionError("contacted Treasury")):
            result = source.rebuild("o/r", "t", self.original.name, self.root / "rebuilt", reserve_gib=0,
                                    assets=assets, opener=opener)
        self.assertEqual((self.root / "rebuilt" / self.original.name).read_bytes(), self.data)
        self.assertFalse(result["contacted_treasury"])
        self.assertEqual(result["method"], "reassembled from verified parts")
        self.assertEqual(list((self.root / "rebuilt" / "_parts").iterdir()),
                         [self.root / "rebuilt" / "_parts" / f"{self.original.name}.parts.json"])

    def test_rebuild_of_an_unsplit_release_checks_the_preserved_digest(self):
        name = "FY2025_All_Contracts_Full_20261106.zip"
        digest = hashlib.sha256(self.data).hexdigest()
        store = {"sha256.txt": f"{digest}  data/zip/{name}\n".encode(), name: self.data}  # as FY2021-24 recorded
        assets, opener = self.release_of(store)
        result = source.rebuild("o/r", "t", name, self.root / "r1", reserve_gib=0, assets=assets, opener=opener)
        self.assertEqual(result["sha256"], digest)
        store[name] = self.data + b"!"
        assets, opener = self.release_of(store)
        with self.assertRaisesRegex(source.SnapshotError, "preserved"):
            source.rebuild("o/r", "t", name, self.root / "r2", reserve_gib=0, assets=assets, opener=opener)

    def test_rebuild_refuses_to_start_below_the_reserve(self):
        name = "FY2025_All_Contracts_Full_20261106.zip"
        digest = hashlib.sha256(self.data).hexdigest()
        assets, opener = self.release_of({"sha256.txt": f"{digest}  {name}\n".encode(), name: self.data})
        reserve = 8 * guard.GIB
        with mock.patch.object(guard, "free_bytes", return_value=reserve + 200):  # sha file fits, the ZIP does not
            with self.assertRaisesRegex(RuntimeError, "Disk reserve"):
                source.rebuild("o/r", "t", name, self.root / "low", reserve_gib=8, assets=assets, opener=opener)
        self.assertFalse((self.root / "low" / name).exists())
        store = self.split_store()
        manifest_bytes = len(store[f"{self.original.name}.parts.json"])
        assets, opener = self.release_of(store)
        # The manifest and any one part fit; the whole file plus one part does not.
        self.assertLess(manifest_bytes, 1072)
        with mock.patch.object(guard, "free_bytes", return_value=reserve + manifest_bytes):
            with self.assertRaisesRegex(RuntimeError, r"Disk reserve: \d+ bytes free, 1072 needed"):
                source.rebuild("o/r", "t", self.original.name, self.root / "low2", reserve_gib=8,
                               assets=assets, opener=opener)

    def test_download_stops_when_free_space_shrinks_mid_stream(self):
        asset = {"name": "big.zip", "id": 1, "size": len(self.data),
                 "digest": "sha256:" + hashlib.sha256(self.data).hexdigest()}
        free = iter([8 * guard.GIB + 10 ** 6, 8 * guard.GIB + 10 ** 6] + [8 * guard.GIB] * 100)

        @contextmanager
        def trickle(_):
            class Slow(io.BytesIO):
                def read(self, n=-1):
                    return super().read(100)
            yield Slow(self.data)
        with mock.patch.object(guard, "free_bytes", side_effect=lambda path: next(free)):
            with self.assertRaisesRegex(RuntimeError, "Disk reserve"):
                source.fetch_asset(asset, self.root / "dl", trickle, reserve_gib=8)
        self.assertFalse((self.root / "dl" / "big.zip").exists())  # unverified partial removed

    def test_download_rejects_wrong_length_or_digest(self):
        good = {"name": "a.bin", "id": 1, "size": 4, "digest": "sha256:" + hashlib.sha256(b"abcd").hexdigest()}

        def opener_of(data):
            @contextmanager
            def opener(_):
                yield io.BytesIO(data)
            return opener
        for data, message in ((b"abcde", "longer"), (b"abc", "recorded"), (b"abcx", "SHA-256")):
            with self.subTest(data=data), self.assertRaisesRegex(source.SnapshotError, message):
                source.fetch_asset(good, self.root / f"d{len(data)}{data[-1]}", opener_of(data), reserve_gib=0)
        with self.assertRaisesRegex(source.SnapshotError, "no recorded"):
            source.fetch_asset(dict(good, digest=None), self.root / "nd", opener_of(b"abcd"), reserve_gib=0)

    def test_rebuild_refuses_parts_that_differ_from_the_release_record(self):
        store = self.split_store()
        assets, opener = self.release_of(store)
        part = next(n for n in assets if n.endswith("part002"))
        assets[part] = dict(assets[part], digest="sha256:" + "0" * 64)
        with self.assertRaisesRegex(source.SnapshotError, "does not match the parts manifest"):
            source.rebuild("o/r", "t", self.original.name, self.root / "x", reserve_gib=0, assets=assets, opener=opener)

    def test_refresh_records_http_identity_and_download_resumes(self):
        from ingest import archive
        from pipeline import fetch
        files = [{"file_name": "FY2025_All_Assistance_Full_20261106.zip",
                  "url": "https://files.usaspending.gov/award_data_archive/FY2025_All_Assistance_Full_20261106.zip"}]
        http = {"bytes": 772, "etag": '"abc"', "last_modified": "Mon, 09 Nov 2026 00:00:00 GMT"}
        with mock.patch.object(fetch, "monthly_files", return_value=files) as listing, \
             mock.patch.object(archive, "head", return_value=http):
            record = source.refresh("assistance", 2025)
        listing.assert_called_once_with(2025, "assistance")
        self.assertEqual((record["http"], record["size"]), (http, 772))
        target = self.root / "zip" / record["file_name"]
        calls = []

        def flaky(rec, directory, reserve_gib, chunk_mib):
            calls.append((rec["http"]["etag"], reserve_gib))
            if len(calls) < 3:
                raise archive.DownloadInterrupted("connection reset")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.data)
            Path(str(target) + ".receipt.json").write_text(json.dumps(
                {"bytes": 772, "sha256": hashlib.sha256(self.data).hexdigest(), "etag": '"abc"',
                 "zip_crc_status": "verified"}))
            return target
        with mock.patch.object(archive, "download", side_effect=flaky):
            result = source.download_source(record, self.root / "zip", reserve_gib=8, pause=0)
        self.assertEqual(calls, [('"abc"', 8)] * 3)  # resumed twice, same pinned identity
        self.assertEqual(result["sha256"], hashlib.sha256(self.data).hexdigest())
        with mock.patch.object(archive, "download", side_effect=RuntimeError("Unsafe range response")):
            with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                source.download_source(record, self.root / "zip2", pause=0)  # not retried

    def test_changed_bytes_get_a_new_snapshot_id(self):
        a = source.snapshot_id("assistance", 2021, "2026-11-06", hashlib.sha256(b"a").hexdigest())
        b = source.snapshot_id("assistance", 2021, "2026-11-06", hashlib.sha256(b"b").hexdigest())
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("assistance-fy2021-2026-11-06-"))


if __name__ == "__main__":
    unittest.main()
