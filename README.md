# Money Trail

Where US federal award obligations go, by place, agency and recipient. The long-term aim is reported prime contracts and grants from fiscal year 2008 onward, with a clickable state map.

**Status: FY2025 national contracts built end to end as a diagnostic pilot; reconciliation does not pass yet.** A GitHub runner downloads Treasury's FY2025 national contracts archive (1.83 GB, 6,658,878 transactions), streams it into a typed transaction layer, aggregates it by state, agency and recipient, and compares each state with USAspending's published totals. All quality gates pass (no unparseable or missing amounts or dates, no duplicate keys). The comparison does not: across the 50 states and DC the archive as published runs $14.81 billion (1.92%) above USAspending's live figures. Compared agency by agency over the same 51 places, the difference is overwhelmingly in the Department of Defense (+$14.82 billion, +3.0%). Most civilian agencies match to the cent or within 0.3%, with smaller gaps elsewhere (the largest in relative terms is the US Agency for Global Media, +$1.34 million or +2.94%). The largest single-state gap is Arkansas (+9.60%); Alabama is +9.31%. Most of the gap is accounted for. The archive contains about 19,000 matched old/new-key candidate pairs from Treasury re-issuing Defense transaction keys in early November 2026. The pairs whose new key was written on 4 November 2026 at 10:11:48 UTC equal the Army's gap to within $4, and to the cent in 50 of 51 places; across all agencies that batch's in-state contracts ($14,814,163,448.02) leave a residual of −$4,561,741.90 against the whole state gap. In sampled awards with a nonzero amount at stake, USAspending's live search counted one copy for that batch, and both copies for a larger 2 November batch whose live transaction records kept one. These are samples, not a check of every transaction, and the live map is not a clean reference either. The figures here stay as Treasury published them. See [decision 005](docs/decisions/005-national-typed-layer.md). The dashboard in `site/` is a local prototype until the gap is explained.

See the [plan review](docs/plan-review.md), [initial scope decision](docs/decisions/001-scope-and-storage.md), [resumable ingestion decision](docs/decisions/002-resumable-agency-ingestion.md), [raw preservation and deletion decision](docs/decisions/003-raw-parquet-retention.md), [remote snapshot decision](docs/decisions/004-remote-snapshots.md), [national typed layer decision](docs/decisions/005-national-typed-layer.md), and [next steps](docs/NEXT.md).

## National build (FY2025 pilot)

`.github/workflows/build-pilot.yml` runs on a GitHub runner when the `run/build-pilot` branch is pushed:

1. **Delta job:** downloads the monthly delta file, types it, writes the transaction keys it marks as deleted, summarises what changed, and preserves all of it in a verified release.
2. **Year job:** downloads the national full file (space checked first), streams each CSV member from the ZIP into typed Parquet (`pipeline/silver.py`), aggregates it with blocking quality gates (`pipeline/gold.py`), and compares it with USAspending by state and by agency (`pipeline/reconcile.py`). The source ZIP, the typed layer and an audit file of rows whose keys the delta deletes are preserved in a release that is published only after every file's SHA-256 is verified (`pipeline/release.py`).
3. **Site data:** combines the results into `site/data/` for the dashboard (`pipeline/site_data.py`).

Money is exact integer cents from source to page. Unparseable values, missing amounts or dates, duplicate keys and rows outside the fiscal year stop the build rather than being counted as zero. The free-space reserve (8 GiB) is checked before each download and on every Parquet write.

To view the dashboard locally after downloading the `site-data` artifact into `site/data/`:

```sh
python3 -m http.server 8765 --directory site
```

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

## Preferred remote run

See [remote snapshots](docs/decisions/004-remote-snapshots.md) for retention, cost and operational limits. `.github/workflows/remote-ingestion.yml` performs the Treasury transfer and conversion on a GitHub runner, retains the raw ZIP in a private release, and publishes only after local and remote checks pass. Its first run selects the smallest nonempty agency in the saved partial catalogue; larger archives require a deliberate scope selection. No scheduled refresh is enabled.

After publication, download only the preserved Parquet and validation records with the authenticated GitHub CLI:

```sh
.venv/bin/python -m pipeline.snapshots snapshot-2026-11-06-fy2025-contracts-agency-3-r2 --repo mintyfizz/money-trail --reserve-gib 8
```

The command uses the verified pilot release; use another exact tag for a later snapshot. Data lands under `data/github/`. The Mac does not download the original ZIP on this path. The retained release ZIP is the real importer fixture. Both machines retain an 8 GiB free-space reserve. Outputs are capped at 2 GiB per archive by default; each release asset must be below 2 GiB. A small agency's Parquet/ZIP ratio is only a provisional capacity scenario because metadata overhead dominates tiny files.

## Local ingestion alternative

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

The pilot completes one agency's download, verifies every ZIP member, converts and independently validates its CSVs, then removes that ZIP before starting another agency. One small real agency ZIP is permanently retained as a local importer fixture. Increase `--limit` deliberately after measuring space. Agency catalog completeness is not proof of national coverage; missing agency files are recorded, and national plus per-agency archives cannot be mixed. Use `--agency-ids` to select known database IDs from the official agency list. National discovery requires explicit `--agency all` and is separate from this pilot.

The download uses **one connection at a time**, with 8 MiB range requests and an 8 GiB free-space reserve. Rerun the same command after a connection failure. A `.zip.part` checkpoint carries the URL, source size, strong ETag and checksum of the saved prefix. Every response must be HTTP 206 with the exact requested range and matching ETag before bytes are appended. Changed source files, ignored range requests and altered checkpoints stop the transfer; no silent restart, merge or national-file fallback occurs. A process kill during the final append/checkpoint window can require manual inspection; normal network interruptions are resumable.

Only a complete archive whose members pass CRC validation becomes `.zip`, with a SHA-256 receipt. The processor requires that receipt and verifies its checksum before reading. It reads 4 MiB CSV input blocks, writes Zstandard Parquet in bounded parts, caps total output at 2 GiB per agency by default, and checks free space before every output write. No expanded CSV or processing spill file is created.

Every source column stays in the raw Parquet layer as text: leading-zero codes, blank strings, literal `NULL`/`NaN`, spaces, embedded newlines and negative amount text are preserved. CSV quoting is decoded; this is field-value preservation, not a byte-for-byte CSV/ZIP copy. Typing, filtering and modeling must read this raw layer in a separate step.

Each row also has `_mt_source_zip`, `_mt_source_file` (the CSV member path), and `_mt_source_row_number` (integer, 1-based logical CSV record, header = 1). Embedded newlines do not increase that number; rejected rows leave gaps. A source column colliding with these reserved names stops conversion rather than overwriting data.

An independent strict Python CSV reader counts all records and computes a digest of every accepted field value and its provenance. Arrow processes the source in batches. Every Parquet part is read back and its values are checked against that independent digest. Both CSV readers must agree on rejected records; `rejects.jsonl` keeps their exact decoded text including line endings and logical row number, and is read back too. For every member and archive, **Parquet rows + rejected rows must equal independently counted CSV rows**. Bad quoting, invalid UTF-8, schema drift, parser disagreement or a read-back mismatch leaves the ZIP and hidden staging directory in place.

Before deletion, the durable manifest records the ZIP SHA-256, byte size, source URL/identity, snapshot date, CSV/Parquet/reject counts, output checksums and measured Parquet-to-ZIP size ratio. All output files are flushed to disk and re-hashed; a durable deletion record is saved before unlinking. ZIP receipts and manifests remain. A rerun verifies preserved artifacts and reuses them without redownloading a deleted ZIP. These checks reduce parsing risk; they cannot prove the original bytes or rule out a shared parser bug.

The first successfully validated, nonempty agency ZIP no larger than 64 MiB is pinned in `data/raw/fixture.json` and never automatically deleted. Until one is available, other ZIPs stay local. A missing/altered fixture blocks later deletions. Archives without a snapshot date or with non-CSV members are retained. `--keep-zips` retains all newly processed ZIPs; it does not restore earlier deletions. Older Parquet outputs without the new validation must be rebuilt explicitly from their retained ZIPs.

`data/pilot-run.json` records each selected agency's compressed size, Parquet size, size ratio, conversion duration, retention outcome and free disk afterward. Start with `--limit 1`, then measure a few additional agencies before expanding. Neither the national ZIP size nor these small files establishes whether a whole year fits above the 8 GiB reserve.

To run the steps separately:

```sh
.venv/bin/python ingest/archive.py download --manifest docs/measurements/fy2025-agencies.json --kind contracts --limit 1
.venv/bin/python -m pipeline.bronze data/raw/VERIFIED_AGENCY_FILE.zip --max-output-gib 2
```

Replace `VERIFIED_AGENCY_FILE.zip` with the actual filename in the manifest. Standalone `pipeline.bronze` conversion retains the ZIP; the sequential `pipeline.pilot` applies the validated deletion policy. The preferred remote workflow stores originals in private GitHub releases; this local alternative needs no cloud provider.

The initial national `.zip.partial` is an explicitly incomplete diagnostic prefix, with no resume checkpoint. It is preserved separately and never adopted by the new downloader. Its sample remains reproducible with:

```sh
.venv/bin/python -m ingest.sample_prefix data/raw/FY2025_All_Contracts_Full_20261106.zip.partial --limit 10000 --output docs/measurements/fy2025-contracts-prefix-sample.json
```

Direct Mac source access remains intermittent (the separate GitHub runner probe succeeded): [paced retry evidence](docs/measurements/slow-agency-pilot.json), [earlier catalogue/pilot attempt](docs/measurements/live-agency-pilot-attempt.json), [partial agency catalogue](docs/measurements/fy2025-agencies.json), and [earlier range check](docs/measurements/source-access-check.json). Controlled tests establish our recovery behavior, not Treasury's current range support. No transfer is left running.

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
| `pipeline/` | Raw Parquet preservation and retention; national typed layer, aggregates, reconciliation, change records and verified releases |
| `site/` | Static dashboard: clickable state map, year control, state panel (local prototype) |
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
