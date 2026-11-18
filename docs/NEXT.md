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
3. **FY2025 contracts difference** ($14.81 billion, Defense re-keyed transactions): bounded and
   documented in decision 005; no correction is applied. Revisit only with new evidence, such as a
   later snapshot without the duplicate copies.
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
