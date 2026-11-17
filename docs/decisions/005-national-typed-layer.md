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

## Re-keyed rows: observed, not corrected

The 2026-11-06 FY2025 full file contains transactions whose keys Treasury re-issued on
2026-11-04 (transaction number 2 became 14, 1 became 16) next to the new keys. For example award
`CONT_AWD_W31P4Q25F0017_9700_W31P4Q22D0004_9700`, USAspending's published total ($471,159,233.07)
counts only the new keys; the archive holds both ($942,318,466.14).

The same release's delta file lists 112,572 deleted keys; 19,239 of them appear in the FY2025 full
file ($49.68B). Every one of the 19,206 that has a twin is the older copy. Removing them was tested
and **rejected**: the 50-state-and-DC gap on contract codes A–D moved from +$14.81B to −$31.63B
(independent check by Codex, `stale-key-independent-check.json`; reproduced here). Restricting the
removal to rows with a live twin gave the same result.

### What explains the gap (17 November 2026, Claude and Codex independently)

- **Accounting (Codex, aggregate API calls only):** the gap is +$14,824,050,391.33 in Defense,
  of which the Army is +$14,711,969,859.64. The archive rows whose surviving twin was written at
  exactly `2026-11-04 10:11:48+00` total $14,711,969,855.64 for the Army: a residual of $4.00.
  State by state, that batch equals the Army gap to the cent in 50 of 51 places (Virginia +$4.00).
  DCMA's part of the same batch equals its −$3,562,088.89 gap exactly. Across all agencies the
  batch holds $14,814,163,448.02 of in-state contracts twice, against a total gap of
  $14,809,601,706.12. Files: Codex's `gap-investigation/`, and `deleted_key_batches` in the summary.
- **Mechanism (Claude, award-level live checks):** 50 sampled PIIDs each match USAspending's live
  search total exactly as either "both copies" or "one copy" of the re-keyed rows, never neither.
  Stratified by batch (7 awards each, all pages of `/api/v2/transactions/`):
  - 4 Nov `10:11:48`: the live search total and the live transaction records agree in all 7 awards
    (one copy in 5, both in 2). The archive holds both, which is the gap.
  - 2 Nov (`14:06:57`, `14:55:48`): the live search total counts both copies in all 7 awards, but
    the live transaction records hold one copy in all 7. The archive also holds both, so the
    archive matches the live map there.
- **Earlier failed fix explained:** removing every deleted key also removed the 2 November copies
  ($30.58B of in-state contracts) that the live map still counts, which is why the gap swung to
  −$31.63B.

**Proven:** the batch accounting (to $4 for the Army, state by state) and the search versus records
disagreement for the sampled 2 November awards. **Not proven:** that every award in each batch
behaves like its sample, why the live search still holds the 2 November copies, and the small
civilian differences (largest relative: USAGM +2.94%).

**Consequence:** neither live figure is a clean reference. The live map totals count the
2 November duplicates; the archive counts both batches; USAspending's transaction records keep one
copy of each sampled pair. No total on this site is "corrected": no deletion rule is applied, and
the page lists the duplicated batches next to the gap.

So the build publishes the archive as Treasury published it. Matched rows are reported
(`deleted_key_matches`) and written to `stale-rows.parquet` for audit, but not removed; the
reconciliation shows `gaps` with the agency breakdown. `--apply-deleted-keys` remains an
experimental option, off by default.

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
