# Next work

Milestone 1 is **in progress**. The sequential downloader, text-only raw Parquet, independent CSV/read-back validation and guarded ZIP deletion are implemented; live completion remains unverified.

1. Resume the partial `fy2025-agencies.json` catalogue using its unresolved agency IDs; preserve the 17 successful checks out of 112 listed agencies. The latest slower retry is recorded in `slow-agency-pilot.json`: all 12 requests failed, including the smallest archive’s metadata request; the browser download also returned an empty response. Do not assume why requests return empty responses. Confirm a real HTTP 206 range response with the expected source identity before relying on live resume.
2. Run the smallest FY2025 contract agency through `pipeline.pilot --limit 1`. Check complete raw CRC/SHA-256, full row counts, rejection records and actual Parquet size. Increase the selection only within the disk reserve.
3. Pin the first validated nonempty agency ZIP no larger than 64 MiB as a permanent local importer fixture. Run the next few agencies, verify deletion/reuse behavior, and measure their raw Parquet sizes before expanding. Cloud storage is optional; no provider is configured.
4. Investigate blank contract award types alongside IDV fields, and map old/new assistance categories before any business filter. The earlier ordered 10,000-row prefix is not nationally representative.
5. Create typed transaction tables from the preserved Parquet source layer, retaining original financial strings and flagged anomalies. Check transaction-key uniqueness and geographic coverage.
6. Reconcile one agency and three geographic slices with official transaction totals using aligned filters and snapshot dates. Extend scope after explaining material differences.

No scheduler, public site or social post is active. The pilot deletes ZIPs only after independent preservation checks pass, while keeping the pinned fixture. No live complete ZIP has yet reached that step; no upload is configured.
