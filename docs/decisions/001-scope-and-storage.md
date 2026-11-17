# Decision 001 — scope and storage

Date: 17 November 2026. Status: **accepted for the initial engineering pilot; national launch scope remains conditional**.

## Decision

Build and validate a FY2025 prime-contract pilot first. Keep the FY2008-onward vision as a later expansion. Retain financial-assistance discovery in the ingestion tool, but do not publish grant-only figures until the new and legacy assistance categories are mapped and tested.

Use the existing Mac only for bounded samples and compressed inputs, with an 8 GiB free-space reserve. Read CSVs inside ZIPs for measurement instead of extracting all files. Do not install Spark or begin the historical backfill until scratch/Parquet requirements are measured and a suitable storage location exists. No external data drive is currently mounted.

## Measurements and provenance

Live discovery and HTTP metadata on 17 November 2026 returned source snapshots dated 6 November 2026:

| FY2025 national archive | Compressed bytes | Decimal GB | Full row count | Columns |
| --- | ---: | ---: | --- | --- |
| Contracts | 1,833,040,425 | 1.833 | Unavailable: full download incomplete | 297 in the first CSV's sampled header |
| Financial assistance, all types | 1,412,581,622 | 1.413 | Unavailable: not downloaded | Unavailable |
| Combined | 3,245,622,047 | 3.246 | Unavailable | Different schemas; do not combine column counts |

The combined ZIP footprint is 3.02 GiB. Available disk at this decision: 11.58 GiB. Both ZIPs would leave only about 0.56 GiB beyond the chosen 8 GiB reserve, before Parquet, another snapshot or engine scratch. No disk cleanup was performed.

A deliberately simple sizing scenario for the 19 completed fiscal years FY2008–FY2026 is `19 × 3,245,622,047` = **61.7 GB (57.4 GiB) compressed**. This is an estimate, not a measured historical inventory or a grant-only estimate. Fiscal-year sizes vary; the unfinished catalog cannot support a precise backfill requirement. Raw expansion, Parquet, two snapshots, download staging and Spark spill are additional and currently unmeasured. FY2027 is partial and excluded from this scenario.

## Real sample, with explicit limits

The initial contract transfer was stopped after 12,357,632 bytes at roughly 0.2 MB/s. That initial throughput implied hours for the national pair; it is an observation, not a guaranteed future rate. Later discovery and download requests returned empty responses. All initiated transfers have been stopped.

The first 10,000 parsed records from the first CSV were inspected:

- 297 columns; zero row-width errors in those 10,000 records.
- 500 records with negative `federal_action_obligation`; retain these as observations rather than infer cancellations.
- 1,109 blank place-of-performance county FIPS codes; investigate geographic scope rather than fill a guessed county.
- 479 blank `award_type_code` values; inspect IDV-related fields before defining prime-contract eligibility.
- No full-file CRC verification, complete row count, key-uniqueness check or official-total reconciliation has been completed.

This is an ordered prefix from an incomplete archive, not a random or nationally representative sample. Its frequencies must not be extrapolated to the full year. The partial file is ignored by Git and cannot be accepted by the full-archive profiler. A local SHA-256 digest identifies the received prefix; it does not validate the missing archive bytes.

## Acceptance gates for the next phase

1. Complete both FY2025 source profiles, or explicitly select a narrower fully downloaded agency scope.
2. Preserve source schema, exact file metadata and ingestion time; ensure each full ZIP passes CRC reading.
3. Measure curated Parquet and scratch space on a complete selected slice.
4. Use source transaction keys; distinguish transaction facts from award summaries and observed revisions.
5. Reconcile obligations to official transaction totals using identical filters and comparable snapshot dates. Explain each material difference.

The downloader and profile workflow passed 11 offline checks, including a completed download/receipt fixture, idempotent reuse, corrupt/truncated ZIPs, quoted newlines and disk reserve enforcement. A complete live national download is still unverified. Milestone 1 remains **in progress**.

Evidence: [FY2025 manifest](../measurements/fy2025-catalog.json), [prefix profile](../measurements/fy2025-contracts-prefix-sample.json), [scope facts](../measurements/scope-facts.json), [Treasury archive](https://www.usaspending.gov/download_center/award_data_archive).
