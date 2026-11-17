"""Category map, per-kind column contracts and assistance extraction."""
import csv
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from pipeline import kinds, silver


def assistance_row(key, obligation, type_code="04", fips="06", country="USA", agency="Department of Education",
                   uei="UEIA", name="State University", record_type="2", face="", subsidy="", fy="2025",
                   date="2025-03-01", pragmatic=None, award=None, **extra):
    values = {c: "" for c in kinds.ASSISTANCE.columns}
    values.update({
        "assistance_transaction_unique_key": key, "assistance_award_unique_key": award or f"ASST_NON_{key}",
        "federal_action_obligation": obligation, "face_value_of_loan": face, "original_loan_subsidy_cost": subsidy,
        "action_date": date, "action_date_fiscal_year": fy, "awarding_agency_name": agency,
        "recipient_uei": uei, "recipient_name": name, "record_type_code": record_type,
        "primary_place_of_performance_country_code": country,
        "prime_award_transaction_place_of_performance_state_fips_code": fips,
        "assistance_type_code": type_code, "cfda_number": "84.007",
        "prime_award_base_transaction_description": "Student aid"})
    if pragmatic is not None:
        values["generated_pragmatic_obligations"] = pragmatic
    values.update(extra)
    return values


def write_assistance_zip(path, rows, pragmatic=True, delta_column=None):
    columns = ([delta_column] if delta_column else []) + list(kinds.ASSISTANCE.columns)
    columns += ["generated_pragmatic_obligations"] if pragmatic else []
    columns += ["unused_column"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({**{c: "" for c in columns}, **r, "unused_column": "x"})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("FY2025_All_Assistance_Full_20261106_1.csv", buffer.getvalue())


ASSISTANCE_ROWS = [
    assistance_row("G1", "1000.00", "04", pragmatic="1000.00"),
    assistance_row("G2", "-50.25", "F001", fips="48", pragmatic="-50.25"),
    assistance_row("L1", "0.00", "07", face="250000.00", subsidy="1234.56", pragmatic="1234.56",
                   agency="Small Business Administration"),
    assistance_row("P1", "300.00", "10", fips="72", pragmatic="300.00"),
    assistance_row("U1", "40.00", "-1", pragmatic="40.00"),
    assistance_row("R1", "75.00", "06", uei="", name="MULTIPLE RECIPIENTS", record_type="1", pragmatic="75.00"),
    assistance_row("M1", "20.00", "02", fips="68", country="MHL", pragmatic="20.00"),
]


class CategoryTests(unittest.TestCase):
    def test_map_loads_and_is_consistent(self):
        data = kinds.load_categories()
        self.assertEqual(data["version"], "2026-11-18")
        self.assertEqual(kinds.category_of("assistance", "F003", data), "loans")
        self.assertEqual(kinds.category_of("assistance", "05", data), "grants")
        self.assertEqual(kinds.category_of("contracts", "c", data), "contracts")
        self.assertEqual(kinds.category_of("contracts", "IDV", data), "idvs")

    def test_minus_one_is_kept_as_not_specified(self):
        self.assertEqual(kinds.category_of("assistance", "-1"), "not_specified")

    def test_unrecognised_codes_fail(self):
        with self.assertRaises(kinds.UnmappedCodeError):
            kinds.category_of("assistance", "F099")
        with self.assertRaises(kinds.UnmappedCodeError):
            kinds.category_of("contracts", "E")

    def test_every_api_code_list_matches_the_map(self):
        data = kinds.load_categories()
        for category, codes in data["api_codes"].items():
            kind = data["categories"][category]["kind"]
            for code in codes:
                if kind == "contracts" and code.startswith("IDV"):
                    continue
                self.assertEqual(kinds.category_of(kind, code, data), category)

    def test_category_pointing_to_wrong_kind_is_rejected(self):
        import json
        bad = json.loads(kinds.CATEGORIES_FILE.read_text())
        bad["codes"]["assistance"]["04"] = "contracts"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(bad, handle)
        with self.assertRaises(ValueError):
            kinds.load_categories(handle.name)
        Path(handle.name).unlink()

    def test_fips_table_keeps_leading_zeros_and_freely_associated_states(self):
        self.assertEqual(kinds.STATE_FIPS["06"], "CA")
        self.assertEqual(kinds.STATE_FIPS["11"], "DC")
        self.assertEqual(len([v for v in kinds.STATE_FIPS.values()
                              if v not in ("AS", "GU", "MP", "PR", "UM", "VI", "FM", "MH", "PW")]), 51)
        self.assertEqual(kinds.FREELY_ASSOCIATED, {"FM", "MH", "PW"})


class AssistanceExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_assistance_money_columns_are_typed_with_raw_kept(self):
        import pyarrow.parquet as pq
        z = self.root / "a.zip"
        write_assistance_zip(z, ASSISTANCE_ROWS)
        report = silver.extract(z, self.root / "a", kind="assistance")
        self.assertEqual((report["rows"], report["bad_money"], report["bad_dates"], report["blank_keys"]), (7, 0, 0, 0))
        self.assertEqual(report["kind"], "assistance")
        self.assertEqual(report["optional_columns_present"], ["generated_pragmatic_obligations"])
        rows = {r["assistance_transaction_unique_key"]: r for r in pq.read_table(self.root / "a" / "part-0000.parquet").to_pylist()}
        loan = rows["L1"]
        self.assertEqual(str(loan["face_value_of_loan"]), "250000.00")
        self.assertEqual(str(loan["original_loan_subsidy_cost"]), "1234.56")
        self.assertEqual(loan["original_loan_subsidy_cost_raw"], "1234.56")
        self.assertIsNone(rows["G1"]["face_value_of_loan"])  # blank stays blank, never zero
        self.assertEqual(rows["M1"]["prime_award_transaction_place_of_performance_state_fips_code"], "68")
        self.assertEqual(rows["G1"]["prime_award_transaction_place_of_performance_state_fips_code"], "06")

    def test_assistance_without_optional_pragmatic_column(self):
        z = self.root / "b.zip"
        write_assistance_zip(z, ASSISTANCE_ROWS, pragmatic=False)
        report = silver.extract(z, self.root / "b", kind="assistance")
        self.assertEqual(report["optional_columns_present"], [])

    def test_bad_subsidy_cost_is_counted(self):
        z = self.root / "c.zip"
        write_assistance_zip(z, [assistance_row("L2", "0.00", "08", subsidy="12,5")])
        report = silver.extract(z, self.root / "c", kind="assistance")
        self.assertEqual(report["bad_money"], 1)

    def test_contract_columns_are_not_accepted_as_assistance(self):
        z = self.root / "d.zip"
        with zipfile.ZipFile(z, "w") as archive:
            archive.writestr("x.csv", ",".join(silver.COLUMNS) + "\n" + ",".join("x" for _ in silver.COLUMNS) + "\n")
        with self.assertRaisesRegex(ValueError, "missing expected assistance columns"):
            silver.extract(z, self.root / "d", kind="assistance")

    def test_assistance_delta_with_newer_indicator_name(self):
        import pyarrow.parquet as pq
        z = self.root / "e.zip"
        write_assistance_zip(z, [{**assistance_row("D1", ""), "correction_delete_indicator": "D"},
                                 {**assistance_row("N1", "5.00"), "correction_delete_indicator": ""}],
                             delta_column="correction_delete_indicator")
        report = silver.extract(z, self.root / "e", kind="assistance")
        self.assertEqual(report["members"][0]["delta_column"], "correction_delete_indicator")
        rows = pq.read_table(self.root / "e" / "part-0000.parquet").to_pylist()
        self.assertEqual([r["correction_delete_ind"] for r in rows], ["D", ""])


if __name__ == "__main__":
    unittest.main()
