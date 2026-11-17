# Money Trail

Where US federal money goes: a lakehouse of every federal contract and grant since fiscal year 2008, with a clickable state-by-state map.

**Status:** planning. Nothing is built yet.

## The three questions

1. **Where does it go?** Federal dollars into each state and county, by agency and award type, year by year.
2. **Who gets it?** The largest recipients in each place, and how concentrated the money is.
3. **What just changed?** New awards, cut-backs and cancellations since the last refresh.

## Dashboard

A map of the United States shaded by federal money received in the selected year. A year slider drives the map. Clicking a state opens a panel with its total, rank, yearly trend, top agencies, award types, top recipients and recent changes, with a drill-down to counties.

## Data

Source: [USAspending.gov](https://www.usaspending.gov) (US Treasury), Award Data Archive bulk files.

Raw data is never committed to this repository.

Known limits, stated on the site:

- Figures are obligations (money committed), not payments.
- The source has documented completeness and accuracy problems.

## How it works

1. **Land**: Python downloads the archive files unchanged.
2. **Convert**: PySpark writes Parquet partitioned by fiscal year and award type; malformed rows go to a reject table.
3. **Model**: one row per award with its modification history.
4. **Resolve**: recipients grouped by federal entity ID and parent.
5. **Aggregate**: summaries by state, county, agency, award type and year.
6. **Track changes**: each refresh is diffed against the last.
7. **Publish**: small summary files served by a static site.

## Layout

| Folder | Purpose |
| --- | --- |
| `ingest/` | Downloader and file checks |
| `pipeline/` | PySpark conversion, award model, aggregates, change tracking |
| `site/` | Static dashboard |
| `docs/decisions/` | Decision records |

## Milestones

| # | Milestone | Proof it works |
| --- | --- | --- |
| 1 | Measure and scope | A written scope decision with the numbers behind it |
| 2 | Raw to Parquet | Row counts match the source files |
| 3 | Award model | Yearly totals per agency land close to the official totals |
| 4 | Map dashboard | Three states checked by hand against the official site |
| 5 | Change log | A second snapshot produces a correct change list |
| 6 | Performance and launch | A full rebuild runs from one command |
