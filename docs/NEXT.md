# Next work

The national build now covers contracts and financial assistance for FY2021–2025, one scope per
run, each preserved with its source and checked against USAspending (see the coverage table on the
site and `site/data/index.json`). Nothing is merged or deployed: the work is on `build/fy-pilot`
for review, and `main` is unchanged. PR #1 (milestone 1, per-agency ingestion) is earlier work.

Open items, in the order they would matter to a reader:

1. **Deployment proposal.** The site is a static folder. Publishing it (for example GitHub Pages)
   changes who can see it and needs an explicit decision; it is not done here.
2. **FY2025 loans difference.** All of it is Department of Veterans Affairs loan subsidy cost
   (−$75,745,774.19, equal in every state). A later snapshot of the FY2025 assistance file, built
   with `source: refresh`, would show whether VA revised the records; the old snapshot stays
   preserved and the disclosure stops showing as current once the source or comparison changes.
3. **Contract source differences.** FY2021–FY2024 also differ from the live state/DC API by
   $10.12B, $8.04B, $15.32B and $11.27B, respectively. Their causes were not investigated in this
   expansion; see the [verified comparison table](measurements/national-fy2021-2025.md). The FY2025
   $14.81B difference has bounded evidence in decision 005. No correction is applied and the
   experimental deleted-key exclusion stays off. Revisit with new evidence, without assuming
   one year's diagnostic explains another.
4. **Earlier years.** FY2008–2020 use the same workflow (`pipeline/run_request.py` would need its
   year range widened). Older assistance archives may carry codes the category map lacks; the build
   stops on any unmapped code rather than guessing.
5. **Refresh cadence.** Treasury publishes monthly. A refresh is one request per scope; nothing is
   scheduled. A schedule would need a decision about Actions usage.
6. **Finer geography and subawards.** County and congressional-district views, and subawards, are
   not built. The typed layer already keeps county FIPS for assistance and county codes for
   contracts.
7. **Recipient rankings by category or agency.** Rankings are exact only for a whole kind and all
   agencies. Narrower rankings need recipient-level aggregates per category and agency in the
   summary, not a re-cut of top-ten lists.
