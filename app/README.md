# CAISO Market Surveillance Dashboard

An interactive market-auditor tool over the CAISO 2025 Real-Time Market data
(`../caiso-data/2025-RTM-BIDS.parquet` + `2025-OUTAGES.parquet`). It runs two
surveillance screens and presents them as clean, slide-ready visuals.

## Quick start

```bash
./run.sh              # first run builds derived data, then opens the dashboard
./run.sh --rebuild    # force re-run the preprocessing pipeline
```

Then open <http://localhost:8531>. (To share a chart in a deck, use the camera
icon on any Plotly chart to export a PNG, or the CSV download buttons for tables.)

## What it does

Two screens, both **self-contained** — no external cost/price data required:

### 1. Economic-Withholding / bid-anomaly
Since the source has **no LMP/clearing price**, we proxy system scarcity with
**hourly forced-outage MW** (from the outages file). For each resource we compare
the share of its offered energy priced at/above an *elevated* threshold in
**tight** hours vs **normal** hours:

> **withholding index = elevated-share(tight) − elevated-share(normal)**

A large positive value = the resource parks capacity at high prices precisely when
the grid is short — the behavioral signature of economic withholding. It is a
*conduct screen* (a lead), not proof; confirmation needs an impact test against
real clearing prices.

### 2. Re-identification / fingerprinting
Demonstrates that the anonymization is **reversible** for many resources. Links an
anonymous `RESOURCEBID_SEQ` to a **named plant** using two public side-channels:

- **Capacity fingerprint** — bidder's offered-MW ceiling ≈ a named resource's PMAX.
- **Outage-timing fingerprint** — a plant on forced outage stops bidding; we score
  the coincidence of the bidder's daily drop-outs with the named resource's daily
  outage pattern using the **Matthews correlation (φ)**. Trivial always-on patterns
  score ~0, so only *distinctive* timing matches survive.

`confidence = 0.60·φ + 0.25·capacity-closeness + 0.15·storage-class-match`

Result on 2025 data: **232 high-confidence (≥0.60) identity links** across ~150
named plants.

## Architecture

```
app/
├── pipeline.py         # DuckDB preprocessing: raw parquet -> compact derived tables
├── dashboard.py        # Streamlit + Plotly UI (4 pages)
├── run.sh              # launcher (venv + pipeline + streamlit)
├── .streamlit/         # theme
└── data/derived/       # pre-computed outputs (built by pipeline.py)
```

The 352 MB / 69 M-row bid file is aggregated **once** by `pipeline.py` into small
(<1 MB) derived tables, so the dashboard loads instantly. Re-run the pipeline only
when the raw data changes.

### Derived tables
| file | contents |
|---|---|
| `market_hourly/daily.parquet` | offered MW, near-cap share, system tightness |
| `product_monthly.parquet` | bid volume by product type |
| `withholding_resource.parquet` | per-resource withholding index + rank |
| `withholding_daily.parquet` | daily drill-down for top offenders |
| `bidder_profiles.parquet` | capacity/storage fingerprint per bidder |
| `bidder_daily_cap.parquet` | daily offered capacity (drill-down overlay) |
| `reident_matches.parquet` | candidate anon→named identity links |
| `resource_outage_daily.parquet` | matched plants' outage days (overlay) |
| `meta.json` | thresholds, coverage, assumptions |

## Key assumptions (see the in-app "Method & Assumptions" page)
- No LMP in source → tightness proxied by forced-outage MW.
- No fuel/heat-rate data → withholding uses a self-referential (tight-vs-normal) benchmark.
- Null-ended forced outages treated as 1-hour (empirical median duration).
- Both screens produce **investigative leads**, not findings of manipulation.
