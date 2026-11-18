"""One build request = one kind and one fiscal year, from a fresh download or a preserved snapshot."""
import unittest

from pipeline.run_request import validate


class RunRequestTests(unittest.TestCase):
    def test_refresh_and_rebuild_requests(self):
        self.assertEqual(validate({"kind": "assistance", "year": 2025, "source": "refresh"})["source"], "refresh")
        r = validate({"kind": "contracts", "year": 2021, "source": "rebuild",
                      "snapshot_tag": "build-fy2021-contracts-2026-11-06",
                      "source_name": "FY2021_All_Contracts_Full_20261106.zip"})
        self.assertEqual(r["snapshot_tag"], "build-fy2021-contracts-2026-11-06")

    def test_bad_requests_are_refused(self):
        bad = [
            {"kind": "loans", "year": 2025, "source": "refresh"},
            {"kind": "assistance", "year": 2019, "source": "refresh"},
            {"kind": "assistance", "year": "2025", "source": "refresh"},
            {"kind": "assistance", "year": 2025, "source": "latest"},
            {"kind": "assistance", "year": 2025, "source": "rebuild"},
            {"kind": "assistance", "year": 2025, "source": "rebuild", "snapshot_tag": "t",
             "source_name": "FY2025_All_Contracts_Full_20261106.zip"},  # wrong kind for the file
            {"kind": "assistance", "year": 2025, "source": "rebuild", "snapshot_tag": "t; rm -rf /",
             "source_name": "FY2025_All_Assistance_Full_20261106.zip"},
            {"kind": "assistance", "year": 2025, "source": "refresh", "snapshot_tag": "t"},
            {"kind": "assistance", "year": 2025, "source": "refresh", "deleted_keys_tag": "d"},
            {"kind": "assistance", "year": 2025, "source": "refresh", "years": [2024]},
        ]
        for request in bad:
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate(request)


if __name__ == "__main__":
    unittest.main()
