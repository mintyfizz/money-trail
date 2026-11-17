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
    row("T8", "0.00", state="DC", award_type="C"),
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
        zero = pq.read_table(self.root / "silver" / "part-0001.parquet").to_pylist()[2]
        self.assertEqual(str(zero["federal_action_obligation"]), "0.00")
        self.assertEqual(zero["action_date_raw"], "2025-03-01")

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
                                            "outside_fiscal_window": 0, "null_obligations": 0, "null_dates": 0,
                                            "negative_rows": 1})
        self.assertEqual(result["deleted_key_matches"]["rows"], 0)

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
            if "geo_layer" not in body:
                return {"results": [{"name": "Department of Defense", "amount": 1100.05}]}
            if codes == reconcile.CONTRACT_CODES:
                return {"results": [{"shape_code": "CA", "aggregated_amount": 810.05, "population": 100},
                                    {"shape_code": "TX", "aggregated_amount": 790.0, "population": 50},
                                    {"shape_code": "DC", "aggregated_amount": 0}]
                        + [{"shape_code": c, "aggregated_amount": 0} for c in gold.STATES if c not in ("CA", "TX", "DC")]}
            return {"results": []}
        with mock.patch.object(reconcile, "post", fake_post):
            result = reconcile.reconcile(summary)
        rows = {r["state"]: r for r in result["contracts"]["rows"]}
        self.assertEqual(rows["CA"]["gap_cents"], 0)
        self.assertEqual(rows["TX"]["gap_cents"], 1000)
        self.assertEqual(result["contracts"]["largest_gaps"][0]["state"], "TX")
        self.assertEqual(result["contracts"]["status"], "incomplete")  # fixture has only 3 of 51 places
        self.assertEqual(result["population"], {"CA": 100, "TX": 50})
        dod = next(a for a in result["agencies"] if a["agency"] == "Department of Defense")
        self.assertEqual((dod["ours_cents"], dod["official_cents"], dod["gap_cents"]), (111005, 110005, 1000))
        self.assertEqual(result["definition"]["time_period"], {"start_date": "2024-10-01", "end_date": "2025-09-30",
                                                               "date_type": "action_date"})
        self.assertEqual(result["definition"]["spending_level"], "transactions")

    def test_identical_snapshots_have_no_changes(self):
        result = changes.diff_snapshots(self.root / "silver", self.root / "silver")
        self.assertEqual(result["totals"]["added"] + result["totals"]["removed"] +
                         result["totals"]["changed"], 0)
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
        self.assertEqual(result["totals"]["changed"], 1)
        self.assertEqual(result["totals"]["net_change_cents"], -10000 - 50000 + 500)
        self.assertEqual(result["places"]["TX"]["removed"], 1)

    def test_delta_file_labels(self):
        delta_zip = self.root / "FY(All)_All_Contracts_Delta_20261106.zip"
        tombstone = {**{c: "" for c in silver.COLUMNS}, "contract_transaction_unique_key": "D1",
                     "correction_delete_ind": "D"}
        write_zip(delta_zip, {"d.csv": [
            {**row("N1", "100.00"), "correction_delete_ind": ""},
            {**row("N2", "-40.00"), "correction_delete_ind": ""},
            {**row("C1", "70.00", state="TX"), "correction_delete_ind": "c"},
            tombstone,
            {**row("O1", "9.00", fy="2019", date="2019-01-01"), "correction_delete_ind": ""},
        ]}, delta=True)
        report = silver.extract(delta_zip, self.root / "delta")
        self.assertEqual((report["bad_money"], report["bad_dates"], report["bad_indicators"]), (0, 0, 0))
        result = changes.delta_summary(self.root / "delta", [2025])
        ca = result["years"]["2025"]["CA"]
        self.assertEqual((ca["added_or_updated_rows"], ca["obligations_on_these_rows_cents"]), (2, 6000))
        self.assertEqual((ca["deobligation_rows"], ca["deobligation_cents"]), (1, -4000))
        self.assertEqual(result["years"]["2025"]["TX"]["flagged_correction_rows"], 1)
        self.assertEqual(result["all_years"], {"added_or_updated_rows": 4, "flagged_correction_rows": 1,
                                               "deleted_rows": 1})
        self.assertNotIn("2019", result["years"])
        self.assertNotIn("new_rows", ca)
        import pyarrow.parquet as pq
        count = changes.deleted_keys(self.root / "delta", self.root / "deleted.parquet")
        self.assertEqual(count, 1)
        self.assertEqual(pq.read_table(self.root / "deleted.parquet").column(0).to_pylist(), ["D1"])

    def test_newer_delta_column_name_and_unknown_flags(self):
        newer = self.root / "newer.zip"
        columns = ["correction_delete_indicator"] + list(silver.COLUMNS)
        with zipfile.ZipFile(newer, "w") as archive:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerow({**row("A1", "1.00"), "correction_delete_indicator": "D"})
            writer.writerow({**row("A2", "1.00"), "correction_delete_indicator": "X"})
            archive.writestr("d.csv", buffer.getvalue())
        report = silver.extract(newer, self.root / "newer")
        self.assertEqual(report["members"][0]["delta_column"], "correction_delete_indicator")
        self.assertEqual(report["bad_indicators"], 1)


    def summary_of(self, rows, **kw):
        z = self.root / f"case{len(list(self.root.iterdir()))}.zip"
        write_zip(z, {"x.csv": rows})
        out = self.root / (z.stem + "_silver")
        silver.extract(z, out)
        return out

    def test_impossible_date_is_rejected_not_normalised(self):
        z = self.root / "dates.zip"
        write_zip(z, {"x.csv": [row("D1", "1.00", date="2025-02-30"), row("D2", "1.00")]})
        report = silver.extract(z, self.root / "dates")
        self.assertEqual(report["bad_dates"], 1)

    def test_blank_amount_blocks_summary(self):
        out = self.summary_of([row("B1", ""), row("B2", "5.00")])
        with self.assertRaises(gold.QualityError):
            gold.summarise(out, 2025, "2026-11-06", "x.zip")

    def test_duplicate_key_blocks_summary(self):
        out = self.summary_of([row("K1", "100.00"), row("K1", "100.00")])
        with self.assertRaisesRegex(gold.QualityError, "duplicate_keys"):
            gold.summarise(out, 2025, "2026-11-06", "x.zip")

    def test_action_date_outside_fiscal_year_blocks_summary(self):
        out = self.summary_of([row("W1", "1.00", fy="2025", date="2024-03-01")])
        with self.assertRaisesRegex(gold.QualityError, "outside_fiscal_window"):
            gold.summarise(out, 2025, "2026-11-06", "x.zip")

    def test_deleted_keys_are_reported_and_only_removed_on_request(self):
        import pyarrow as pa, pyarrow.parquet as pq
        out = self.summary_of([row("OLD_2", "440.00"), row("NEW_14", "440.00"), row("X", "10.00", state="TX")])
        deleted = self.root / "deleted.parquet"
        pq.write_table(pa.table({"contract_transaction_unique_key": ["OLD_2", "NOT_PRESENT"]}), deleted)
        reported = gold.summarise(out, 2025, "2026-11-06", "x.zip", deleted_keys=deleted)
        self.assertEqual(reported["states"]["CA"]["net_obligations_cents"], 88000)  # as published by default
        self.assertEqual(reported["deleted_key_matches"], {"rows": 1, "cents": 44000, "deleted_keys_listed": 2,
                         "applied": False, "meaning": reported["deleted_key_matches"]["meaning"]})
        result = gold.summarise(out, 2025, "2026-11-06", "x.zip", deleted_keys=deleted, apply_deleted_keys=True)
        self.assertEqual(result["states"]["CA"]["net_obligations_cents"], 44000)

    def test_reconcile_never_skips_a_state(self):
        ours = {"CA": 10000}
        official = {"CA": {"aggregated_amount": 100}, "TX": {"aggregated_amount": 900}}
        result = reconcile.compare(ours, official)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("TX", result["missing_ours"])
        self.assertEqual(len(result["rows"]), 51)

    def test_reconcile_pass_rule(self):
        ours = {c: 100000 for c in gold.STATES}
        official = {c: {"aggregated_amount": reconcile.Decimal("1000.00")} for c in gold.STATES}
        self.assertEqual(reconcile.compare(ours, official)["status"], "passed")
        ours["AL"] = 102000  # 2 percent off in one state
        self.assertEqual(reconcile.compare(ours, official)["status"], "gaps")

    def test_moved_row_changes_both_states(self):
        old = self.summary_of([row("M1", "100.00", state="CA")])
        new = self.summary_of([row("M1", "100.00", state="TX")])
        result = changes.diff_snapshots(old, new)
        self.assertEqual(result["totals"]["changed"], 1)
        self.assertEqual(result["totals"]["moved_place"], 1)
        self.assertEqual(result["places"]["CA"]["net_change_cents"], -10000)
        self.assertEqual(result["places"]["TX"]["net_change_cents"], 10000)

    def test_reserve_is_checked_before_writing(self):
        from pipeline import guard
        with mock.patch.object(guard, "free_bytes", return_value=8 * guard.GIB + 10):
            with self.assertRaises(RuntimeError):
                guard.require(self.root, 100, 8)
        z = self.root / "space.zip"
        write_zip(z, {"x.csv": [row("S1", "1.00")]})
        with mock.patch("pipeline.guard.free_bytes", return_value=0):
            with self.assertRaises(RuntimeError):
                silver.extract(z, self.root / "space")


    def test_guarded_writer_counts_parquet_footer(self):
        from pipeline import guard
        z = self.root / "tiny.zip"
        write_zip(z, {"x.csv": [row("G1", "1.00")]})
        # 2 KB above the reserve: more than one row's in-memory size, less than a Parquet file.
        with mock.patch("pipeline.guard.free_bytes", return_value=8 * guard.GIB + 2048):
            with self.assertRaises(RuntimeError):
                silver.extract(z, self.root / "tiny")

    def test_spill_allowance_respects_reserve(self):
        from pipeline import guard
        with mock.patch("pipeline.guard.free_bytes", return_value=int(8.5 * guard.GIB)):
            self.assertEqual(guard.spill_allowance(self.root, 8), guard.GIB // 2)
        with mock.patch("pipeline.guard.free_bytes", return_value=8 * guard.GIB + 1024):
            with self.assertRaises(RuntimeError):
                guard.spill_allowance(self.root, 8)

    def test_same_place_correction_counts_once(self):
        old = self.summary_of([row("C9", "100.00", state="CA")])
        new = self.summary_of([row("C9", "90.00", state="CA")])
        result = changes.diff_snapshots(old, new)
        self.assertEqual(result["places"]["CA"]["changed"], 1)
        self.assertEqual(result["places"]["CA"]["net_change_cents"], -1000)
        self.assertIn("place of performance only", result["scope"])

    def test_delta_rows_other_than_deletions_need_amounts(self):
        delta_zip = self.root / "badDelta.zip"
        write_zip(delta_zip, {"d.csv": [{**row("N1", ""), "correction_delete_ind": ""}]}, delta=True)
        silver.extract(delta_zip, self.root / "bad_delta")
        with self.assertRaises(changes.DeltaQualityError):
            changes.delta_summary(self.root / "bad_delta", [2025])

    def test_undated_delta_rows_are_listed_not_counted(self):
        delta_zip = self.root / "undated.zip"
        write_zip(delta_zip, {"d.csv": [{**row("U1", "5.00", fy="", date=""), "correction_delete_ind": ""},
                                        {**row("N1", "1.00"), "correction_delete_ind": ""}]}, delta=True)
        silver.extract(delta_zip, self.root / "undated")
        result = changes.delta_summary(self.root / "undated", [2025])
        self.assertEqual([r["key"] for r in result["incomplete_rows_outside_any_year"]], ["U1"])
        self.assertEqual(result["years"]["2025"]["CA"]["added_or_updated_rows"], 1)

    def test_stale_audit_file_and_full_totals(self):
        import pyarrow as pa, pyarrow.parquet as pq
        out = self.summary_of([row("OLD_2", "440.00"), row("NEW_14", "440.00")])
        deleted = self.root / "deleted2.parquet"
        pq.write_table(pa.table({"contract_transaction_unique_key": ["OLD_2"]}), deleted)
        audit = self.root / "stale.parquet"
        result = gold.summarise(out, 2025, "2026-11-06", "x.zip", deleted_keys=deleted, stale_out=audit)
        self.assertEqual(result["full_snapshot"]["net_obligations_cents"], 88000)
        self.assertEqual(result["national"]["net_obligations_cents"], 88000)
        rows = pq.read_table(audit).to_pylist()
        self.assertEqual([r["contract_transaction_unique_key"] for r in rows], ["OLD_2"])
        self.assertEqual(rows[0]["source_row"], 2)

    def test_release_asset_names_match_github(self):
        from pipeline import release
        self.assertEqual(release.asset_name("FY(All)_All_Contracts_Delta_20261106.zip"),
                         "FY.All._All_Contracts_Delta_20261106.zip")
        self.assertEqual(release.asset_name("part-0001.parquet"), "part-0001.parquet")

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
