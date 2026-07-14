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
└── OUTAGES_2025-01-01_2025-12-31/  # 365 daily "prior trade date" outage XLSX reports
```

These names are referenced directly in `../app/pipeline.py` and `../app/build_outages_v2.py`.

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
