# Next work

Milestone 1 is **in progress**. The sequential downloader and Arrow processing path are implemented; live completion remains unverified.

1. Restore source access and run per-agency discovery. Do not assume why requests return empty responses. Confirm a real HTTP 206 range response with the expected source identity before relying on live resume.
2. Run the smallest FY2025 contract agency through `pipeline.pilot --limit 1`. Check complete raw CRC/SHA-256, full row counts, rejection records and actual Parquet size. Increase the selection only within the disk reserve.
3. Select the raw-snapshot destination. Google Drive can support a small free pilot if enough of its shared quota is available; R2 has a free allowance with billable overages. No provider is configured yet. Keep ZIPs until a remote copy is verified.
4. Investigate blank contract award types alongside IDV fields, and map old/new assistance categories before any business filter. The earlier ordered 10,000-row prefix is not nationally representative.
5. Create typed transaction tables from the preserved Parquet source layer, retaining original financial strings and flagged anomalies. Check transaction-key uniqueness and geographic coverage.
6. Reconcile one agency and three geographic slices with official transaction totals using aligned filters and snapshot dates. Extend scope after explaining material differences.

No scheduler, public site or social post is active. No raw ZIP is automatically deleted or uploaded.
