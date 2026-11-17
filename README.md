# Money Trail

Where US federal award obligations go, by place, agency and recipient. The long-term aim is reported prime contracts and grants from fiscal year 2008 onward, with a clickable state map.

**Status: milestone 1 initiated, not complete.** Archive discovery, guarded downloads and streaming CSV profiling are implemented. FY2025 national ZIP sizes were verified; a 10,000-record contract prefix was inspected. Full national downloads, assistance profiling and official-total reconciliation are still outstanding. There is no dashboard or scheduled refresh yet.

See the [plan review](docs/plan-review.md), [scope and storage decision](docs/decisions/001-scope-and-storage.md), and [next steps](docs/NEXT.md).

## The three questions

1. **Where does it go?** Federal dollars into each state and county, by agency and award type, year by year.
2. **Who gets it?** The largest recipients in each place, and how concentrated the money is.
3. **What just changed?** New or revised source records and changes in obligations since the last refresh. A negative obligation or deleted record alone is not evidence of cancellation.

## Dashboard

A planned map shaded by net obligations for transactions in the selected fiscal year, using place of performance. State and county totals must retain explicit unknown, foreign and territorial buckets. Inflation adjustment and per-resident comparisons require additional documented sources and are deferred from the first pilot.

## Data

Source: [USAspending.gov](https://www.usaspending.gov) (US Treasury), Award Data Archive bulk files.

The archive publishes monthly snapshots and all-years change files. The verified FY2025 files are dated **2026-11-06**. Financial assistance includes more than grants; its categories must be mapped before publishing grant-only results.

Raw data is never committed to this repository.

Known limits, stated on the site:

- Figures are obligations (money committed), not payments. Negative obligations are retained.
- The source has documented completeness and accuracy problems.
- A transaction-year slice does not establish an award's complete lifetime history.
- The inspected prefix is not a random sample and does not establish national row counts or error rates.

## Run milestone 1

Requirements: Python 3.11+ and curl; no Python package installation is needed.

```sh
python3 -m unittest discover -s tests -v
python3 ingest/archive.py discover --years 2025 --output docs/measurements/fy2025-catalog.json
python3 ingest/archive.py download --manifest docs/measurements/fy2025-catalog.json --directory data/raw --reserve-gib 8
python3 ingest/archive.py profile data/raw/FY2025_All_Contracts_Full_20261106.zip --output docs/measurements/fy2025-contracts-full.json
python3 ingest/archive.py profile data/raw/FY2025_All_Assistance_Full_20261106.zip --output docs/measurements/fy2025-assistance-full.json
```

Use the filenames returned by discovery if the archive has changed. Downloads retain originals, check size and source metadata, record a local SHA-256 digest, and refuse to consume the configured free-space reserve. Profiling reads every ZIP member to validate CRCs and counts CSV records correctly even with quoted line breaks. It records malformed row widths and invalid/missing amounts; those are not silently corrected. Source ETags are opaque identifiers, not assumed MD5 checksums.

An interrupted transfer remains `.partial`; it is never accepted as a full archive. The first contract transfer in this session was stopped after 12,357,632 bytes because its observed throughput implied hours of work. To retry, preserve or move that partial file out of `data/raw` first. Subsequent source requests returned empty responses. No download is left running.

The diagnostic sample can be reproduced separately:

```sh
python3 -m ingest.sample_prefix data/raw/FY2025_All_Contracts_Full_20261106.zip.partial --limit 10000 --output docs/measurements/fy2025-contracts-prefix-sample.json
```

This diagnostic never produces a lakehouse input or a complete-file integrity claim.

## How it works

1. **Land**: Python downloads the archive files unchanged.
2. **Convert**: PySpark writes Parquet partitioned by fiscal year and award type; malformed rows go to a reject table.
3. **Model**: one row per source transaction in the current snapshot, an append-only observed-version history, and a separate award summary. Sum transaction obligations by action fiscal year; do not sum repeated award totals.
4. **Resolve**: preserve source names and group by known UEI; keep parent relationships separate and versioned. Missing identifiers remain unresolved.
5. **Aggregate**: summaries by state, county, agency, award type and year.
6. **Track changes**: compare complete, compatible snapshots or apply a verified delta sequence with inserts, updates and deletes. Keep source corrections separate from business events.
7. **Publish**: small summary files served by a static site.

## Layout

| Folder | Purpose |
| --- | --- |
| `ingest/` | Downloader and file checks |
| `pipeline/` | PySpark conversion, award model, aggregates, change tracking |
| `site/` | Static dashboard |
| `docs/decisions/` | Decision records |
| `docs/measurements/` | Source metadata and explicitly scoped profiling results |
| `tests/` | Offline ingestion and integrity checks |

## Milestones

| # | Milestone | Proof it works |
| --- | --- | --- |
| 1 | Measure and scope | A written scope decision with the numbers behind it |
| 2 | Raw to Parquet | Row counts match the source files |
| 3 | Award model | Yearly totals per agency land close to the official totals |
| 4 | Map dashboard | Three states checked by hand against the official site |
| 5 | Change log | A second snapshot produces a correct change list |
| 6 | Performance and launch | A full rebuild runs from one command |
