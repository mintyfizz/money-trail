# 005 — National scope: typed layer streamed from a retained ZIP, gated aggregates

Date: 2026-11-17. Status: accepted for the FY2025 national contracts pilot.

## Decision

For national archives, the runner streams each CSV member directly from the checksum-verified
original ZIP into a typed transaction Parquet of 31 analysis columns. The full text-Parquet copy
from decision 003 is not built at national scope. The original ZIP is preserved in a release next to
the typed layer, so the typed layer can always be rebuilt from the exact source bytes.

This is a bounded-storage design choice, not an observed failure to fit. National text-Parquet
size is unmeasured; the Library of Congress ratio (5.83×) is not representative.

Every typed row keeps `federal_action_obligation_raw`, `action_date_raw`, `source_file` and
`source_row`, so any typed value traces to its source cell.

## Gates (any failure stops the build)

- Unparseable money or dates, or unknown delta indicators: counted, kept as null, and the run fails.
  Dates must round-trip to their exact source text, so impossible dates such as 2025-02-30 fail.
- Aggregation refuses: null amounts or dates, duplicate transaction keys, a source fiscal year that
  differs from the run, or an action date outside the fiscal window. Missing money is never zero.
- Disk: exact free bytes are checked before each download (Content-Length plus 8 GiB) and before
  each Parquet batch write. DuckDB spill is capped at 2 GiB.
- Releases are created as drafts, every asset's size and SHA-256 is checked against the runner's
  files, and only then published. An existing tag is never overwritten; tags carry the code SHA.

## Stale keys in the full file

The 2026-11-06 FY2025 full file contains transactions whose keys Treasury re-issued on
2026-11-04 (transaction number 2 became 14, 1 became 16) alongside the new keys. USAspending's
live API lists only the new keys. Alabama's +$1.92B reconciliation gap was entirely Army delivery
orders of this kind (example award `CONT_AWD_W31P4Q25F0017_9700_W31P4Q22D0004_9700`).

The same release's delta file lists deleted keys. The build removes those keys from the full file
and reports how many rows and how many cents were removed. The delta and full file must carry the
same snapshot date. Dropping repeated actions by key prefix was tested and rejected: most repeated
actions under one modification are legitimate, and that rule removed $66.4B.

## Reconciliation rule

Each of the 50 states and DC is compared with `/api/v2/search/spending_by_geography/` using
`spending_level: transactions`, an inclusive action-date window and place of performance. Contract
codes A–D and indefinite-delivery vehicles are compared separately. Amounts are parsed as exact
decimals. A place missing on either side makes the result `incomplete`.

Rule fixed before the corrected run: `passed` only if all 51 are present, the national gap is within
0.1% and every place within 1%; otherwise `gaps`, published with the gaps shown. The state layer
is domestic only and is not the national all-location total.

## Change records

A delta file's blank indicator can mean a new or an updated row, so blank and C rows are reported
together as "added or updated". D rows have no amount, date or place, so deletions are counted
nationally only. A negative amount is a de-obligation, never a cancellation. The snapshot diff
moves money between places when a row's place changes, and refuses snapshots with duplicate keys.
