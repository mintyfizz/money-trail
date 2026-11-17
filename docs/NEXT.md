# Next work

Milestone 1 is **in progress**, not complete.

1. When source access recovers, rediscover FY2025 archives. Preserve the interrupted contract prefix separately before retrying a full transfer. Keep an 8 GiB disk reserve. Download one file at a time and profile without extracting CSVs.
2. Finish full contracts and assistance row/column/size counts. Record CSV expansion and later Parquet size; do not extrapolate rows from the ordered prefix sample. Validate ZIP CRCs and reconcile valid plus rejected records with parsed records.
3. Investigate the 479 blank contract `award_type_code` values in the sample, alongside `award_or_idv_flag` and IDV fields, before fixing a prime-contract filter. Profile all assistance category codes/descriptions, including 2026 changes, before defining grants/cooperative agreements.
4. Decide storage for a full backfill from measured compressed, Parquet, scratch and two-snapshot requirements. Current disk capacity is sufficient for bounded sampling but not the proposed full-history lakehouse.
5. Implement the FY2025 contract transaction table with explicit schema and decimal amounts. Quarantine invalid records, retain leading-zero geography codes, and check uniqueness of the supplied transaction key.
6. Reconcile one agency and three geographic slices using aligned official transaction filters and snapshot dates. Build a small nominal-obligation state summary only after explaining all material differences.

No scheduler, public site or social post is active. Dagster scheduling belongs after idempotent ingestion, storage and reconciliation are verified.
