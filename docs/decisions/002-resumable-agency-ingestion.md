# Decision 002 — sequential agency downloads and streamed processing

Date: 17 November 2026. Status: accepted implementation direction; live source verification remains outstanding. The raw-ZIP retention policy below is superseded by decision 003. This refines decision 001 and preserves its incomplete measurement status.

## Download first, process second

Discover fiscal-year files per agency using Treasury's agency list and archive API. Save the catalog after each result and stop on a transport failure. Record agencies with no returned archive explicitly. Sort available files by compressed size for the pilot. Reject overlap with national files; never claim nationwide coverage from the selectable agency list alone.

Use one sequential connection and 8 MiB pieces. Before a run, compare HEAD metadata with the saved source identity. Each range request carries `If-Match` with a strong ETag. Accept only the exact requested `206 Content-Range`, matching ETag, identity content encoding and consistent length. The server's `Accept-Ranges` header alone is not proof that resume works.

Keep the response in a small temporary chunk until its headers are validated. A valid but interrupted response can contribute its received prefix to `.zip.part`; record its size and cumulative SHA-256 before stopping. A rerun checks the prefix and asks for the next byte. No automatic parallel connections or retry loops are enabled. Refuse changed identities, unsafe ranges and local checkpoint modifications. An abrupt process/power failure during append and metadata update can leave a mismatch requiring inspection; do not guess which bytes are trustworthy.

Read all ZIP members to validate CRCs before promotion to the completed filename. Save a SHA-256 receipt. No incomplete archive is sent to Arrow.

## Bounded conversion

Read CSVs directly from the verified local ZIP using Arrow, one batch at a time. Preserve every source column as text in the initial Parquet layer so identifiers, blank values and unusual financial strings survive unchanged. Use Zstandard compression and retain source checksum/member metadata. Numeric typing and business semantics are a later curated layer.

Retain malformed-width rows with their original text in a rejection ledger. Other parser failures stop the conversion. Verify Parquet row counts and save output checksums. Only rename the staging directory to its final location after the complete conversion and manifest are saved. Existing outputs are reused only when their recorded checksums match.

Default limits: 8 GiB free disk reserve, 8 MiB transfer pieces, 4 MiB CSV input blocks, 128 MiB approximate Parquet part target, and 2 GiB total output cap per archive. The input block size is not a hard process-RAM limit; Arrow/string overhead also uses memory. Disk checks occur before transfer/append and on every processing write. Raw ZIPs, failed staging and completed Parquet remain counted against actual free space.

The current roughly 21 GiB free is room for a bounded pilot, not evidence that every FY2025 agency plus Parquet and retained snapshots fits. The measured 3.246 GB is the national ZIP pair; per-agency total and Parquet size remain unmeasured.

## Raw preservation and cloud storage

Keep verified raw ZIPs locally until the destination is specified. A future offload must copy the ZIP and receipt, verify the destination object, and only then release the local copy. A provider choice, available quota and authenticated upload path must be established before sending files. No cloud account, subscription, upload or deletion was performed for this change.

For a free pilot, Google Drive offers 15 GB shared with Gmail and Photos; actual remaining space must be checked. Cloudflare R2 Standard includes 10 GB-month each month plus request allowances and free egress, with billable overages. Those allowances can support a limited pilot but not the estimated full history or unlimited monthly snapshots. Free storage does not provide free compute. [Google storage](https://support.google.com/drive/answer/6374270), [R2 pricing](https://developers.cloudflare.com/r2/pricing/)

## Verification and remaining evidence

Automated fixtures cover disconnected ranges and exact-offset resumption, changed identities, ignored ranges, corrupt ZIPs, complete receipts, batch conversion, leading-zero codes, rejection records and storage caps. They also exercise the complete interrupted-download-to-Parquet workflow. These are synthetic correctness tests, not measured Treasury throughput.

The live discovery and bounded 64 KiB range probe are recorded in `docs/measurements/source-access-check.json`. Empty replies do not establish an outage, throttling or geographic blocking. A different-network test could narrow the cause, but has not been performed. No complete agency ZIP or production Parquet output has yet been obtained from the source in this session.

Sources: [Treasury agency contract](https://github.com/fedspendingtransparency/usaspending-api/blob/master/usaspending_api/api_contracts/contracts/v2/bulk_download/list_agencies.md), [HTTP conditional range semantics](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1.1), [Arrow incremental CSV reading](https://arrow.apache.org/docs/python/csv.html#incremental-reading).
