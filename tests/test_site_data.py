"""Site data: every loaded scope's full matrix, explicit not-loaded kinds, coverage index."""
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from pipeline import gold, kinds, silver, site_data
from tests.test_kinds import ASSISTANCE_ROWS, assistance_silver


def contracts_silver(root, year=2025):
    columns = list(kinds.CONTRACTS.columns)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for key, state, amount, agency in (("K1", "CA", "100.00", "Department of Defense"),
                                       ("K2", "TX", "50.00", "Department of Education"),
                                       ("K3", "GU", "7.00", "Department of Defense")):
        writer.writerow({**{c: "" for c in columns}, "contract_transaction_unique_key": key,
                         "contract_award_unique_key": f"A_{key}", "federal_action_obligation": amount,
                         "action_date": f"{year}-03-01", "action_date_fiscal_year": str(year),
                         "awarding_agency_code": "097" if "Defense" in agency else "091",
                         "awarding_agency_name": agency, "recipient_uei": "U" + key, "recipient_name": "Co " + key,
                         "primary_place_of_performance_state_code": state,
                         "primary_place_of_performance_country_code": "USA", "award_type_code": "D"})
    z = Path(root) / f"c{year}.zip"
    with zipfile.ZipFile(z, "w") as archive:
        archive.writestr(f"FY{year}_All_Contracts_Full_20261106_1.csv", buffer.getvalue())
    silver.extract(z, Path(root) / f"c{year}")
    return Path(root) / f"c{year}"


class SiteDataTests(unittest.TestCase):
    def test_combines_kinds_and_marks_missing_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scopes = {
                "scope-contracts-fy2025": gold.summarise(contracts_silver(root), 2025, "2026-11-06", "c.zip"),
                "scope-assistance-fy2025": gold.summarise(assistance_silver(root, ASSISTANCE_ROWS), 2025,
                                                          "2026-11-06", "a.zip", kind="assistance"),
                "scope-contracts-fy2024": gold.summarise(contracts_silver(root, 2024), 2024, "2026-11-06", "c4.zip"),
            }
            for name, summary in scopes.items():
                (root / "in" / name).mkdir(parents=True)
                (root / "in" / name / "summary.json").write_text(json.dumps(summary))
            (root / "in" / "scope-assistance-fy2025" / "provenance.json").write_text(json.dumps(
                {"source_release": "source-a", "build_release": "build-a", "code_commit": "a" * 40}))
            index = site_data.build(root / "in", root / "out")
            fy21 = json.loads((root / "out" / "fy2021.json").read_text())
            fy25 = json.loads((root / "out" / "fy2025.json").read_text())
            fy24 = json.loads((root / "out" / "fy2024.json").read_text())
        # Every target year is listed; years with nothing loaded say so rather than disappear.
        self.assertEqual([y["fiscal_year"] for y in index["years"]], [2021, 2022, 2023, 2024, 2025])
        self.assertEqual(index["years"][0]["kinds"]["contracts"], {"loaded": False, "reason": site_data.NOT_LOADED})
        self.assertEqual(index["years"][3]["kinds"]["assistance"], {"loaded": False, "reason": site_data.NOT_LOADED})
        self.assertEqual(fy21["cells"], [])
        self.assertEqual(fy25["kinds"]["assistance"]["provenance"]["build_release"], "build-a")
        self.assertTrue(fy25["kinds"]["assistance"]["loaded"])
        self.assertFalse(fy24["kinds"]["assistance"]["loaded"])
        cols = fy25["cell_columns"]
        cells = [dict(zip(cols, c)) for c in fy25["cells"]]
        total = sum(c["amount_cents"] for c in cells)
        self.assertEqual(total, scopes["scope-contracts-fy2025"]["national"]["amount_cents"]
                         + scopes["scope-assistance-fy2025"]["national"]["amount_cents"])
        ca = sum(c["amount_cents"] for c in cells if c["place"] == "CA")
        self.assertEqual(ca, 10000 + 234956)
        loans = [c for c in cells if fy25["category_ids"][c["category"]] == "loans"]
        self.assertEqual(loans[0]["face_value_cents"], 25000000)
        self.assertIn("contracts", fy25["states"]["CA"]["by_kind"])
        self.assertIn("assistance", fy25["states"]["CA"]["by_kind"])
        self.assertNotIn("assistance", fy24["states"]["CA"]["by_kind"])

    def test_two_summaries_for_one_scope_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = gold.summarise(contracts_silver(root), 2025, "2026-11-06", "c.zip")
            for name in ("a", "b"):
                (root / "in" / name).mkdir(parents=True)
                (root / "in" / name / "summary.json").write_text(json.dumps(summary))
            with self.assertRaises(SystemExit):
                site_data.build(root / "in", root / "out")
