#!/usr/bin/env python3
"""
Build a corrected 2025-OUTAGES parquet FROM THE GROUND UP, straight from the raw
daily XLSX reports in caiso-data/OUTAGES_2025-01-01_2025-12-31/.

Why this exists
---------------
Each XLSX is a "prior-trade-date" snapshot for ONE trade date (in its header). A row
with a null CURTAILMENT END DATE TIME means the outage was still ongoing *as of that
trade date* — NOT a 1-hour blip. A persistent outage is re-listed every day with the
same anchored start and a null end until it ends. The existing 2025-OUTAGES.parquet
concatenated all reports but DROPPED the trade date, so those null-end rows became
indistinguishable duplicates and their true span was lost.

This builder preserves the trade date and fills each null end with the END OF ITS OWN
TRADE DATE. Keeping one row per report-appearance then lets the UNION of
[start, end_filled] intervals reconstruct the true multi-day span automatically — even
for outages still ongoing at the last report.

Output: caiso-data/2025-OUTAGES-v2.parquet  (v1 is left untouched for comparison)
"""

import glob
import os
import re

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "caiso-data", "OUTAGES_2025-01-01_2025-12-31"))
OUT = os.path.abspath(os.path.join(HERE, "..", "caiso-data", "2025-OUTAGES-v2.parquet"))

HEADER_ROW = 9  # verified constant across all 365 reports
KEEP = [
    "OUTAGE MRID",
    "RESOURCE NAME",
    "RESOURCE ID",
    "OUTAGE TYPE",
    "NATURE OF WORK",
    "CURTAILMENT START DATE TIME",
    "CURTAILMENT END DATE TIME",
    "CURTAILMENT MW",
    "RESOURCE PMAX MW",
    "NET QUALIFYING CAPACITY MW",
]
_TD = re.compile(r"Trade Date\s*:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})")


def log(m):
    print(f"[outages-v2] {m}", flush=True)


def trade_date_of(path):
    """Read the 'Trade Date: M/D/YYYY' cell from the report header (authoritative;
    the filename is unreliable — one file is named 'jan-12-2025')."""
    head = pd.read_excel(path, header=None, nrows=12, engine="openpyxl")
    for val in head.to_numpy().ravel():
        m = _TD.search(str(val))
        if m:
            return pd.to_datetime(m.group(1), format="%m/%d/%Y").normalize()
    raise ValueError(f"no Trade Date cell in {os.path.basename(path)}")


def main():
    files = sorted(glob.glob(os.path.join(SRC, "*.xlsx")))
    log(f"reading {len(files)} daily reports from {SRC}")

    frames = []
    for i, f in enumerate(files, 1):
        td = trade_date_of(f)
        df = pd.read_excel(f, header=HEADER_ROW, engine="openpyxl")
        df.columns = [str(c).strip() for c in df.columns]
        df = df[KEEP].copy()
        df = df[df["OUTAGE MRID"].notna()]  # drop any blank/separator rows
        df["TRADE DATE"] = td
        df["SOURCE FILE"] = os.path.basename(f)
        frames.append(df)
        if i % 50 == 0 or i == len(files):
            log(f"  {i}/{len(files)} reports parsed")

    raw = pd.concat(frames, ignore_index=True)

    # types
    for c in ("CURTAILMENT START DATE TIME", "CURTAILMENT END DATE TIME"):
        raw[c] = pd.to_datetime(raw[c], errors="coerce")
    for c in ("CURTAILMENT MW", "RESOURCE PMAX MW", "NET QUALIFYING CAPACITY MW"):
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    raw["OUTAGE MRID"] = pd.to_numeric(raw["OUTAGE MRID"], errors="coerce").astype("Int64")
    raw["RESOURCE ID"] = raw["RESOURCE ID"].astype(str)
    raw["OUTAGE TYPE"] = raw["OUTAGE TYPE"].astype(str)

    # THE FIX: a null end means "ongoing through this report's trade date".
    # Fill it with the end of that trade date (CAISO's own end-of-period marker is
    # HH:MM = 23:59). Keeping one row per report-appearance means the union of
    # [start, END FILLED] over all rows reconstructs the true span.
    eod = raw["TRADE DATE"] + pd.Timedelta(hours=23, minutes=59)
    raw["CURTAILMENT END FILLED"] = raw["CURTAILMENT END DATE TIME"].fillna(eod)
    raw["END WAS NULL"] = raw["CURTAILMENT END DATE TIME"].isna()

    raw.to_parquet(OUT, index=False)

    log(f"wrote {OUT}")
    log(f"  rows: {len(raw):,}")
    log(f"  trade dates covered: {raw['TRADE DATE'].nunique()} "
        f"({raw['TRADE DATE'].min().date()} → {raw['TRADE DATE'].max().date()})")
    log(f"  null-end rows filled to trade-date end: {int(raw['END WAS NULL'].sum()):,}")
    size = os.path.getsize(OUT) / 1e6
    log(f"  file size: {size:.2f} MB")


if __name__ == "__main__":
    main()
