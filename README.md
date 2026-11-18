# Money Trail

Where US federal award obligations go, by place, agency and recipient. The long-term aim is reported prime contracts and grants from fiscal year 2008 onward, with a clickable state map.

**Status: national contracts and financial assistance, FY2021–2025, being loaded one scope at a time.** Each scope (one kind, one fiscal year) is built on a GitHub runner from Treasury's national archive, checked by blocking quality gates, compared category by category and agency by agency with USAspending's published state figures, and preserved with its source in verified releases. The [coverage table on the site](site/) and `site/data/index.json` say exactly what is loaded; anything not loaded is shown as not loaded, never as zero.

What the checks currently show:

- **FY2025 financial assistance:** grants and insurance/other match USAspending to the cent in all 51 places; direct payments pass (+$809,419.06, Arizona); loans fail by −$75,745,774.19, all of it Department of Veterans Affairs loan subsidy cost, equal in every state. The archive is internally consistent (every row matches Treasury's generated amount); whether VA revised records after the snapshot is not established. [Evidence](docs/evidence/fy2025-va-loans/).
- **FY2024 financial assistance:** all four comparable categories pass (direct payments and other exact, loans within one cent, grants −0.0011%).
- **FY2025 contracts:** the archive as published runs $14.81 billion (1.92%) above USAspending across the 50 states and DC, overwhelmingly in the Department of Defense, from transaction keys Treasury re-issued in early November 2026. See [decision 005](docs/decisions/005-national-typed-layer.md) for what is proven and what is not. Figures stay as Treasury published them.
- Assistance type "not specified" (−1) is kept as its own group; USAspending returns no state figure for it, so it is shown unchecked.

How spending types, the loan measure, place and agencies are defined: [decision 006](docs/decisions/006-assistance-and-categories.md).

See also the [plan review](docs/plan-review.md), [decisions 001–004](docs/decisions/) on scope, ingestion and preservation, and [next steps](docs/NEXT.md).

## National build

`.github/workflows/build-national.yml` runs one scope per push of the `run/build-national` branch, described by `.github/run-request.json` (validated by `pipeline/run_request.py`). Runs never overlap and are never cancelled.

1. **Source.** *Refresh* lists Treasury's files (`pipeline/fetch.py`), records the file's size and strong ETag, downloads it with the resumable, ETag-pinned downloader (`ingest/archive.py`) and preserves it in its own release named by kind, year, snapshot date and SHA-256 prefix; a file at or above GitHub's 2 GiB asset limit is preserved as exact byte ranges with a manifest (`pipeline/release.py`). *Rebuild* reproduces a preserved snapshot from its release alone, verifying every part and the whole file, without contacting Treasury (`pipeline/source.py`).
2. **Typed layer.** Each CSV member streams from the ZIP into typed Parquet (`pipeline/silver.py`, column contracts in `pipeline/kinds.py`).
3. **Aggregates.** Place, category (`pipeline/categories.json`) and measure for every row, a full agency × place × category matrix that must add up exactly, recipients and outside-the-map totals, with blocking gates (`pipeline/gold.py`).
4. **Reconciliation.** Every category against USAspending's state figures, and every agency over the same 51 places (`pipeline/reconcile.py`).
5. **Preservation.** Typed layer, results and provenance in a build release published only after every asset's SHA-256 is verified.

`pipeline/site_data.py` combines the scopes into `site/data/`. Hand-written findings in `pipeline/disclosures.json` are shown only while the source file and comparison they were made against are still the ones loaded.

Money is exact integer cents from source to page. The free-space reserve (8 GiB) is checked before every download, write and query spill.

To view the dashboard locally:

```sh
python3 -m http.server 8765 --directory site
```

### Reproduce

Run one scope on GitHub: put one request on the `run/build-national` branch and push it. Refresh downloads Treasury's current file; rebuild reuses a preserved snapshot exactly.

```json
{"kind": "assistance", "year": 2024, "source": "refresh"}
{"kind": "contracts", "year": 2025, "source": "rebuild",
 "snapshot_tag": "build-fy2025-contracts-2026-11-06-47b4a4d",
 "source_name": "FY2025_All_Contracts_Full_20261106.zip",
 "deleted_keys_tag": "build-delta-contracts-2026-11-06-47b4a4d"}
```

Or by hand, with the authenticated GitHub CLI and at least the source size plus 8 GiB free:

```sh
# exact preserved source, verified part by part and as a whole; never contacts Treasury
python -m pipeline.source rebuild --repository mintyfizz/money-trail \
  --tag source-assistance-fy2024-2026-11-06-b9c4c62700a8 \
  --source-name FY2024_All_Assistance_Full_20261106.zip --out-dir data/zip --sha-asset sha256-source.txt
python -m pipeline.silver data/zip/FY2024_All_Assistance_Full_20261106.zip data/silver --kind assistance
python -m pipeline.gold data/silver out/assistance-fy2024/summary.json --kind assistance --year 2024 \
  --snapshot-date 2026-11-06 --source-zip FY2024_All_Assistance_Full_20261106.zip
python -m pipeline.reconcile out/assistance-fy2024/summary.json out/assistance-fy2024/reconcile.json
python -m pipeline.site_data out site/data   # one folder per scope: summary.json, reconcile.json, provenance.json
```

The national typed layer keeps only the columns listed in `pipeline/kinds.py`: money and dates are typed with their raw text kept beside them, the rest stay as text. It does not preserve every source column; the preserved original ZIP is the raw layer.

## The three questions

1. **Where does it go?** Federal dollars into each state and county, by agency and award type, year by year.
2. **Who gets it?** The largest recipients in each place, and how concentrated the money is.
3. **What just changed?** New or revised source records and changes in obligations since the last refresh. A negative obligation or deleted record alone is not evidence of cancellation.

## Dashboard

A map of the 50 states and DC shaded by the selected year, spending type and awarding agency, by total or per resident; a state panel with types, agencies (net reductions listed apart) and, for whole kinds of award, the largest recipients; money outside the map; a sortable table; a coverage table; and the checks against USAspending. Every figure that a selection changes is summed from the full agency × place × category matrix.

## Data

Source: [USAspending.gov](https://www.usaspending.gov) (US Treasury), Award Data Archive bulk files.

The archive publishes monthly snapshots and all-years change files. The files loaded so far are dated **2026-11-06**.

Raw data is never committed to this repository.

Known limits, stated on the site:

- Figures are obligations (money committed), not payments. Negative obligations are retained.
- The source has documented completeness and accuracy problems.
- A transaction-year slice does not establish an award's complete lifetime history.
- The inspected prefix is not a random sample and does not establish national row counts or error rates.

## Earlier per-agency remote run (milestone 1)

See [remote snapshots](docs/decisions/004-remote-snapshots.md) for retention, cost and operational limits. `.github/workflows/remote-ingestion.yml` performs the Treasury transfer and conversion on a GitHub runner, retains the raw ZIP in a private release, and publishes only after local and remote checks pass. Its first run selects the smallest nonempty agency in the saved partial catalogue; larger archives require a deliberate scope selection. No scheduled refresh is enabled.

After publication, download only the preserved Parquet and validation records with the authenticated GitHub CLI:

```sh
.venv/bin/python -m pipeline.snapshots snapshot-2026-11-06-fy2025-contracts-agency-3-r2 --repo mintyfizz/money-trail --reserve-gib 8
```

The command uses the verified pilot release; use another exact tag for a later snapshot. Data lands under `data/github/`. The Mac does not download the original ZIP on this path. The retained release ZIP is the real importer fixture. Both machines retain an 8 GiB free-space reserve. Outputs are capped at 2 GiB per archive by default; each release asset must be below 2 GiB. A small agency's Parquet/ZIP ratio is only a provisional capacity scenario because metadata overhead dominates tiny files.

## Local per-agency ingestion (milestone 1)

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
| `site/` | Static dashboard: map, year, spending-type and agency controls, state panel, coverage (not deployed) |
| `docs/decisions/` | Decision records |
| `docs/measurements/` | Source metadata and explicitly scoped profiling results |
| `docs/evidence/` | Small saved requests and comparisons behind specific findings |
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
