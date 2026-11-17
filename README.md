# Money Trail

Where US federal award obligations go, by place, agency and recipient. The long-term aim is reported prime contracts and grants from fiscal year 2008 onward, with a clickable state map.

**Status: milestone 1 initiated, not complete.** Per-agency discovery, sequential resumable downloads, full ZIP verification and batch Parquet conversion are implemented. FY2025 national ZIP sizes were verified; a 10,000-record contract prefix was inspected. Full national downloads, assistance profiling and official-total reconciliation are still outstanding. There is no dashboard or scheduled refresh yet.

See the [plan review](docs/plan-review.md), [initial scope decision](docs/decisions/001-scope-and-storage.md), [resumable ingestion decision](docs/decisions/002-resumable-agency-ingestion.md), and [next steps](docs/NEXT.md).

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

Python 3.11+, curl, and macOS/Linux are required. Discovery, downloads and profiling use the standard library; Parquet conversion uses the pinned Arrow dependency. With the existing `uv` installation:

```sh
uv venv --python python3 .venv
uv pip install --python .venv/bin/python --system-certs -r requirements.txt
make test
```

Discover FY2025 contract archives **per agency**, then complete the smallest available agency first:

```sh
.venv/bin/python ingest/archive.py discover --years 2025 --kind contracts --output docs/measurements/fy2025-agencies.json
.venv/bin/python -m pipeline.pilot --manifest docs/measurements/fy2025-agencies.json --limit 1
```

The pilot completes one agency's download, verifies every ZIP member, then converts its CSVs before starting another agency. Increase `--limit` deliberately after measuring space. Agency catalog completeness is not proof of national coverage; missing agency files are recorded, and national plus per-agency archives cannot be mixed. Use `--agency-ids` to select known database IDs from the official agency list. National discovery requires explicit `--agency all` and is separate from this pilot.

The download uses **one connection at a time**, with 8 MiB range requests and an 8 GiB free-space reserve. Rerun the same command after a connection failure. A `.zip.part` checkpoint carries the URL, source size, strong ETag and checksum of the saved prefix. Every response must be HTTP 206 with the exact requested range and matching ETag before bytes are appended. Changed source files, ignored range requests and altered checkpoints stop the transfer; no silent restart, merge or national-file fallback occurs. A process kill during the final append/checkpoint window can require manual inspection; normal network interruptions are resumable.

Only a complete archive whose members pass CRC validation becomes `.zip`, with a SHA-256 receipt. The processor requires that receipt and verifies its checksum before reading. It reads 4 MiB CSV input blocks, writes Zstandard Parquet in bounded parts, caps total output at 2 GiB per agency by default, and checks free space before every output write. No expanded CSV or processing spill file is created.

All source values are preserved as strings in this initial Parquet layer, including leading-zero codes, blank strings and negative amount text. Financial typing, key uniqueness and official-total reconciliation remain modeling work. Rows with the wrong number of fields are saved in `rejects.jsonl`; malformed syntax, schema changes and other parse failures stop processing. A failed conversion stays in a hidden staging directory for inspection and is never exposed as completed output. `data/pilot-run.json` records the selected-file status and interrupted phase.

To run the steps separately:

```sh
.venv/bin/python ingest/archive.py download --manifest docs/measurements/fy2025-agencies.json --kind contracts --limit 1
.venv/bin/python -m pipeline.bronze data/raw/VERIFIED_AGENCY_FILE.zip --max-output-gib 2
```

Replace `VERIFIED_AGENCY_FILE.zip` with the actual filename in the manifest. **Raw ZIPs are retained locally.** Offloading and local removal are not enabled until a destination is selected and a verified copy exists there. A cloud sync folder can still keep a local copy and is not automatically an offload.

The initial national `.zip.partial` is an explicitly incomplete diagnostic prefix, with no resume checkpoint. It is preserved separately and never adopted by the new downloader. Its sample remains reproducible with:

```sh
.venv/bin/python -m ingest.sample_prefix data/raw/FY2025_All_Contracts_Full_20261106.zip.partial --limit 10000 --output docs/measurements/fy2025-contracts-prefix-sample.json
```

Live source access remains unresolved: [access check](docs/measurements/source-access-check.json). Controlled tests establish our recovery behavior, not Treasury's current range support. No transfer is left running.

## Planned pipeline

1. **Land**: Python downloads the archive files unchanged.
2. **Convert**: Arrow preserves source columns in Parquet batches without extracting CSVs. Benchmark PySpark for later typed modeling and aggregates; malformed-width records are retained in a reject ledger.
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
