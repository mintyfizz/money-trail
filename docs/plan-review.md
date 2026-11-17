# Money Trail plan review

Reviewed 17 November 2026. **Assessment: needs revision before a full-history build; suitable to initiate as a bounded pilot.** The starter documentation now reflects the corrections below; the data model and dashboard are still to be built. The original Claude artifact was not edited.

## What to keep

The public question is clear, raw-file preservation is sound, and reconciling official totals before publishing is the right gate. A static site backed by small summaries is practical. The plan's ambition to show source limitations should remain central.

## Material corrections

| Location in the plan | Finding and implementation decision |
| --- | --- |
| Model: one row per award | Use a transaction fact, an observed-version history and a separate award summary. Annual activity is the sum of transaction obligations by action fiscal year. Repeated lifetime award totals must never be summed across transaction rows. A FY2025 slice cannot claim complete lifetime history. |
| Changes and cancellations | Distinguish a new observation, a source correction, a deletion, a de-obligation and a documented termination. Never infer cancellation from a negative amount, disappearance or zero balance. Store action date and observation date separately. |
| Data and refresh | The bulk archive is monthly, even though underlying site data updates more frequently. Delta files are available and span fiscal years; the source defines C/update, D/delete, blank/add. A delta must not simply be appended and summed. Missing a monthly delta requires a new compatible baseline. |
| Grants, loans and other assistance | Assistance ZIPs are not grant-only. Preserve raw type codes and descriptions, use a versioned category mapping, and treat unknown codes as unresolved. Loans require subsidy-cost treatment rather than loan face value in a comparable spending measure. Start with contracts while assistance mapping is verified. |
| Recipient resolution | Group on verified UEI where present. Preserve raw names and unresolved entities. Parent relationships need their own time-aware mapping; a current parent must not silently rewrite historical ownership. Do not merge on names. |
| Geography and headline wording | Say net obligations by place of performance, rather than money received by residents. Preserve unknown, foreign and territory totals so the 50-state-plus-DC map does not pretend to explain the entire dataset. Keep leading-zero geographic codes as strings. |
| Inflation and per-resident measures | These contradict the plan's single-source premise unless additional sources are named. Add Census population/geographic vintages and a documented inflation index/base-year policy before enabling these measures. The first pilot shows nominal dollars only. |
| Scope, engine and delivery | Select FY2025 contracts as the engineering pilot. Gate national release and older years on complete profiling, storage and reconciliation. Benchmark Spark and a DuckDB alternative on the same inputs before claiming Spark is required. Small precomputed JSON summaries may serve the first static dashboard without a browser database. |
| Validation milestones | Replace 'close to official totals' with identical filters, dates, award categories and source snapshot; record dollar and percentage gaps and explain them. Do not demand a non-empty change list: an unchanged second snapshot is valid. Test an identical snapshot and deliberate correction/deletion fixtures. |

## Source changes that affect the design

Treasury's October 2026 disclosure documents changes to financial-assistance category values, new action types and geographic classifications. It also records a July 2025 change in financial-assistance award-key construction. Preserve the supplied keys and schema version instead of rebuilding keys using an old formula. These changes make snapshot comparability an explicit requirement. [Treasury, About the Data, pp. 9–10](https://www.usaspending.gov/data/about-the-data-download.pdf)

The archive page explains monthly full/delta publication and its correction indicator. Its current listings include both FY2025 full files and November 2026 delta files. [Award Data Archive](https://www.usaspending.gov/download_center/award_data_archive)

The federal-spending guide distinguishes obligations, outlays and loan subsidy cost, and distinguishes transaction downloads from award summaries. [Analyst's Guide](https://www.usaspending.gov/federal-spending-guide)

The API contract supplies archive discovery parameters; Treasury's implementation exposes distinct transaction and award keys and transaction versus cumulative amounts. [Discovery contract](https://github.com/fedspendingtransparency/usaspending-api/blob/master/usaspending_api/api_contracts/contracts/v2/bulk_download/list_monthly_files.md), [assistance export implementation](https://github.com/fedspendingtransparency/usaspending-api/blob/master/usaspending_api/download/delta_downloads/transaction_assistance_monthly.py)

## Scope of this review

Reviewed the supplied project's data, model, metrics, dashboard concept, timeline, risks and first-session tasks. Verified live archive metadata and source disclosures, and inspected a bounded contract prefix. No dashboard exists to inspect. National totals, full row counts, recipient uniqueness, delta application and complete source-file integrity are not yet verified. Six weeks at 8–10 hours per week should be treated as a pilot target; a full-history public launch remains conditional on those checks.
