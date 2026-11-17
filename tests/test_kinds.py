"""Category map, per-kind column contracts and assistance extraction."""
import csv
import io
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest import mock

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


def assistance_silver(root, rows, name="s", **kw):
    z = Path(root) / f"{name}.zip"
    write_assistance_zip(z, rows, **kw)
    silver.extract(z, Path(root) / name, kind="assistance")
    return Path(root) / name


class AssistanceGoldTests(unittest.TestCase):
    def setUp(self):
        from pipeline import gold
        self.gold = gold
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def summary(self, rows, **kw):
        self.calls = getattr(self, "calls", 0) + 1
        silver_dir = assistance_silver(self.root, rows, name=f"s{self.calls}", **kw)
        return self.gold.summarise(silver_dir, 2025, "2026-11-06", "a.zip", kind="assistance")

    def test_pragmatic_measure_place_and_categories(self):
        r = self.summary(ASSISTANCE_ROWS)
        self.assertEqual(r["kind"], "assistance")
        # Loans count their subsidy cost; every other row its obligation. Face value is never added.
        self.assertEqual(r["national"]["amount_cents"], 261931)
        self.assertEqual(r["national"]["net_obligations_cents"], 138475)
        cats = {c["category"]: c for c in r["national"]["categories"]}
        self.assertEqual(cats["grants"]["amount_cents"], 96975)
        self.assertEqual(cats["loans"]["amount_cents"], 123456)
        self.assertEqual(cats["loans"]["face_value_cents"], 25000000)
        self.assertNotIn("face_value_cents", cats["grants"])
        self.assertEqual(cats["direct_payments"]["amount_cents"], 37500)
        self.assertEqual(cats["not_specified"]["amount_cents"], 4000)  # source -1 kept as its own bucket
        self.assertEqual(r["states"]["CA"]["amount_cents"], 234956)
        self.assertEqual(r["states"]["TX"]["amount_cents"], -5025)
        self.assertEqual(r["outside_map"]["territory_cents"], 30000)
        self.assertEqual(r["outside_map"]["territories"][0]["code"], "PR")
        # Marshall Islands: freely associated, counted as foreign, listed by name.
        self.assertEqual(r["outside_map"]["foreign_cents"], 2000)
        self.assertEqual(r["outside_map"]["freely_associated_states"][0]["code"], "MH")
        self.assertEqual(r["checks"]["pragmatic_mismatch_rows"], 0)
        self.assertEqual(r["checks"]["pragmatic_null_rows"], 0)
        self.assertEqual(r["checks"]["rows_without_amount"], 0)

    def test_aggregate_and_unidentified_recipients_are_not_ranked(self):
        ca = self.summary(ASSISTANCE_ROWS)["states"]["CA"]
        self.assertEqual([x["uei"] for x in ca["recipients"]], ["UEIA"])
        self.assertEqual(ca["recipients"][0]["amount_cents"], 227456)
        self.assertEqual(ca["recipients_not_ranked"]["amount_cents"], 7500)
        self.assertEqual(ca["recipients_not_ranked"]["aggregate_records"], 1)
        redacted = self.summary([assistance_row("X1", "5.00", uei="UEIZ", record_type="3")])["states"]["CA"]
        self.assertEqual(redacted["recipients"], [])
        self.assertEqual(redacted["recipients_not_ranked"]["redacted_records"], 1)

    def test_full_matrix_adds_up_to_every_total(self):
        r = self.summary(ASSISTANCE_ROWS)
        cols = r["matrix"]["columns"]
        rows = [dict(zip(cols, x)) for x in r["matrix"]["rows"]]
        self.assertEqual(sum(x["amount_cents"] for x in rows), r["national"]["amount_cents"])
        ca = {(x["agency"], x["category"]): x["amount_cents"] for x in rows if x["place"] == "CA"}
        self.assertEqual(ca[("Small Business Administration", "loans")], 123456)
        self.assertEqual(ca[("Department of Education", "not_specified")], 4000)
        self.assertEqual(sum(ca.values()), r["states"]["CA"]["amount_cents"])
        loan = next(x for x in rows if x["category"] == "loans")
        self.assertEqual(loan["face_value_cents"], 25000000)
        self.assertEqual({x["place"] for x in rows if x["place_kind"] == "foreign"}, {"foreign"})

    def test_unrecognised_type_code_fails(self):
        with self.assertRaisesRegex(kinds.UnmappedCodeError, "F099"):
            self.summary([assistance_row("N1", "1.00", "F099")])

    def test_missing_amount_is_disclosed_not_blocking(self):
        r = self.summary([assistance_row("B1", ""), assistance_row("B2", "10.00")])
        self.assertEqual(r["checks"]["rows_without_amount"], 1)
        self.assertEqual(r["national"]["amount_cents"], 1000)
        self.assertEqual(r["states"]["CA"]["categories"][0]["rows_without_amount"], 1)

    def test_unknown_fips_goes_to_unknown_and_is_listed(self):
        r = self.summary([assistance_row("F1", "7.00", fips="99"), assistance_row("F2", "3.00", fips="6")])
        self.assertEqual(r["checks"]["unrecognised_state_fips_rows"], 1)
        self.assertEqual(r["outside_map"]["unknown_cents"], 700)
        self.assertEqual(r["unrecognised_state_fips"], [{"fips": "99", "rows": 1, "amount_cents": 700}])
        self.assertEqual(r["states"]["CA"]["amount_cents"], 300)  # one-digit FIPS is padded

    def test_pragmatic_cross_check_separates_nulls_from_mismatches(self):
        r = self.summary([assistance_row("P1", "1000.00", pragmatic="999.00"),
                          assistance_row("P2", "5.00", pragmatic=""),
                          assistance_row("P3", "0.00", "07", subsidy="12.00", pragmatic="12.00")])
        self.assertEqual(r["checks"]["pragmatic_mismatch_rows"], 1)
        self.assertEqual(r["checks"]["pragmatic_mismatch_cents"], -100)
        self.assertEqual(r["checks"]["pragmatic_null_rows"], 1)
        absent = self.gold.summarise(assistance_silver(self.root, ASSISTANCE_ROWS, name="np", pragmatic=False),
                                     2025, "2026-11-06", "a.zip", kind="assistance")
        self.assertIsNone(absent["checks"]["pragmatic_mismatch_rows"])

    def test_source_correction_code_is_not_the_delta_marker(self):
        z = self.root / "full.zip"
        write_assistance_zip(z, [assistance_row("C1", "1.00", correction_delete_indicator_code="C")],
                             delta_column="correction_delete_indicator_code")
        report = silver.extract(z, self.root / "full", kind="assistance")
        self.assertIsNone(report["members"][0]["delta_column"])

    def test_optional_columns_are_tracked_per_member(self):
        z = self.root / "mixed.zip"
        with zipfile.ZipFile(z, "w") as archive:
            for i, pragmatic in enumerate((True, False)):
                columns = list(kinds.ASSISTANCE.columns) + (["generated_pragmatic_obligations"] if pragmatic else [])
                buffer = io.StringIO()
                writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
                writer.writeheader()
                writer.writerow(assistance_row(f"K{i}", "2.00", pragmatic="2.00" if pragmatic else None))
                archive.writestr(f"FY2025_All_Assistance_Full_20261106_{i + 1}.csv", buffer.getvalue())
        report = silver.extract(z, self.root / "mixed", kind="assistance")
        self.assertEqual(report["optional_columns_present"], [])
        self.assertFalse(report["optional_columns_consistent"])
        self.assertEqual(report["members"][0]["optional_columns"], ["generated_pragmatic_obligations"])
        r = self.gold.summarise(self.root / "mixed", 2025, "2026-11-06", "m.zip", kind="assistance")
        self.assertEqual(r["national"]["amount_cents"], 400)
        self.assertEqual(r["checks"]["pragmatic_null_rows"], 1)  # the member without the column


class ContractsPlaceTests(unittest.TestCase):
    def test_freely_associated_contracts_are_foreign(self):
        from pipeline import gold
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            columns = list(kinds.CONTRACTS.columns)
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            for key, state, country in (("K1", "FM", "FSM"), ("K2", "PW", "USA"), ("K3", "GU", "USA")):
                writer.writerow({**{c: "" for c in columns}, "contract_transaction_unique_key": key,
                                 "contract_award_unique_key": f"A_{key}", "federal_action_obligation": "10.00",
                                 "action_date": "2025-03-01", "action_date_fiscal_year": "2025",
                                 "primary_place_of_performance_state_code": state,
                                 "primary_place_of_performance_country_code": country, "award_type_code": "D"})
            with zipfile.ZipFile(root / "c.zip", "w") as archive:
                archive.writestr("FY2025_All_Contracts_Full_20261106_1.csv", buffer.getvalue())
            silver.extract(root / "c.zip", root / "c")
            r = gold.summarise(root / "c", 2025, "2026-11-06", "c.zip")
            self.assertEqual(r["outside_map"]["foreign_cents"], 2000)
            self.assertEqual(r["outside_map"]["territory_cents"], 1000)
            self.assertEqual({x["code"] for x in r["outside_map"]["freely_associated_states"]}, {"FM", "PW"})


class AssistanceReconcileTests(unittest.TestCase):
    def test_each_category_compared_alone_with_outside_places_kept_apart(self):
        from pipeline import gold, reconcile
        with tempfile.TemporaryDirectory() as tmp:
            summary = gold.summarise(assistance_silver(tmp, ASSISTANCE_ROWS), 2025, "2026-11-06", "a.zip",
                                     kind="assistance")
        data = kinds.load_categories()
        calls = []

        def fake_post(url, body):
            codes = body["filters"]["award_type_codes"]
            calls.append((url, tuple(codes), body.get("page")))
            if "geo_layer" not in body:
                page = body["page"]
                results = ([{"name": "Department of Education", "amount": 900.00}] if page == 1 else
                           [{"name": "Small Business Administration", "amount": 1234.56}])
                return {"results": results, "page_metadata": {"hasNext": page == 1}}
            if codes == data["api_codes"]["grants"]:
                return {"results": [{"shape_code": "CA", "aggregated_amount": 1000.00, "population": 10},
                                    {"shape_code": "TX", "aggregated_amount": -50.25},
                                    {"shape_code": "PR", "aggregated_amount": 5.00},
                                    {"shape_code": "", "aggregated_amount": 7.00}]
                        + [{"shape_code": c, "aggregated_amount": 0} for c in gold.STATES if c not in ("CA", "TX")]}
            return {"results": []}

        with mock.patch.object(reconcile, "post", fake_post):
            result = reconcile.reconcile(summary)
        self.assertEqual(result["categories"], ["grants", "direct_payments", "loans", "other_assistance",
                                                "not_specified"])
        grants = {r["state"]: r for r in result["grants"]["rows"]}
        self.assertEqual(grants["CA"]["gap_cents"], 0)
        self.assertEqual(grants["TX"]["gap_cents"], 0)
        self.assertEqual(result["official_outside_states"]["grants"], {"PR": 500, "(blank)": 700})
        self.assertEqual(result["grants"]["total_official_cents"], 94975)  # PR and blank never enter
        self.assertEqual(result["not_specified"]["status"], "no official figure")
        self.assertEqual(result["not_specified"]["total_ours_cents"], 4000)
        self.assertFalse(any(codes == ("-1",) for _, codes, _ in calls))
        agencies = {a["agency"]: a for a in result["agencies"]}
        self.assertEqual(agencies["Small Business Administration"]["gap_cents"], 0)  # second page was read
        self.assertNotIn("not_specified", result["agency_categories"])
