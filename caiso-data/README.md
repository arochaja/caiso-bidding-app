# caiso-data — place raw data files here

The pipeline reads its raw input files from **this folder**. They are **not committed
to git** (the bids and LMP files are hundreds of MB / GB, over GitHub's 100 MB limit,
and the data is supplied separately), so after cloning you must drop them in here yourself.

## Expected files (exact names)

```
caiso-data/
├── 2025-RTM-BIDS.parquet           # CAISO Real-Time Market bids (~352 MB)
├── 2025-DAM-BIDS.parquet           # CAISO Day-Ahead Market bids (~130 MB)
├── 2025-DAM-LMP-full.parquet       # CAISO Day-Ahead LMP, full year (~3 GB)
├── 2025-RTM-LMP/                   # CAISO Real-Time LMP, 5-minute (~50 GB, node-level)
│   ├── RTM_2025-01.parquet … RTM_2025-11.parquet   # one parquet per month (Jan–Nov)
│   └── december parts/*.parquet                     # December split as per-hour parquets
└── OUTAGES_2025-01-01_2025-12-31/  # 365 daily "prior trade date" outage XLSX reports
```

These names are referenced directly in `../app/pipeline.py` and `../app/build_outages_v2.py`.
The pipeline never loads the RTM-LMP files whole — they hold every grid node (~18k), but
every query filters to the three trading-hub nodes first, so only a few seconds per file
are spent. Both the monthly files and the `december parts/` folder are read together
(matched by glob), so adding more months/days needs no code change as long as the schema
(`INTERVALSTARTTIME_GMT, NODE_ID, LMP_TYPE, VALUE, …`) and naming pattern hold.

## Outages: how the parquet is built

CAISO publishes outages as **daily snapshot reports** (one XLSX per trade date). A row
with no `CURTAILMENT END DATE TIME` means the outage was still ongoing **as of that
report's trade date** — not a one-hour blip. `build_outages_v2.py` reads all 365 reports,
preserves the trade date, and fills each null end with the end of its own trade date,
writing a corrected **`2025-OUTAGES-v2.parquet`** (also gitignored).

You don't need to run that by hand: `pipeline.py` builds `2025-OUTAGES-v2.parquet`
automatically on first run if it's missing (and the XLSX folder above is present), then
caches it. To rebuild it explicitly: `.venv/bin/python build_outages_v2.py`.

> The older single-file `2025-OUTAGES.parquet` (a flat concatenation that dropped the
> trade date) is no longer used — it mis-read null ends as 1-hour outages. See the
> dashboard method notes for details.

## Then

From the repo root, run `./app/run.sh` — it builds the virtualenv, runs the preprocessing
pipeline (which reads the files above), and launches the dashboard. See `../app/README.md`
for full details.
