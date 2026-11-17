"""Tests for the typed extraction, aggregates, reconciliation and change tracking."""
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from pipeline import changes, gold, reconcile, silver

BASE = {c: "" for c in silver.COLUMNS}


def row(key, amount, state="CA", country="USA", agency="Department of Defense", uei="UEI1", name="Acme Corp",
        award_type="D", idv="", fy="2025", date="2025-03-01", award=None, description="Widgets", **extra):
    values = dict(BASE)
    values.update({
        "contract_transaction_unique_key": key, "contract_award_unique_key": award or f"CONT_AWD_{key}",
        "federal_action_obligation": amount, "action_date": date, "action_date_fiscal_year": fy,
        "awarding_agency_name": agency, "recipient_uei": uei, "recipient_name": name,
        "primary_place_of_performance_country_code": country, "primary_place_of_performance_state_code": state,
        "award_type_code": award_type, "idv_type_code": idv,
        "prime_award_base_transaction_description": description})
    values.update(extra)
    return values


def write_zip(path, members, delta=False):
    columns = (["correction_delete_ind"] if delta else []) + list(silver.COLUMNS) + ["unused_column"]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, rows in members.items():
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for r in rows:
                writer.writerow({**{c: "" for c in columns}, **r, "unused_column": "x"})
            archive.writestr(name, buffer.getvalue())


ROWS_A = [
    row("T1", "1000.10", description="Line one\nline two, with a comma"),
    row("T2", "-200.05"),
    row("T3", "500.00", state="TX", agency="NASA", uei="UEI2", name="Orbit LLC", award_type="A"),
    row("T4", "300.00", state="TX", uei="", name="Somebody", award_type="B"),
    row("T5", "50.00", state="", award_type="", idv="B", award="CONT_IDV_X"),
]
ROWS_B = [
    row("T6", "75.25", state="PR", country="USA"),
    row("T7", "40.00", state="", country="DEU"),
    row("T8", "", state="DC", award_type="C"),
    row("T9", "10.00", state="CA", award="CONT_AWD_T1"),
]


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.zip = self.root / "FY2025_All_Contracts_Full_20261106.zip"
        write_zip(self.zip, {"part_1.csv": ROWS_A, "part_2.csv": ROWS_B})
        self.report = silver.extract(self.zip, self.root / "silver", block_mib=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_counts_and_exact_sum(self):
        self.assertEqual(self.report["rows"], 9)
        self.assertEqual([m["rows"] for m in self.report["members"]], [5, 4])
        self.assertEqual((self.report["bad_money"], self.report["bad_dates"], self.report["blank_keys"]), (0, 0, 0))
        self.assertEqual(self.report["members"][0]["obligation_sum"], "1650.05")

    def test_extract_keeps_raw_values_and_source_rows(self):
        import pyarrow.parquet as pq
        table = pq.read_table(self.root / "silver" / "part-0000.parquet").to_pylist()
        self.assertEqual(table[0]["prime_award_base_transaction_description"], "Line one\nline two, with a comma")
        self.assertEqual(table[1]["federal_action_obligation_raw"], "-200.05")
        self.assertEqual(str(table[1]["federal_action_obligation"]), "-200.05")
        self.assertEqual([r["source_row"] for r in table], [2, 3, 4, 5, 6])
        blank = pq.read_table(self.root / "silver" / "part-0001.parquet").to_pylist()[2]
        self.assertIsNone(blank["federal_action_obligation"])
        self.assertEqual(blank["federal_action_obligation_raw"], "")

    def test_bad_money_is_counted_not_zeroed(self):
        bad_zip = self.root / "bad.zip"
        write_zip(bad_zip, {"x.csv": [row("B1", "12,000.00"), row("B2", "5.00")]})
        report = silver.extract(bad_zip, self.root / "bad")
        self.assertEqual(report["bad_money"], 1)
        self.assertEqual(report["members"][0]["obligation_sum"], "5.00")

    def test_missing_columns_fail(self):
        broken = self.root / "broken.zip"
        with zipfile.ZipFile(broken, "w") as archive:
            archive.writestr("x.csv", "contract_transaction_unique_key,action_date\nK,2025-01-01\n")
        with self.assertRaises(ValueError):
            silver.extract(broken, self.root / "broken")

    def test_summary_buckets_and_cents(self):
        result = gold.summarise(self.root / "silver", 2025, "2026-11-06", self.zip.name)
        self.assertEqual(result["national"]["net_obligations_cents"], 177530)
        self.assertEqual(result["states"]["CA"]["net_obligations_cents"], 81005)
        self.assertEqual(result["states"]["CA"]["deobligations_cents"], -20005)
        self.assertEqual(result["states"]["TX"]["net_obligations_cents"], 80000)
        self.assertEqual(result["states"]["DC"]["net_obligations_cents"], 0)
        self.assertEqual(result["outside_map"]["territory_cents"], 7525)
        self.assertEqual(result["outside_map"]["foreign_cents"], 4000)
        self.assertEqual(result["outside_map"]["unknown_cents"], 5000)
        self.assertEqual(result["outside_map"]["territories"][0]["code"], "PR")
        self.assertEqual(result["states"]["CA"]["rank"], 1)
        self.assertEqual(result["checks"], {"rows": 9, "duplicate_keys": 0, "other_fiscal_year": 0,
                                            "null_obligations": 1, "negative_rows": 1})

    def test_summary_awards_and_recipients(self):
        result = gold.summarise(self.root / "silver", 2025, "2026-11-06", self.zip.name)
        ca = result["states"]["CA"]
        self.assertEqual(ca["awards"], 2)  # T1 and T9 share an award; T2 is its own
        self.assertEqual(ca["transactions"], 3)
        tx = result["states"]["TX"]
        names = {r["name"] for r in tx["recipients"]}
        self.assertIn("Recipients without an ID (combined)", names)
        self.assertNotIn("Somebody", names)
        types = {t["code"]: t["amount_cents"] for t in tx["types"]}
        self.assertEqual(types, {"A": 50000, "B": 30000})
        self.assertEqual(ca["agencies"][0]["share"], 1.0)

    def test_reconcile_compares_in_cents(self):
        summary = gold.summarise(self.root / "silver", 2025, "2026-11-06", self.zip.name)
        def fake_post(url, body):
            codes = body["filters"]["award_type_codes"]
            if codes == reconcile.CONTRACT_CODES:
                return {"results": [{"shape_code": "CA", "aggregated_amount": 810.05, "population": 100},
                                    {"shape_code": "TX", "aggregated_amount": 790.0, "population": 50},
                                    {"shape_code": "DC", "aggregated_amount": 0}]}
            return {"results": []}
        with mock.patch.object(reconcile, "post", fake_post):
            result = reconcile.reconcile(summary)
        rows = {r["state"]: r for r in result["contracts"]["rows"]}
        self.assertEqual(rows["CA"]["gap_cents"], 0)
        self.assertEqual(rows["TX"]["gap_cents"], 1000)
        self.assertEqual(result["contracts"]["largest_gaps"][0]["state"], "TX")
        self.assertEqual(result["population"], {"CA": 100, "TX": 50})
        self.assertEqual(result["definition"]["time_period"], {"start_date": "2024-10-01", "end_date": "2025-09-30"})

    def test_identical_snapshots_have_no_changes(self):
        result = changes.diff_snapshots(self.root / "silver", self.root / "silver")
        self.assertEqual(result["totals"]["added"] + result["totals"]["removed"] +
                         result["totals"]["amount_changed"], 0)
        self.assertEqual(result["totals"]["unchanged"], 9)

    def test_planted_correction_and_deletion_are_detected(self):
        changed = [r for r in ROWS_A if r["contract_transaction_unique_key"] != "T3"]
        changed[0] = {**changed[0], "federal_action_obligation": "900.10"}
        second = self.root / "second.zip"
        write_zip(second, {"part_1.csv": changed + [row("T10", "5.00")], "part_2.csv": ROWS_B})
        silver.extract(second, self.root / "silver2")
        result = changes.diff_snapshots(self.root / "silver", self.root / "silver2")
        self.assertEqual(result["totals"]["removed"], 1)
        self.assertEqual(result["totals"]["added"], 1)
        self.assertEqual(result["totals"]["amount_changed"], 1)
        self.assertEqual(result["totals"]["net_change_cents"], -10000 - 50000 + 500)
        self.assertEqual(result["places"]["TX"]["removed"], 1)

    def test_delta_file_labels(self):
        delta_zip = self.root / "FY(All)_All_Contracts_Delta_20261106.zip"
        write_zip(delta_zip, {"d.csv": [
            {**row("N1", "100.00"), "correction_delete_ind": ""},
            {**row("N2", "-40.00"), "correction_delete_ind": ""},
            {**row("C1", "70.00", state="TX"), "correction_delete_ind": "C"},
            {**row("D1", "", state="", fy="", date=""), "correction_delete_ind": "D"},
            {**row("O1", "9.00", fy="2019", date="2019-01-01"), "correction_delete_ind": ""},
        ]}, delta=True)
        silver.extract(delta_zip, self.root / "delta")
        result = changes.delta_summary(self.root / "delta", [2025])
        ca = result["years"]["2025"]["CA"]
        self.assertEqual((ca["new_rows"], ca["new_cents"]), (2, 6000))
        self.assertEqual((ca["deobligation_rows"], ca["deobligation_cents"]), (1, -4000))
        self.assertEqual(result["years"]["2025"]["TX"]["corrected_rows"], 1)
        self.assertEqual(result["deletions_without_listed_year"], 1)
        self.assertNotIn("2019", result["years"])

    def test_fetch_picks_latest_file(self):
        from pipeline import fetch
        files = [{"file_name": "FY2025_All_Contracts_Full_20261006.zip", "url": "u1"},
                 {"file_name": "FY2025_All_Contracts_Full_20261106.zip", "url": "u2"},
                 {"file_name": "FY(All)_All_Contracts_Delta_20261106.zip", "url": "u3"},
                 {"file_name": "FY2025_097_Contracts_Full_20261106.zip", "url": "u4"}]
        self.assertEqual(fetch.pick(files, "Full"), {"file_name": "FY2025_All_Contracts_Full_20261106.zip",
                                                     "url": "u2", "snapshot_date": "2026-11-06"})
        self.assertEqual(fetch.pick(files, "Delta")["url"], "u3")


if __name__ == "__main__":
    unittest.main()
