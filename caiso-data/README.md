# caiso-data — place raw data files here

The application reads its two raw input files from **this folder**. They are **not
committed to git** (the bids file is 352 MB, over GitHub's 100 MB limit, and the
data is supplied separately), so after cloning you must drop them in here yourself.

## Expected files (exact names)

```
caiso-data/
├── 2025-RTM-BIDS.parquet     # CAISO Real-Time Market bids (2025, ~69M rows, ~352 MB)
└── 2025-OUTAGES.parquet      # CAISO resource outages / curtailments (~5 MB)
```

These names are referenced directly in `../app/pipeline.py`:

```python
DATA = os.path.abspath(os.path.join(HERE, "..", "caiso-data"))  # this folder
BIDS = os.path.join(DATA, "2025-RTM-BIDS.parquet")
OUTG = os.path.join(DATA, "2025-OUTAGES.parquet")
```

If your files have different names, either rename them to match or edit those two
lines in `pipeline.py`.

## Then

From the repo root, run `./app/run.sh` — it builds the virtualenv, runs the
preprocessing pipeline (which reads the files above), and launches the dashboard.
See `../app/README.md` for full details.
