# Verified national builds: FY2021–FY2025

All ten scopes were built sequentially and independently checked against the actual preserved outputs. Original snapshots are retained in private GitHub releases. The national measure is net obligations for contracts and non-loan assistance; loans use subsidy cost. Loan face values are separate. Amounts are nominal dollars.

Total: **80,451,419 transactions**, **25,337 complete agency/place/category cells**. Each source and output asset has a verified digest. No deleted-key exclusion was applied.

| FY | Kind | Transactions | All-location amount | ZIP GiB | Parquet GiB | Parquet / ZIP | Job minutes |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2025 | [assistance](https://github.com/mintyfizz/money-trail/actions/runs/44204890982) | 6,440,401 | $4,522,828,936,171.33 | 1.316 | 0.584 | 44.41% | 15.18 |
| 2025 | [contracts](https://github.com/mintyfizz/money-trail/actions/runs/44207572323) | 6,658,878 | $843,407,140,137.53 | 1.707 | 0.593 | 34.74% | 5.88 |
| 2024 | [assistance](https://github.com/mintyfizz/money-trail/actions/runs/44209444890) | 5,196,680 | $4,191,354,399,235.87 | 1.205 | 0.511 | 42.42% | 10.68 |
| 2024 | [contracts](https://github.com/mintyfizz/money-trail/actions/runs/44219725130) | 6,709,181 | $802,697,165,802.52 | 1.737 | 0.602 | 34.68% | 14.32 |
| 2023 | [assistance](https://github.com/mintyfizz/money-trail/actions/runs/44211409089) | 6,176,722 | $4,071,604,324,933.39 | 1.131 | 0.513 | 45.36% | 11.43 |
| 2023 | [contracts](https://github.com/mintyfizz/money-trail/actions/runs/44221650339) | 6,707,708 | $809,108,870,612.13 | 1.757 | 0.603 | 34.30% | 6.77 |
| 2022 | [assistance](https://github.com/mintyfizz/money-trail/actions/runs/44213592741) | 8,811,553 | $3,918,557,380,707.20 | 1.399 | 0.615 | 43.93% | 14.65 |
| 2022 | [contracts](https://github.com/mintyfizz/money-trail/actions/runs/44222656814) | 6,679,458 | $725,121,451,450.56 | 1.785 | 0.606 | 33.93% | 14.38 |
| 2021 | [assistance](https://github.com/mintyfizz/money-trail/actions/runs/44215930765) | 20,656,186 | $4,666,274,977,800.82 | 2.545 | 1.223 | 48.04% | 28.23 |
| 2021 | [contracts](https://github.com/mintyfizz/money-trail/actions/runs/44224599393) | 6,414,652 | $681,375,622,627.49 | 1.746 | 0.577 | 33.05% | 5.17 |

Job time includes setup, storage preparation, download/rebuild, validation and preservation. It excludes queue time and the independent Mac review. GiB means 1,073,741,824 bytes. Exact sizes, source/build tags, commits, query times, checks and step durations are in [the measurement JSON](national-fy2021-2025.json).

## Comparisons with the live API

These comparisons cover the 50 states and DC, using the same category definitions. They are separate from the all-location amounts above. `Within tolerance` means the national difference is within 0.1% and every state within 1%; it does not mean exact equality. The archive and live API are different publication times.

| FY | Kind / category | Result | Archive minus live |
| --- | --- | --- | ---: |
| 2025 | Grants | Exact in all 51 places | $0.00 |
| 2025 | Direct payments | Within tolerance | $809,419.06 |
| 2025 | Loan subsidy cost | Outside tolerance | −$75,745,774.19 |
| 2025 | Insurance and other assistance | Exact in all 51 places | $0.00 |
| 2025 | Unspecified assistance | Unavailable | No comparable figure |
| 2025 | Contracts excluding IDVs | Outside tolerance | $14,809,601,706.12 |
| 2025 | Indefinite-delivery vehicles | Unavailable | No comparable figure |
| 2024 | Grants | Within tolerance | −$12,444,870.00 |
| 2024 | Direct payments | Exact in all 51 places | $0.00 |
| 2024 | Loan subsidy cost | Within tolerance | −$0.01 |
| 2024 | Insurance and other assistance | Exact in all 51 places | $0.00 |
| 2024 | Unspecified assistance | Unavailable | No comparable figure |
| 2024 | Contracts excluding IDVs | Outside tolerance | $11,274,995,284.68 |
| 2024 | Indefinite-delivery vehicles | Unavailable | No comparable figure |
| 2023 | Grants | Within tolerance | −$4,645,000.00 |
| 2023 | Direct payments | Exact in all 51 places | $0.00 |
| 2023 | Loan subsidy cost | Exact in all 51 places | $0.00 |
| 2023 | Insurance and other assistance | Exact in all 51 places | $0.00 |
| 2023 | Unspecified assistance | Unavailable | No comparable figure |
| 2023 | Contracts excluding IDVs | Outside tolerance | $15,323,631,214.58 |
| 2023 | Indefinite-delivery vehicles | Unavailable | No comparable figure |
| 2022 | Grants | Exact in all 51 places | $0.00 |
| 2022 | Direct payments | Exact in all 51 places | $0.00 |
| 2022 | Loan subsidy cost | Exact in all 51 places | $0.00 |
| 2022 | Insurance and other assistance | Exact in all 51 places | $0.00 |
| 2022 | Unspecified assistance | Unavailable | No comparable figure |
| 2022 | Contracts excluding IDVs | Outside tolerance | $8,042,934,149.26 |
| 2022 | Indefinite-delivery vehicles | Unavailable | No comparable figure |
| 2021 | Grants | Exact in all 51 places | $0.00 |
| 2021 | Direct payments | Within tolerance | −$135,922.50 |
| 2021 | Loan subsidy cost | Exact in all 51 places | $0.00 |
| 2021 | Insurance and other assistance | Exact in all 51 places | $0.00 |
| 2021 | Unspecified assistance | Unavailable | No comparable figure |
| 2021 | Contracts excluding IDVs | Outside tolerance | $10,122,377,669.15 |
| 2021 | Indefinite-delivery vehicles | Unavailable | No comparable figure |

The FY2025 contracts difference is documented in [decision 005](../decisions/005-national-typed-layer.md); the FY2025 VA loan subsidy difference has [saved evidence](../evidence/fy2025-va-loans/). Other differences are reported as measured, without an unsupported causal explanation. IDVs and unspecified assistance retain explicit unavailable-comparison status. No totals were forced to match the API.

## FY2021 assistance: oversized original ZIP

The exact 2,733,086,438-byte original is preserved as two ordered parts of 1,992,294,400 and 740,792,038 bytes in `source-assistance-fy2021-2026-11-06-18f3bcfedde3`. The `.parts.json` manifest records contiguous offsets and each digest. Whole-file SHA-256: `18f3bcfedde33ee76bf6807c2f46c2e851e1f7533f283ee1ec0254f053046a1f`. The original download passed CRC verification for all 21 CSV members.

Use the existing `pipeline.source rebuild` command with that release, the original ZIP name and `--sha-asset sha256-source.txt`; it retrieves and verifies each part and the reconstructed whole. Reconstruction needs space for the whole ZIP plus the largest temporary part, then Parquet/query headroom, above the enforced 8 GiB reserve. The current dashboard needs only the small build outputs, not the ZIPs or typed layer.

## Review boundary

144 offline tests passed independently. Every generated matrix cell was compared with an independent query over the actual Parquet (FY2025 contracts reuse was also byte-identical to its previously verified build). Final site imports were checked against the verified summaries and provenance. Browser checks cover the year/category/agency controls, civilian agencies, signed amounts, per-resident ranks, history, keyboard navigation and mobile width. No merge or public deployment is included.
