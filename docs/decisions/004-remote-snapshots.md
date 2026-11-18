# 004 — Ingest on a GitHub runner; retain raw snapshots in private releases

Date: 2026-11-17. Status: accepted for the FY2025 pilot.

Repeated Treasury requests from the Mac failed even with sequential requests and backoff. The separate [probe](https://github.com/mintyfizz/money-trail/actions/runs/44067660053) downloaded the 1,833,040,425-byte national contracts ZIP in 40 seconds from a runner reporting Azure `westcentralus`. All seven CSV members passed ZIP integrity checking; they total 14,326,204,686 uncompressed bytes. This proves a working route at that time, not geographic blocking or a guarantee that all standard runners run in the US.

## Decision

Run discovery, resumable download, ZIP CRC/SHA-256 verification, streaming text-preserving Parquet conversion and independent CSV/read-back validation in `.github/workflows/remote-ingestion.yml`. Never extract the full CSVs. Retain the original ZIP and the verified output in a private prerelease, with a tag identifying source date, fiscal year, award category, agency/national scope and rebuild revision. Do not combine agency and national scopes or claim reconciled national coverage.

First run `agency=smallest` with the saved catalogue: recheck selected source identities, try known files in ascending compressed size, skip header-only archives, and stop after the first valid nonempty agency. This mode refuses candidates above 64 MiB. Measure output size before any larger run. This identifies the smallest nonempty file in the saved list, not across unresolved agencies. Leave the catalogue input blank for fresh discovery; an explicit agency database ID or `all` then selects a later scope. A first remote attempt confirmed 18 agencies before a connection error, so expanding the whole catalogue is not a prerequisite for the saved-catalogue pilot.

Each archive keeps an 8 GiB free-space reserve and a default 2 GiB output cap. The job has a 30-minute timeout, is sequential and has no schedule. Source metadata requests are spaced at least two seconds apart and transient failures use 5/15-second backoff; interrupted downloads use the existing strong-ETag range checkpoint. The runner retains its ZIP until it disappears with the ephemeral job; successful publication preserves another exact copy in the release. Failed conversion/publication does not guarantee a remotely retained raw copy.

Upload to a draft, compare the complete asset inventory, byte sizes and GitHub-provided SHA-256 digests, then publish. Never overwrite or delete published assets. A changed rebuild needs a new revision. Zero-byte rejection ledgers are represented by explicit size-zero/empty-SHA entries and reconstructed locally, avoiding empty-file upload limitations. No Actions artifact storage or cache is used for data.

`pipeline.snapshots` downloads only Parquet, provenance, catalogue and validation records to the Mac. It checks the same disk reserve, verifies provider and local hashes, and exposes a completed folder only after validation. It does not download the source ZIP. An interrupted pull leaves a hidden staging folder for inspection; it is not silently overwritten.

This is the preferred remote path and supersedes deletion as the default deployment choice in decision 003. The local-only `pipeline.pilot` and its guarded deletion/fixture policy remain available. Remote snapshots always retain the original ZIP; the first small source remains available as a real importer fixture in its release.

## Limits and cost

[GitHub documents](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases) at most 1,000 assets per release, each strictly below 2 GiB, with no documented total release-size or bandwidth limit. The national contracts ZIP fits that per-file limit; larger ZIPs must use agency scopes or another storage design. Release assets are separate from Actions artifact storage, but they are not an independently backed-up archive.

[Current Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) gives GitHub Free accounts $12/month of included compute; standard Linux is $0.006/minute (approximately 2,000 minutes). Account plan, other use and remaining allowance have not been verified. The 30-minute job cap bounds one standard Linux attempt to roughly $0.18 of compute at that rate; it does not enforce an account-wide free-only budget. No billing settings are changed.

`workflow_dispatch` only works once a workflow exists on the default branch. Before PR #1 is merged, explicitly pushing the tested commit to `run/remote-ingestion-pilot` starts the single smallest-agency pilot. Normal PR pushes only run offline checks. This launch does not merge the PR or change `main`. Once on the default branch, manual inputs choose later scopes and revisions.

## Live validation

[The successful pilot](https://github.com/mintyfizz/money-trail/actions/runs/44071758365) ran for 29 seconds including setup/tests/publication on a runner reporting `northcentralus`. It used the saved partial catalogue, skipped four header-only archives, and preserved Library of Congress: 106 rows, 297 source columns, zero rejects, 20,541 ZIP bytes and 119,809 Parquet bytes. Seven nonempty assets passed provider SHA-256 verification and were published in [revision 2](https://github.com/mintyfizz/money-trail/releases/tag/snapshot-2026-11-06-fy2025-contracts-agency-3-r2). The empty rejection ledger is represented in the index.

The Mac fetched seven files including the reconstructed empty ledger (191,398 logical bytes) in 6.787 seconds, with no raw ZIP download. All hashes, source text column types and logical row numbers passed local checks. Free space remained 21.52 GiB. No national conversion was started.

Pre-merge bootstrap: GitHub limits the automatic token's ability to create release tags for commits that change workflows relative to the default branch ([release API permissions](https://docs.github.com/en/rest/releases/releases)). The pilot's tag was prepared with the existing local GitHub authorization, pointing to the exact ingestion commit, before launch. No personal token was copied into Actions. Draft lookup by tag failed during testing; publication now uses the release ID returned by the create API for uploads, verification and publication. Failed empty drafts were removed; published assets were never overwritten. A changed rerun needs a new snapshot revision.
