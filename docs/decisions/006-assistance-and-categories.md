# 006 — Assistance alongside contracts: categories, measure, place, preservation

Date: 2026-11-18. Status: accepted for FY2021–2025 (contracts and financial assistance).
Source checks for this decision were made independently by Codex: real FY2021–2025 assistance
headers (116 columns, identical across years), the delta header (117 columns), archive sizes, and
Treasury's own `transaction_search` dataframe code.

## Categories

`pipeline/categories.json` (versioned, with sources) maps every award type code to one category:
contracts, IDVs, grants, direct payments, loans, insurance and other assistance, and
"assistance type not specified" for source code `-1`. Known codes include both the legacy
numeric codes and the newer `F0xx` codes, which appear together in real archives. `-1` is kept as
its own disclosed bucket, never folded into another category. A code the map does not know stops
the build: it has to be added with a cited source, not guessed.

## Measure

The same rule Treasury uses for its own search totals: loans count `original_loan_subsidy_cost`
(the government's expected cost); everything else counts `federal_action_obligation`. Loan face
value is reported beside the subsidy cost and is never added to any total. All money is integer
cents end to end.

An assistance row without an amount is counted per category and shown, and adds zero, as in
Treasury's rule. Malformed money is different: it still fails the build. When the source carries
`generated_pragmatic_obligations`, every row is compared with our measure; rows where that
column is null are counted apart from rows where it disagrees, and a member without the column is
reported as not checked.

## Place

Place of performance, not the recipient's address. Assistance place comes from the state FIPS
code (padded to two digits); an unrecognised FIPS goes to "unknown" and is listed. Micronesia, the
Marshall Islands and Palau are freely associated states, which Treasury classifies as foreign; they
count as foreign for both kinds and are named in the outside-the-map figures. Reconciliation uses
the 50 states and DC only; territories and the API's blank-place bucket are reported beside it.

## Agencies and recipients

Awarding agency only (by code), stated as such. A full agency × place × category matrix is
emitted and gated to add up exactly to national, state and state-category totals; it is the only
source for agency filters. Top-N lists are for display only.
Aggregate records (record type 1), privacy-redacted records (type 3) and rows without a recipient
UEI are never ranked as a recipient; their total is shown beside the ranking.

## Preservation: rebuild versus refresh

- **Refresh** finds what Treasury publishes now, downloads it, and preserves it in a source release
  named by kind, year, snapshot date and SHA-256 prefix. Changed bytes under the same file name get
  a new identifier; an existing release is checked, never overwritten.
- **Rebuild** reproduces a preserved snapshot from its release only, verifying every part, the
  whole file's size and SHA-256 and the source file name. It never contacts Treasury, so a
  snapshot stays reproducible after Treasury replaces it.
- A source file at or above GitHub's per-asset limit (FY2021 assistance: 2,733,086,438 bytes) is
  preserved as exact byte ranges of the original plus a manifest of the whole file's size and
  SHA-256 and each part's offset, size and SHA-256. Parts are uploaded straight from the original,
  so this needs no extra disk; rebuilding keeps only one part on disk at a time. This keeps the
  national file whole and exact, rather than replacing it with many per-agency downloads.

One request is one kind and one year (`pipeline/run_request.py`); runs are sequential.
