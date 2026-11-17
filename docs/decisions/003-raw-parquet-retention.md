# Decision 003 — preserve raw fields, then release ZIP space

Date: 17 November 2026. Status: user-approved workflow, implemented and tested with controlled fixtures; real-agency measurements remain blocked by source access. Supersedes decision 002's requirement to retain every ZIP pending cloud offload.

## Raw layer contract

Every source column is preserved in order as a UTF-8 string. Do not infer numeric/date types, replace null-like strings, trim spaces, filter records or drop columns. Add `_mt_source_zip`, `_mt_source_file` (CSV member path) and `_mt_source_row_number` (integer logical record number, header = 1). Reject source schema collisions rather than overwriting columns. Typing, filtering, deduplication and business modeling belong in a separate step reading the raw Parquet plus the rejection ledger as needed.

CSV quotes and escapes are syntax and are decoded. The retained representation preserves decoded field values, not the exact CSV serialization or original ZIP bytes. ZIP checksum/size, catalog snapshot date, source URL/ETag and member inventory remain in the manifest. Without the original ZIP, that checksum identifies the former file but cannot establish its contents independently.

## Independent validation before deletion

Use a second strict CSV parser to count logical records, identify rejected-width rows and compute a canonical SHA-256 over accepted values and provenance. Embedded newlines do not create extra records. Compare every written Parquet value through a bounded read-back pass against that digest. Compare Arrow's rejection records with the independent parser, and read back the saved rejection ledger, which retains original decoded record text with line endings and row numbers.

Require `Parquet rows + rejected rows = independently counted CSV rows` per member and per ZIP. Counts from one parser alone would be circular evidence and would miss value corruption. Bad quoting, invalid encoding, parser disagreement, schema drift, output corruption or incomplete processing prevents publication and deletion. Both parsers can still share an interpretation error; these checks reduce that risk, they do not eliminate it.

Flush completed outputs and manifest to disk, verify output hashes again, then durably record deletion eligibility before unlinking the source ZIP. Keep the small receipt and deletion record. A crash after unlink but before the final status update can reuse the already verified outputs. Never redownload an intentionally deleted archive just to repeat a completed conversion. A changed catalog identity, changed output or changed manifest fails closed.

## One permanent fixture and bounded storage

Pin the first successfully validated, nonempty agency ZIP no larger than 64 MiB in `data/raw/fixture.json`. Selection is smallest-first from the requested agency files. This real ZIP remains local, outside Git, and the pilot never deletes it. Future importer changes can reprocess it with `pipeline.bronze --output-root data/fixture-check` and compare source-field counts/digests with the preserved manifest. Until it exists, retain all ZIPs. If it disappears or its checksum changes, block other deletions. An archive with non-CSV members or without a catalog snapshot date is retained because preservation/provenance is incomplete. The old national diagnostic `.partial` is unrelated and remains untouched.

The default sequential pilot now applies the deletion workflow. Standalone conversion does not delete. `--keep-zips` retains newly processed archives, without restoring previously deleted files.

The 8 GiB reserve and per-archive output cap remain active. Record source bytes, Parquet bytes, size ratio, conversion time and remaining disk after each agency. Independent parsing and full read-back add CPU/I/O work, but do not extract a CSV to disk. Memory is bounded by Arrow batches, a 16 MiB-character maximum CSV record and small read-back batches; these are not a strict process-RAM quota.

Do not extrapolate the full year from national ZIP size or a few small agencies. Actual agency storage ratios, total agency coverage and live resume support remain unmeasured. No cloud upload is required by this policy; users accept loss of byte-level source evidence except for the pinned ZIP.

Implementation references: [Python CSV behavior](https://docs.python.org/3/library/csv.html), [Arrow CSV parsing options](https://arrow.apache.org/docs/python/generated/pyarrow.csv.ParseOptions.html).
