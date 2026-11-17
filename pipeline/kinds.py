"""Per-kind column contracts and the versioned award-type → category map.

Two kinds of award archive exist: contracts and financial assistance. They have different keys,
money columns and place columns, so each kind declares exactly which source columns the typed
layer keeps and which of them hold money or dates. Anything an archive lacks fails the build.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

CATEGORIES_FILE = Path(__file__).with_name("categories.json")


@dataclass(frozen=True)
class Kind:
    name: str
    key: str                      # transaction key, unique per row
    award_key: str                # award key, shared by an award's transactions
    columns: tuple[str, ...]      # required source columns, kept as typed text
    money: tuple[str, ...]        # columns cast to exact decimals (blank = null, bad = counted)
    dates: tuple[str, ...] = ("action_date",)
    optional: tuple[str, ...] = field(default=())  # kept when present, never required


CONTRACTS = Kind(
    name="contracts",
    key="contract_transaction_unique_key",
    award_key="contract_award_unique_key",
    columns=(
        "contract_transaction_unique_key", "contract_award_unique_key", "award_id_piid", "modification_number",
        "federal_action_obligation", "action_date", "action_date_fiscal_year",
        "awarding_agency_code", "awarding_agency_name", "awarding_sub_agency_name",
        "funding_agency_code", "funding_agency_name",
        "recipient_uei", "recipient_name", "recipient_parent_uei", "recipient_parent_name",
        "primary_place_of_performance_country_code", "primary_place_of_performance_state_code",
        "primary_place_of_performance_state_name", "prime_award_transaction_place_of_performance_county_fips_code",
        "primary_place_of_performance_county_name",
        "award_type_code", "award_type", "idv_type_code", "idv_type", "action_type_code",
        "naics_code", "naics_description", "product_or_service_code_description",
        "prime_award_base_transaction_description", "last_modified_date",
    ),
    money=("federal_action_obligation",),
)

ASSISTANCE = Kind(
    name="assistance",
    key="assistance_transaction_unique_key",
    award_key="assistance_award_unique_key",
    columns=(
        "assistance_transaction_unique_key", "assistance_award_unique_key", "award_id_fain", "award_id_uri",
        "modification_number", "federal_action_obligation", "face_value_of_loan", "original_loan_subsidy_cost",
        "action_date", "action_date_fiscal_year",
        "awarding_agency_code", "awarding_agency_name", "awarding_sub_agency_name",
        "funding_agency_code", "funding_agency_name",
        "recipient_uei", "recipient_name", "recipient_parent_uei", "recipient_parent_name",
        "primary_place_of_performance_country_code", "primary_place_of_performance_state_name",
        "prime_award_transaction_place_of_performance_state_fips_code",
        "prime_award_transaction_place_of_performance_county_fips_code", "primary_place_of_performance_county_name",
        "assistance_type_code", "assistance_type_description", "record_type_code",
        "cfda_number", "cfda_title", "prime_award_base_transaction_description", "last_modified_date",
    ),
    money=("federal_action_obligation", "face_value_of_loan", "original_loan_subsidy_cost",
           "generated_pragmatic_obligations"),
    optional=("generated_pragmatic_obligations", "primary_place_of_performance_state_code",
              "primary_place_of_performance_scope"),
)

KINDS = {k.name: k for k in (CONTRACTS, ASSISTANCE)}


class UnmappedCodeError(ValueError):
    """An award type code the category map does not know. The build stops rather than guess."""


def load_categories(path: str | Path = CATEGORIES_FILE) -> dict:
    data = json.loads(Path(path).read_text())
    for kind, codes in data["codes"].items():
        for code, category in codes.items():
            if category not in data["categories"]:
                raise ValueError(f"{kind} code {code} maps to undefined category {category}")
            if data["categories"][category]["kind"] != kind:
                raise ValueError(f"{kind} code {code} maps to a {data['categories'][category]['kind']} category")
    return data


def category_of(kind: str, code: str, categories: dict | None = None) -> str:
    """The category for one source code. Contracts use 'IDV' for a blank award type with an IDV type."""
    categories = categories or load_categories()
    code = (code or "").strip().upper() if kind == "contracts" else (code or "").strip()
    try:
        return categories["codes"][kind][code]
    except KeyError:
        raise UnmappedCodeError(f"Unrecognised {kind} award type code {code!r}; add it to categories.json "
                                "with a cited source") from None


# 2-digit state FIPS → postal code. Kept as text; leading zeros matter.
STATE_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY",
    # Territories
    "60": "AS", "66": "GU", "69": "MP", "72": "PR", "74": "UM", "78": "VI",
    # Freely associated states: Treasury now classifies them as foreign (About the Data, pp 9-10)
    "64": "FM", "68": "MH", "70": "PW",
}
FREELY_ASSOCIATED = {"FM", "MH", "PW"}
