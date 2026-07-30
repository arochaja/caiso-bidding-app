#!/usr/bin/env python3
"""
CAISO Market Surveillance — preprocessing pipeline.

Reads the two raw parquet files (2025 RTM bids + outages) and pre-computes
compact derived tables that the Streamlit dashboard loads instantly.

Screens implemented:
  1. Economic-withholding / bid-anomaly  (statistical peer benchmark, no cost data needed)
  2. Re-identification / fingerprinting   (anon RESOURCEBID_SEQ -> named resource)

Run:  python pipeline.py
Outputs land in ./data/derived/
"""

import glob
import json
import os
from collections import defaultdict

import duckdb
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "caiso-data"))
OUT = os.path.join(HERE, "data", "derived")
SCRATCH = os.environ.get("CAISO_SCRATCH", os.path.join(HERE, ".cache"))
os.makedirs(OUT, exist_ok=True)
os.makedirs(SCRATCH, exist_ok=True)

BIDS = os.path.join(DATA, "2025-RTM-BIDS.parquet")
DAMB = os.path.join(DATA, "2025-DAM-BIDS.parquet")  # day-ahead bids (same market as the LMP)
OUTG = os.path.join(
    DATA, "2025-OUTAGES-v2.parquet"
)  # trade-date-preserving rebuild (build_outages_v2.py)
LMP = os.path.join(DATA, "2025-DAM-LMP-full.parquet")  # merged full-year day-ahead LMP
# Real-time (RTM) LMP: 5-minute node-level prices. The raw files are ~50GB (billions
# of rows across ~18k nodes), so we never load them whole — every query filters to the
# three trading-hub nodes up front. Ships as monthly parquets (Jan–Nov) plus a folder
# of per-hour parquets for December; both share the same schema and are read together.
RTM_LMP = [
    os.path.join(DATA, "2025-RTM-LMP", "RTM_2025-*.parquet"),
    os.path.join(DATA, "2025-RTM-LMP", "december parts", "*.parquet"),
]

# the two bid markets scored side by side; RTM is the default/primary market.
MARKETS = {"RTM": BIDS, "DAM": DAMB}

# CAISO trading hubs — the standard regional price references. LMP_TYPE='LMP' is the
# total price; MCE/MCC/MCL are its energy/congestion/loss components (LMP = MCE+MCC+MCL).
HUBS = {
    "SP15": "TH_SP15_GEN-APND",  # Southern California (largest load)
    "NP15": "TH_NP15_GEN-APND",  # Northern California
    "ZP26": "TH_ZP26_GEN-APND",  # Central California
}
SYS_HUBS = list(HUBS.values())  # "system price" = mean LMP across the three hubs

# ---- tunable thresholds (documented in the dashboard "Method" panel) ----
THR_ELEV = 250.0  # $/MWh: capacity offered at/above this is "elevated" (well above ~$32 median)
THR_NEAR = 900.0  # $/MWh: at/above this is "near-cap" ($1000 bid cap) -> classic withholding zone
TIGHT_PCTL = 0.90  # hours with forced-outage MW above this percentile are "system-tight"
PRICE_TIGHT_PCTL = 0.90  # hours with system LMP above this percentile are "price-scarce"
CAP_TOL = 0.12  # re-ident: |bidder_cap - resource_PMAX| / PMAX must be <= this
MIN_ELEV_HOURS = 200  # withholding: min tight-hour presence to be scored
TOP_DRILL = 60  # how many top resources get a stored daily drill-down series


def log(m):
    print(f"[pipeline] {m}", flush=True)


# The outage input is a trade-date-corrected rebuild of CAISO's daily XLSX reports
# (build_outages_v2.py): a null end there means "still ongoing as of that report's trade
# date", not a 1-hour blip. Build it once from the raw XLSX if the parquet isn't present;
# it is cached thereafter so repeat runs stay fast.
if not os.path.exists(OUTG):
    import build_outages_v2

    if not os.path.isdir(build_outages_v2.SRC):
        raise SystemExit(
            f"Outage input missing and cannot be built:\n  need {OUTG}\n"
            f"  or the raw daily XLSX reports in {build_outages_v2.SRC}\n"
            "See caiso-data/README.md."
        )
    log("Outage parquet not found — building it from the raw daily XLSX (one-time)...")
    build_outages_v2.main()

# fail early and clearly if any required raw input is missing
_missing = [p for p in (BIDS, DAMB, OUTG, LMP) if not os.path.exists(p)]
if not any(glob.glob(g) for g in RTM_LMP):
    _missing.append(os.path.join(DATA, "2025-RTM-LMP", "(no RTM parquet files found)"))
if _missing:
    raise SystemExit(
        "Missing required input file(s) in caiso-data/:\n  "
        + "\n  ".join(_missing)
        + "\nSee caiso-data/README.md for the expected files."
    )

con = duckdb.connect()
con.execute("PRAGMA disable_progress_bar")
con.execute("PRAGMA threads=6")
con.execute("PRAGMA memory_limit='6GB'")
con.execute(f"PRAGMA temp_directory='{SCRATCH}/duck_tmp'")

# =====================================================================
# Stage 0: resource-hour aggregation of ENERGY bids (the workhorse table)
# =====================================================================
log("Stage 0: aggregating energy bids to resource-hour grain (this is the heavy step)...")
con.execute(f"""
create or replace table rh as
select
  RESOURCEBID_SEQ                          as res,
  min(SCHEDULINGCOORDINATOR_SEQ)           as sc,  -- deterministic: a resource can be
                                                  -- bid by several SCs; any_value() picked
                                                  -- an arbitrary one that changed per run
  cast(STARTTIME as timestamp)             as h,
  cast(substr(STARTTIME,1,10) as date)     as day,
  max(SCH_BID_XAXISDATA)                   as cap,
  max(SCH_BID_XAXISDATA)
    - coalesce(max(case when SCH_BID_Y1AXISDATA < {THR_ELEV} then SCH_BID_XAXISDATA end),0) as elev_mw,
  max(SCH_BID_XAXISDATA)
    - coalesce(max(case when SCH_BID_Y1AXISDATA < {THR_NEAR} then SCH_BID_XAXISDATA end),0) as near_mw,
  max(SCH_BID_Y1AXISDATA)                  as max_price,
  bool_or(MAXEOHSTATEOFCHARGE is not null) as is_storage
from read_parquet('{BIDS}')
where MARKETPRODUCTTYPE='EN' and SCH_BID_XAXISDATA is not null and SCH_BID_XAXISDATA > 0
  -- GENERATOR only, matching rh_dam below: a withholding screen is about SUPPLY.
  -- Real-time EN bids also carry INTERTIE (import/export schedules) and LOAD (demand);
  -- neither is a generator that can withhold its own capacity, and keeping them here
  -- while rh_dam excludes them made the RTM and DAM headline counts incomparable.
  and RESOURCE_TYPE = 'GENERATOR'
group by RESOURCEBID_SEQ, cast(STARTTIME as timestamp), cast(substr(STARTTIME,1,10) as date)
""")
n_rh = con.execute("select count(*) from rh").fetchone()[0]
log(f"  resource-hours: {n_rh:,}")

# Self-scheduled MW per resource-day, for BOTH markets. `rh` above deliberately keeps only
# PRICED offer curves (SCH_BID_XAXISDATA), because every Screen 1 share is a share of priced
# capacity. But a resource that submits ONLY a fixed self-schedule has no priced curve at all,
# so it vanishes from `rh` — and Stage 4 then reads that absence as the bidder "going quiet"
# when it was in fact running at full output. These tables let Stage 4 tell the two apart.
# Kept separate on purpose: folding self-schedule MW into `cap` would corrupt elev_mw/near_mw,
# which subtract priced-step maxima.
for _tbl, _src in (("ss_day", BIDS), ("ss_day_dam", DAMB)):
    con.execute(f"""
    create or replace table {_tbl} as
    select RESOURCEBID_SEQ as res,
           cast(substr(STARTTIME,1,10) as date) as day,
           max(SELFSCHEDMW) as ss_mw
    from read_parquet('{_src}')
    where MARKETPRODUCTTYPE='EN' and RESOURCE_TYPE='GENERATOR'
      and SELFSCHEDMW is not null and SELFSCHEDMW > 0
    group by 1, 2
    """)
log(
    f"  self-scheduled resource-days: RTM {con.execute('select count(*) from ss_day').fetchone()[0]:,}"
    f"; DAM {con.execute('select count(*) from ss_day_dam').fetchone()[0]:,}"
)

# =====================================================================
# Stage 1: system tightness — forced-outage MW active per hour
# =====================================================================
log("Stage 1: computing hourly system tightness from forced outages...")

# Shared collapsed-segment table, used by BOTH the tightness screen (below) and the
# re-identification calendar (Stage 4). The v2 outage source is CAISO's daily
# "prior trade date" reports concatenated: a persistent outage is re-listed every day
# with the same anchored start, and a null end means "still ongoing as of that report's
# trade date" — already resolved into CURTAILMENT END FILLED by build_outages_v2.py.
# We collapse those per-report duplicates to one segment per (outage, start), taking the
# furthest END FILLED as the true extent. This kills the double-count that inflated
# tightness AND the +1h truncation that hid long/ongoing outages (see outage-null-end).
con.execute(f"""
create or replace table outg_seg as
select
  "OUTAGE MRID"                     as mrid,
  min("RESOURCE ID")                as rid,   -- deterministic (see build_wh)
  min("RESOURCE NAME")              as rname,
  "OUTAGE TYPE"                     as otype,
  "CURTAILMENT START DATE TIME"     as ostart_raw,
  max("CURTAILMENT END FILLED")     as oend_filled,
  max("CURTAILMENT MW")             as cmw,
  max("RESOURCE PMAX MW")           as pmax,
  max("NET QUALIFYING CAPACITY MW") as nqc
from read_parquet('{OUTG}')
where "OUTAGE TYPE" in ('FORCED','PLANNED')
group by "OUTAGE MRID", "OUTAGE TYPE", "CURTAILMENT START DATE TIME"
""")

# Forced-only, for tightness. No start-date filter: an outage that BEGAN before 2025 but
# was still active in 2025 must count — the `hours` join below restricts to 2025 hours.
con.execute("""
create or replace table outg as
select rid, rname,
       date_trunc('hour', ostart_raw) as ostart,
       oend_filled                    as oend,
       cmw, pmax, nqc
from outg_seg
where otype='FORCED'
  and oend_filled >= timestamp '2025-01-01'
""")

con.execute("create or replace table hours as select distinct h from rh order by h")
# Hourly tightness at the RESOURCE grain: each resource contributes its DEEPEST active
# curtailment for the hour, then we add across resources. Never sum cmw across segments —
# CAISO files one physical curtailment as many overlapping segments, all carrying the same
# MW, and summing them invents capacity that was never offline:
#   * one MRID is re-filed as chained sub-intervals (08:00->09:37, 09:37->15:00, ...), and
#     date_trunc above pulls 09:37 back to 09:00, so at hour 09 both the ending and the
#     starting sub-interval match the join (~3-8% of MW every month); and
#   * one curtailment is re-filed under many MRIDs — SunZia Wind North/South alone arrive
#     in late Oct 2025 with ~70 MRIDs each (~8-9% of MW in Nov/Dec).
# Together those inflated December by 18% and put 24% of "system-tight" hours in the set
# for the wrong reason. Per-resource max fixes both at once. The AGGREGATION RULE was
# cross-checked against CAISO's own 2025-12-17 trade-date snapshot, at that snapshot's day
# grain: forced-only, per-resource max sums to 23,319 MW here vs 23,321 MW in CAISO's report
# (forced+planned: 26,901 vs 26,903; naive segment sum of the same rows: 160,255 forced /
# 169,713 forced+planned). Note that check is a DAY-grain, snapshot-scoped comparison — the
# `tight` series below is forced-only and HOURLY, and runs 16,223-18,103 MW on that date, so
# the two are not the same quantity. What transfers is the grain, not the number.
con.execute("""
create or replace table tight as
select hh.h as h,
       coalesce(sum(r.rmw),0) as tight_mw,
       count(r.rid)           as n_out
from hours hh
left join (
  select hh2.h as h, o.rid as rid, max(o.cmw) as rmw
  from hours hh2
  join outg o on (hh2.h >= o.ostart and hh2.h < o.oend)
  group by hh2.h, o.rid
) r on r.h = hh.h
group by hh.h
""")
tight_thr = con.execute(f"select quantile_cont(tight_mw,{TIGHT_PCTL}) from tight").fetchone()[0]
log(
    f"  system-tight threshold (P{int(TIGHT_PCTL * 100)} of hourly forced-outage MW): {tight_thr:,.0f} MW"
)

# =====================================================================
# Stage 1b: PRICE data — day-ahead LMP at the three trading hubs.
#   Gives (a) a real market-price backdrop for its own screen, and (b) a
#   PRICE-based scarcity signal (high-LMP hours) as an alternative to the
#   outage-based one above. Hours align to bid hours: the LMP interval start
#   is GMT; converting to Pacific reproduces the bid STARTTIME wall-clock.
# =====================================================================
log("Stage 1b: hub prices + price-based scarcity signal...")
con.execute(f"""
create or replace table price_hourly as
select
  (INTERVALSTARTTIME_GMT::TIMESTAMPTZ AT TIME ZONE 'America/Los_Angeles') as h,
  case NODE_ID
    when '{HUBS["SP15"]}' then 'SP15'
    when '{HUBS["NP15"]}' then 'NP15'
    when '{HUBS["ZP26"]}' then 'ZP26'
  end as hub,
  max(case when LMP_TYPE='LMP' then MW end) as lmp,
  max(case when LMP_TYPE='MCE' then MW end) as energy,
  max(case when LMP_TYPE='MCC' then MW end) as congestion,
  max(case when LMP_TYPE='MCL' then MW end) as loss
from read_parquet('{LMP}')
where NODE_ID in ('{HUBS["SP15"]}','{HUBS["NP15"]}','{HUBS["ZP26"]}')
group by 1, 2
""")
# system price = mean LMP across the three hubs (the scarcity reference series)
con.execute("""
create or replace table sysprice as
select h,
       avg(lmp)        as sys_price,
       avg(energy)     as sys_energy,
       avg(congestion) as sys_congestion,
       avg(loss)       as sys_loss
from price_hourly group by h
""")
price_thr = con.execute(
    f"select quantile_cont(sys_price,{PRICE_TIGHT_PCTL}) from sysprice"
).fetchone()[0]
log(
    f"  price-scarce threshold (P{int(PRICE_TIGHT_PCTL * 100)} of hourly system LMP): ${price_thr:,.0f}/MWh"
)

# ---- Stage 1b-RTM: REAL-TIME (RTM) LMP at the same three hubs -----------
#   The raw 5-minute files are ~50GB, so we extract just the hub nodes. This
#   gives three things day-ahead LMP alone can't: (a) the real-time price the
#   grid actually settled at, (b) the DAM->RTM spread (day-ahead vs real-time
#   divergence — a classic surveillance signal), and (c) an RTM-based scarcity
#   definition that catches the intra-hour price spikes hourly DAM flattens.
#   Native 5-min is kept for the volatility view. NOTE the timestamp asymmetry
#   between the two price files: the DAM file stores INTERVALSTARTTIME_GMT as a
#   VARCHAR carrying an explicit offset ("...T16:00:00-00:00"), so casting it to
#   TIMESTAMPTZ resolves correctly; the RTM file stores a NAIVE TIMESTAMP already
#   in GMT, so the same cast would interpret it in the session timezone and leave
#   the GMT wall-clock untouched — putting the whole real-time series 7-8 hours
#   late (it once ran to 2026-01-01 07:55 and correlated 0.06 with day-ahead
#   instead of 0.85). It must be read AT TIME ZONE 'UTC' first. Hourly means then
#   align to bid hours exactly as the DAM series does, and we also carry each
#   hour's peak 5-minute value so the spike survives the hourly rollup.
log("Stage 1b-RTM: real-time hub prices (5-min) + RTM scarcity signal...")
con.execute(f"""
create or replace table rtm_5min as
select
  -- naive GMT timestamp -> Pacific. Do NOT use ::TIMESTAMPTZ here (see note above),
  -- and do not rely on a session TimeZone setting for correctness.
  (INTERVALSTARTTIME_GMT AT TIME ZONE 'UTC' AT TIME ZONE 'America/Los_Angeles') as ts,
  case NODE_ID
    when '{HUBS["SP15"]}' then 'SP15'
    when '{HUBS["NP15"]}' then 'NP15'
    when '{HUBS["ZP26"]}' then 'ZP26'
  end as hub,
  max(case when LMP_TYPE='LMP' then VALUE end) as lmp,
  max(case when LMP_TYPE='MCE' then VALUE end) as energy,
  max(case when LMP_TYPE='MCC' then VALUE end) as congestion,
  max(case when LMP_TYPE='MCL' then VALUE end) as loss
from read_parquet({RTM_LMP!r})
where NODE_ID in ('{HUBS["SP15"]}','{HUBS["NP15"]}','{HUBS["ZP26"]}')
group by 1, 2
""")
# hourly rollup per hub: mean price + the hour's peak/trough 5-min value.
con.execute("""
create or replace table rtm_hourly as
select date_trunc('hour', ts) as h, hub,
       avg(lmp)        as lmp,
       max(lmp)        as peak5,
       min(lmp)        as min5,
       avg(energy)     as energy,
       avg(congestion) as congestion,
       avg(loss)       as loss
from rtm_5min group by 1, 2
""")
# system real-time price = mean LMP across the three hubs (RTM scarcity ref).
# Build the SYSTEM 5-minute series first, then roll it up: taking max(peak5) over the
# hourly per-hub rows would give "the highest 5-min print at ANY single hub", which is not
# a system price at all and overstated the system 5-minute peak in 99.5% of hours.
con.execute("""
create or replace table rtm_sys5 as
select ts,
       avg(lmp)        as lmp,
       avg(energy)     as energy,
       avg(congestion) as congestion,
       avg(loss)       as loss
from rtm_5min group by ts
""")
con.execute("""
create or replace table rtm_sysprice as
select date_trunc('hour', ts) as h,
       avg(lmp)        as sys_price,
       max(lmp)        as sys_peak5,
       avg(energy)     as sys_energy,
       avg(congestion) as sys_congestion,
       avg(loss)       as sys_loss
from rtm_sys5 group by 1
""")
price_thr_rtm = con.execute(
    f"select quantile_cont(sys_price,{PRICE_TIGHT_PCTL}) from rtm_sysprice"
).fetchone()[0]
log(
    f"  RTM price-scarce threshold (P{int(PRICE_TIGHT_PCTL * 100)} of hourly system RTM LMP): ${price_thr_rtm:,.0f}/MWh"
)

# per-hour scarcity table carrying ALL definitions, keyed to bid hours:
#   outage (capacity offline), price (day-ahead LMP spike), price_rtm (real-time spike).
con.execute(f"""
create or replace table scar as
select t.h,
       t.tight_mw,
       (t.tight_mw >= {tight_thr})                          as tight_outage,
       p.sys_price,
       (p.sys_price is not null and p.sys_price >= {price_thr}) as tight_price,
       r.sys_price                                          as sys_price_rtm,
       (r.sys_price is not null and r.sys_price >= {price_thr_rtm}) as tight_price_rtm
from tight t
left join sysprice p using (h)
left join rtm_sysprice r using (h)
""")

# price tables for the dashboard: both markets (DAM day-ahead, RTM real-time),
# 3 hubs + a 'SYS' (mean) series each. `peak` = the hour's high (for DAM there is
# only the one hourly value, so peak == lmp; for RTM it's the peak 5-min value).
con.execute("""
create or replace table price_hourly_out as
select 'DAM' as market, h, hub, lmp, lmp as peak, energy, congestion, loss from price_hourly
union all
select 'DAM', h, 'SYS', sys_price, sys_price, sys_energy, sys_congestion, sys_loss from sysprice
union all
select 'RTM', h, hub, lmp, peak5, energy, congestion, loss from rtm_hourly
union all
select 'RTM', h, 'SYS', sys_price, sys_peak5, sys_energy, sys_congestion, sys_loss from rtm_sysprice
""")
con.execute(
    f"copy (select * from price_hourly_out order by market, hub, h) to '{OUT}/price_hourly.parquet' (format parquet)"
)
con.execute(f"""
copy (
  select market, hub, cast(h as date) as day,
         avg(lmp)        as avg_lmp,
         max(peak)       as peak_lmp,
         min(lmp)        as min_lmp,
         avg(congestion) as avg_congestion,
         avg(loss)       as avg_loss,
         sum(case when lmp < 0 then 1 else 0 end) as neg_hours
  from price_hourly_out group by market, hub, day order by market, hub, day
) to '{OUT}/price_daily.parquet' (format parquet)
""")
# native 5-minute RTM series (hubs + SYS mean) for the intraday-volatility view.
con.execute(f"""
copy (
  with sys as (
    select ts, 'SYS' as hub, avg(lmp) as lmp, avg(energy) as energy,
           avg(congestion) as congestion, avg(loss) as loss
    from rtm_5min group by ts
  )
  select ts, hub, lmp, energy, congestion, loss from rtm_5min
  union all select ts, hub, lmp, energy, congestion, loss from sys
  order by hub, ts
) to '{OUT}/rtm_5min.parquet' (format parquet)
""")
price_stats = con.execute("""
select round(avg(sys_price),2), round(median(sys_price),2), round(max(sys_price),2),
       sum(case when sys_price < 0 then 1 else 0 end), count(*)
from sysprice
""").fetchone()
rtm_stats = con.execute("""
select round(avg(sys_price),2), round(median(sys_price),2), round(max(sys_price),2),
       sum(case when sys_price < 0 then 1 else 0 end), count(*)
from rtm_sysprice
""").fetchone()
# DAM->RTM spread on the shared hours (real-time minus day-ahead, system level).
spread_stats = con.execute("""
select round(avg(r.sys_price - p.sys_price),2), round(stddev(r.sys_price - p.sys_price),2),
       round(max(r.sys_price - p.sys_price),1), round(min(r.sys_price - p.sys_price),1),
       count(*)
from rtm_sysprice r join sysprice p using (h)
""").fetchone()
log(
    f"  system LMP: avg ${price_stats[0]}/MWh, peak ${price_stats[2]}/MWh, {price_stats[3]:,} negative-price hours"
)
log(
    f"  RTM system LMP: avg ${rtm_stats[0]}/MWh, peak ${rtm_stats[2]}/MWh, {rtm_stats[3]:,} negative-price hours"
)
log(
    f"  DAM->RTM spread: mean ${spread_stats[0]}/MWh, sd ${spread_stats[1]}, range [${spread_stats[3]}, ${spread_stats[2]}]"
)

# --- build assertions: fail loudly rather than ship a misaligned price series ----------
# These exist because the RTM series once shipped 8 hours late (naive-GMT timestamp read in
# the session timezone). Every symptom below was visible in the output at the time.
_rtm_lo, _rtm_hi = con.execute("select min(ts), max(ts) from rtm_5min").fetchone()
if not (_rtm_lo.year == 2025 and _rtm_hi.year == 2025):
    raise SystemExit(
        f"RTM price series is not inside 2025 ({_rtm_lo} -> {_rtm_hi}) — check the "
        "INTERVALSTARTTIME_GMT timezone conversion in Stage 1b-RTM."
    )
_corr, _shared = con.execute("""
  select corr(r.sys_price, p.sys_price), count(*) from rtm_sysprice r join sysprice p using (h)
""").fetchone()
if _shared < 8000 or _corr is None or _corr < 0.5:
    raise SystemExit(
        f"day-ahead and real-time hourly prices correlate at {_corr} over {_shared} shared "
        "hours; below 0.5 means the two series are not on the same clock."
    )
log(f"  check: DAM/RTM hourly price correlation {_corr:.2f} over {_shared:,} shared hours")

# =====================================================================
# Stage 1c: DAY-AHEAD bids at resource-hour grain — same market as the LMP.
#   Adds `above_clearing_mw`: capacity the resource offered at a price ABOVE the
#   actual day-ahead system clearing price that hour (so it would not clear). This
#   is the real-clearing-price impact test the outage/$250 proxy can't do. `rh`
#   (RTM) is left as the primary market for the overview and re-identification.
# =====================================================================
log("Stage 1c: day-ahead bids + real clearing-price impact...")
con.execute(f"""
create or replace table rh_dam as
select
  b.RESOURCEBID_SEQ                          as res,
  min(b.SCHEDULINGCOORDINATOR_SEQ)           as sc,  -- deterministic (see rh)
  cast(b.STARTTIME as timestamp)             as h,
  cast(substr(b.STARTTIME,1,10) as date)     as day,
  max(b.SCH_BID_XAXISDATA)                   as cap,
  max(b.SCH_BID_XAXISDATA)
    - coalesce(max(case when b.SCH_BID_Y1AXISDATA < {THR_ELEV} then b.SCH_BID_XAXISDATA end),0) as elev_mw,
  max(b.SCH_BID_XAXISDATA)
    - coalesce(max(case when b.SCH_BID_Y1AXISDATA < {THR_NEAR} then b.SCH_BID_XAXISDATA end),0) as near_mw,
  -- capacity priced strictly above the real clearing price (null when no price that hour)
  case when any_value(p.sys_price) is null then null else
    max(b.SCH_BID_XAXISDATA)
      - coalesce(max(case when b.SCH_BID_Y1AXISDATA <= p.sys_price then b.SCH_BID_XAXISDATA end),0)
  end                                        as above_clearing_mw,
  max(b.SCH_BID_Y1AXISDATA)                  as max_price,
  bool_or(b.MAXEOHSTATEOFCHARGE is not null) as is_storage
from read_parquet('{DAMB}') b
left join sysprice p on (cast(b.STARTTIME as timestamp) = p.h)
where b.MARKETPRODUCTTYPE='EN' and b.SCH_BID_XAXISDATA is not null and b.SCH_BID_XAXISDATA > 0
  -- GENERATOR only: a withholding screen is about SUPPLY. Day-ahead EN bids also
  -- include LOAD (demand) and INTERTIE; a load pricing its demand above the clearing
  -- price is willingness-to-pay, not withheld generation, so it must be excluded.
  and b.RESOURCE_TYPE = 'GENERATOR'
group by b.RESOURCEBID_SEQ, cast(b.STARTTIME as timestamp), cast(substr(b.STARTTIME,1,10) as date)
""")
n_rh_dam = con.execute("select count(*) from rh_dam").fetchone()[0]
log(f"  day-ahead resource-hours: {n_rh_dam:,}")

# =====================================================================
# Stage 2: market overview (hourly + daily + monthly product mix)
# =====================================================================
log("Stage 2: market overview tables...")
con.execute("""
create or replace table market_hourly as
select r.h as h, cast(r.h as date) as day,
       count(distinct r.res) as n_res,
       sum(r.cap)            as total_cap_mw,
       sum(r.elev_mw)        as elev_mw,
       sum(r.near_mw)        as near_mw,
       median(r.max_price)   as med_maxprice,
       s.tight_mw            as tight_mw,
       s.tight_outage        as is_tight,
       s.sys_price           as sys_price,
       s.tight_price         as is_tight_price
from rh r join scar s using (h)
group by r.h, s.tight_mw, s.tight_outage, s.sys_price, s.tight_price
order by r.h
""")
con.execute(f"copy market_hourly to '{OUT}/market_hourly.parquet' (format parquet)")

con.execute(f"""
copy (
  select day,
         avg(n_res) as n_res,
         sum(total_cap_mw)/24.0 as avg_cap_mw,
         sum(elev_mw)/nullif(sum(total_cap_mw),0) as elev_share,
         sum(near_mw)/nullif(sum(total_cap_mw),0) as near_share,
         max(tight_mw) as peak_tight_mw,
         sum(case when is_tight then 1 else 0 end) as tight_hours,
         avg(sys_price) as avg_sys_price,
         max(sys_price) as peak_sys_price,
         sum(case when is_tight_price then 1 else 0 end) as price_tight_hours
  from market_hourly group by day order by day
) to '{OUT}/market_daily.parquet' (format parquet)
""")

con.execute(f"""
copy (
  select date_trunc('month', cast(STARTTIME as timestamp)) as month,
         MARKETPRODUCTTYPE as product, count(*) as n_bids
  from read_parquet('{BIDS}')
  group by 1,2 order by 1,2
) to '{OUT}/product_monthly.parquet' (format parquet)
""")

# =====================================================================
# Stage 2b: DEMAND side of the market (the "Demand" panel)
#   The bid files also carry the buy side: RESOURCE_TYPE='LOAD' energy bids.
#   A LOAD offer is either self-scheduled (a fixed, price-insensitive "must-take"
#   quantity in SELFSCHEDMW) or an economic demand curve (SCH_BID_XAXISDATA = MW the
#   load would buy, at the willingness-to-pay in SCH_BID_Y1AXISDATA). Total demand
#   bid by a resource-hour = self-scheduled MW + the top of its economic curve. We
#   sum across resources to an hourly system-demand series, then take each day's peak
#   and average. Kept for BOTH markets so the panel's DAM/RTM toggle mirrors Screen 1;
#   note that real-time demand is barely re-bid (see meta.json["demand"] for the current
#   figures — do not hardcode them here), because load is essentially set day-ahead.
#
#   HOUR KEY: use the interval columns, NOT STARTTIME. For a self-scheduled LOAD row
#   STARTTIME is the 24-hour ENVELOPE (00:00 -> next-day 00:00) and the actual hour lives
#   in TIMEINTERVALSTART; economic curve rows carry theirs in SCH_BID_TIMEINTERVALSTART.
#   Keying on STARTTIME piled every self-scheduled MW into hour 00 and then divided it
#   across 24 hours, which understated average day-ahead demand 3x (9.7 GW vs 29.2 GW)
#   and inverted the must-take share (12% vs 71%). Verified: no LOAD row has both
#   interval columns null, in either market.
# =====================================================================
log("Stage 2b: demand-side (LOAD) bids...")
demand_selects = []
for market, path in MARKETS.items():
    demand_selects.append(f"""
    select '{market}' as market, day,
           max(tot_mw)  as demand_peak_mw,
           avg(tot_mw)  as demand_avg_mw,
           max(econ_mw) as econ_peak_mw,
           avg(econ_mw) as econ_avg_mw,
           max(self_mw) as self_peak_mw,
           avg(self_mw) as self_avg_mw
    from (
      select day, h,
             sum(dem_mw)  as tot_mw,
             sum(econ_mw) as econ_mw,
             sum(self_mw) as self_mw
      from (
        select cast(coalesce(TIMEINTERVALSTART, SCH_BID_TIMEINTERVALSTART) as date)      as day,
               cast(coalesce(TIMEINTERVALSTART, SCH_BID_TIMEINTERVALSTART) as timestamp) as h,
               coalesce(max(SCH_BID_XAXISDATA),0)
                 + max(coalesce(SELFSCHEDMW,0))     as dem_mw,
               coalesce(max(SCH_BID_XAXISDATA),0)   as econ_mw,
               max(coalesce(SELFSCHEDMW,0))         as self_mw
        from read_parquet('{path}')
        where MARKETPRODUCTTYPE='EN' and RESOURCE_TYPE='LOAD'
          and coalesce(TIMEINTERVALSTART, SCH_BID_TIMEINTERVALSTART) is not null
        group by 1, 2, RESOURCEBID_SEQ
      ) dh
      group by day, h
    ) hourly
    group by day
    """)
con.execute(f"""
copy (
  {" union all ".join(demand_selects)}
  order by market, day
) to '{OUT}/demand_daily.parquet' (format parquet)
""")
demand_stats = con.execute(f"""
  select round(avg(demand_avg_mw),0), round(max(demand_peak_mw),0),
         round(avg(self_avg_mw)/nullif(avg(demand_avg_mw),0)*100,1)
  from read_parquet('{OUT}/demand_daily.parquet') where market='DAM'
""").fetchone()
log(
    f"  day-ahead demand: avg {demand_stats[0]:,.0f} MW, peak {demand_stats[1]:,.0f} MW, "
    f"{demand_stats[2]}% must-take (self-scheduled)"
)

# =====================================================================
# Stage 3: ECONOMIC-WITHHOLDING screen
#   withholding_index = (elevated share of offered MW in tight hours)
#                     - (elevated share in normal hours)
#   Positive & large => shifts capacity to high prices exactly when system is short.
# =====================================================================
# Scored across two dimensions, stored as `market` + `basis` columns the dashboard
# toggles: market in {RTM (real-time), DAM (day-ahead)} × basis in {outage, price
# (day-ahead LMP spike), price_rtm (real-time LMP spike)}. For the DAM market the
# resource-hours also carry `above_clearing_mw`, so the DAM view adds a real
# clearing-price impact test (capacity offered above the price that would have
# cleared it) on top of the fixed-$threshold conduct index.
log("Stage 3: economic-withholding screen (RTM & DAM markets × outage/price/price_rtm bases)...")
BASES = [
    ("outage", "tight_outage"),
    ("price", "tight_price"),
    ("price_rtm", "tight_price_rtm"),
]
MARKET_RH = [("RTM", "rh"), ("DAM", "rh_dam")]


def build_wh(market, rh_table, basis, flagcol):
    """Withholding for one (market, basis). Creates wh_<market>_<basis> and its
    top_wh_<market>_<basis>. DAM tables carry the clearing-price impact columns;
    RTM tables carry nulls there so all the tables union cleanly."""
    tag = f"{market}_{basis}"
    has_impact = rh_table == "rh_dam"
    impact_agg = (
        "sum(case when is_tight then above_clearing_mw else 0 end) as above_t,\n"
        "        sum(case when not is_tight then above_clearing_mw else 0 end) as above_n,"
        if has_impact
        else "cast(null as double) as above_t, cast(null as double) as above_n,"
    )
    con.execute(f"""
    create or replace table rh2_{tag} as
    select r.*, s.{flagcol} as is_tight
    from {rh_table} r
    join (select h, {flagcol} from scar) s using (h)
    """)
    con.execute(f"""
    create or replace table wh_{tag} as
    with agg as (
      -- min()/bool_or(), not any_value(): a resource can carry different SCs across hours,
      -- so an arbitrary pick made this table differ between identical runs.
      select res, min(sc) as sc, bool_or(is_storage) as is_storage,
        count(*) as en_hours,
        sum(case when is_tight then 1 else 0 end) as tight_hours,
        sum(case when is_tight then cap     else 0 end) as cap_t,
        sum(case when is_tight then elev_mw else 0 end) as elev_t,
        sum(case when is_tight then near_mw else 0 end) as near_t,
        sum(case when not is_tight then cap     else 0 end) as cap_n,
        sum(case when not is_tight then elev_mw else 0 end) as elev_n,
        {impact_agg}
        median(max_price) as med_price,
        max(cap) as cap_max
      from rh2_{tag} group by res
    )
    select *,
      '{market}' as market, '{basis}' as basis,
      (elev_t/nullif(cap_t,0)) as hi_share_tight,
      (elev_n/nullif(cap_n,0)) as hi_share_normal,
      (elev_t/nullif(cap_t,0)) - (elev_n/nullif(cap_n,0)) as withholding_index,
      near_t as nearcap_mwh_tight,
      (above_t/nullif(cap_t,0)) as above_share_tight,
      (above_n/nullif(cap_n,0)) as above_share_normal,
      (above_t/nullif(cap_t,0)) - (above_n/nullif(cap_n,0)) as impact_index,
      above_t as withheld_mwh_tight
    from agg
    where tight_hours >= {MIN_ELEV_HOURS} and cap_t > 0
    """)
    con.execute(f"""
    create or replace table top_wh_{tag} as
    select res from wh_{tag}
    -- `res` breaks ties so this picker and the rank column below cannot disagree:
    -- 70-75% of scored resources have withholding_index AND nearcap_mwh_tight of exactly 0.
    order by withholding_index desc, nearcap_mwh_tight desc, res limit {TOP_DRILL}
    """)
    return con.execute(f"select count(*) from wh_{tag}").fetchone()[0]


n_wh_by = {}
for market, rh_table in MARKET_RH:
    for basis, flagcol in BASES:
        n_wh_by[(market, basis)] = build_wh(market, rh_table, basis, flagcol)
        log(f"  scored {n_wh_by[(market, basis)]:,} resources — {market} market, {basis} basis")
n_wh = n_wh_by[("RTM", "outage")]  # backward-compatible headline count

# union fragments over every (market, basis) combo — generated so adding a basis
# to BASES flows through the result tables automatically (no hardcoded combo lists).
_wh_union = "\n    union all ".join(
    f"select * from wh_{m}_{b}" for m, _ in MARKET_RH for b, _ in BASES
)
_top_union = "\n    union all ".join(
    f"select res, '{m}' as market, '{b}' as basis from top_wh_{m}_{b}"
    for m, _ in MARKET_RH
    for b, _ in BASES
)
_basis_union = "\n    union all ".join(
    f"select h, '{b}' as basis, {flag} as was_tight from scar" for b, flag in BASES
)

# resource-level results for all (market, basis) combos, ranked within each.
con.execute(f"""
copy (
  select res, sc, is_storage, cap_max, en_hours, tight_hours, med_price,
         hi_share_tight, hi_share_normal, withholding_index, nearcap_mwh_tight,
         above_share_tight, above_share_normal, impact_index, withheld_mwh_tight,
         market, basis,
         row_number() over (
           partition by market, basis
           order by withholding_index desc, nearcap_mwh_tight desc, res  -- `res`: see top_wh
         ) as rank
  from (
    {_wh_union}
  )
  order by market, basis, withholding_index desc, nearcap_mwh_tight desc, res
) to '{OUT}/withholding_resource.parquet' (format parquet)
""")

# daily drill-down for each (market, basis)'s own worst offenders; had_tight_hour
# reflects that basis's scarcity definition and the drill uses that market's bids.
con.execute("""
create or replace table rh_long as
select 'RTM' as market, res, h, day, cap, elev_mw, near_mw from rh
union all
select 'DAM' as market, res, h, day, cap, elev_mw, near_mw from rh_dam
""")
con.execute(f"""
copy (
  select r.res, r.day, tw.market, tw.basis,
         sum(r.cap) as cap, sum(r.elev_mw) as elev_mw, sum(r.near_mw) as near_mw,
         sum(r.elev_mw)/nullif(sum(r.cap),0) as hi_share,
         max(case when sc.was_tight then 1 else 0 end) as had_tight_hour
  from rh_long r
  join (
    {_top_union}
  ) tw on (r.res = tw.res and r.market = tw.market)
  join (
    {_basis_union}
  ) sc on (r.h = sc.h and sc.basis = tw.basis)
  group by r.res, r.day, tw.market, tw.basis
  order by tw.market, tw.basis, r.res, r.day
) to '{OUT}/withholding_daily.parquet' (format parquet)
""")

# =====================================================================
# Stage 4: RE-IDENTIFICATION / fingerprinting
# =====================================================================
log("Stage 4: re-identification / fingerprinting...")

# 4a. bidder profiles (capacity fingerprint + storage tag)
bidder = con.execute("""
select res,
       min(sc) as sc,  -- deterministic (see rh)
       bool_or(is_storage) as is_storage,
       quantile_cont(cap,0.99) as cap_ref,
       max(cap) as cap_max,
       avg(cap) as cap_avg,
       count(*) as active_hours,
       min(day) as first_day, max(day) as last_day
from rh group by res
""").fetchdf()

# bidder daily capacity (for dip detection + drill-down overlay)
con.execute(f"""
copy (
  select res, day, max(cap) as cap from rh group by res, day order by res, day
) to '{OUT}/bidder_daily_cap.parquet' (format parquet)
""")
bday = con.execute("select res, day, max(cap) cap from rh group by res, day").fetchdf()

log("  building day-sets and scoring candidate matches...")

# Re-id leverages BOTH forced and planned outages. (Screen 1 'tightness'
# stays forced-only — planned outages aren't unexpected scarcity.) We build
# a dedicated outage table that keeps the outage TYPE, then score the
# fingerprint twice: forced-only, and the merged "unavailable" set
# (forced OR planned). The dashboard toggles between the two.
# Built from the collapsed segments (Stage 1). Clip each segment to the 2025 window so
# day-expansion stays in-year: outages that began before 2025 (some run for years) are
# clamped up to Jan 1, and ongoing ones are clamped down to Dec 31 — otherwise
# build_res_days would emit years of out-of-range days.
con.execute("""
create or replace table outg_reid as
select rid, rname, otype,
       date_trunc('hour', greatest(ostart_raw, timestamp '2025-01-01')) as ostart,
       least(oend_filled, timestamp '2025-12-31 23:59:00')              as oend,
       cmw, pmax, nqc
from outg_seg
where oend_filled >= timestamp '2025-01-01'
  and ostart_raw  <  timestamp '2026-01-01'
""")

# resource metadata (name / PMAX / NQC / storage tag) from the union of
# forced+planned records, so planned-only resources are candidates too.
res_intervals = con.execute("""
select rid, min(rname) rname, max(pmax) pmax, max(nqc) nqc from outg_reid group by rid
""").fetchdf()


def name_is_storage(n):
    if not isinstance(n, str):
        return False
    n = n.upper()
    return any(k in n for k in ("STORAGE", "BATTERY", "BESS", "ENERGY STORAGE", " ES", "_ES"))


res_meta = {}
for rid, rname, pmax, nqc in res_intervals[["rid", "rname", "pmax", "nqc"]].itertuples(index=False):
    res_meta[rid] = dict(rname=rname, pmax=pmax, nqc=nqc, storage=name_is_storage(rname))


# expand outage intervals to daily coverage, split by type
def build_res_days(where_clause):
    rows = con.execute(f"select rid, ostart, oend from outg_reid {where_clause}").fetchdf()
    rd = defaultdict(set)
    for rid, s, e in rows.itertuples(index=False):
        d0 = pd.Timestamp(s).normalize()
        # The tightness join is half-open [start, end), so an outage ending exactly at
        # midnight does not cover that final day at all. Step back a second before
        # normalising, or a zero-minute day enters the calendar and inflates phi/recall.
        d1 = max(d0, (pd.Timestamp(e) - pd.Timedelta(seconds=1)).normalize())
        for d in pd.date_range(d0, d1, freq="D"):
            rd[rid].add(d.date())
    return rd


res_days_forced = build_res_days("where otype='FORCED'")
res_days_planned = build_res_days("where otype='PLANNED'")
res_days_combined = build_res_days("")  # forced OR planned

# bidder -> dip-days (mode-independent: derived from gaps in the bidder's OWN
# offers). A plant on ANY outage typically STOPS bidding (absent day) or
# offers collapsed capacity vs its TYPICAL day. Reference = median of present
# daily caps (robust); dip-days = {absent days in span} U {present days < 0.35*median}.
present_cap = defaultdict(dict)  # res -> {date: cap}
for res, day, cap in bday.itertuples(index=False):
    present_cap[res][pd.Timestamp(day).date()] = cap


def merge_selfsched(pc, tbl):
    """Count a self-scheduled day as PRESENT at its self-scheduled MW.

    A fixed self-schedule is the opposite of going quiet — it is a commitment to run
    regardless of price. Without this, a resource that switched from priced offers to a
    self-schedule looked absent, and Stage 4 scored that absence as a went-quiet day that
    could coincide with a plant outage. Takes the max of the two so a day with BOTH a priced
    curve and a self-schedule keeps whichever is larger.
    """
    added = 0
    for res, day, ss_mw in (
        con.execute(f"select res, day, ss_mw from {tbl}").fetchdf().itertuples(index=False)
    ):
        dd = pd.Timestamp(day).date()
        if float(ss_mw) > pc[res].get(dd, 0.0):
            added += dd not in pc[res]
            pc[res][dd] = float(ss_mw)
    return added


_ss_added = merge_selfsched(present_cap, "ss_day")
log(f"  real-time days reclassified absent -> self-scheduled: {_ss_added:,}")
bref = {res: float(np.median(list(caps.values()))) for res, caps in present_cap.items()}

# Days on which the market as a whole has NO bids are data gaps, not behaviour: nobody
# can go quiet on a day the file does not cover. 2025-03-09 is absent from both bid files,
# and leaving it in made it a dip day for 80% of real-time bidders — half the quiet days of
# a typical bidder, feeding straight into phi. Days like that are excluded from both the
# dip sets and the span length N.
market_days = {
    "RTM": {
        pd.Timestamp(d).date() for d in con.execute("select distinct day from rh").fetchdf()["day"]
    },
    "DAM": {
        pd.Timestamp(d).date()
        for d in con.execute("select distinct day from rh_dam").fetchdf()["day"]
    },
}


def _span_days_in_market(first_day, last_day, mkt):
    """Calendar days in [first, last] that the market actually has data for."""
    return [
        d.date()
        for d in pd.date_range(pd.Timestamp(first_day), pd.Timestamp(last_day), freq="D")
        if d.date() in market_days[mkt]
    ]


bidder_dip_days = defaultdict(set)
for b in bidder.itertuples(index=False):
    ref = bref.get(b.res, 0)
    if not ref or ref <= 0:
        continue
    caps = present_cap.get(b.res, {})
    dips = set()
    for dd in _span_days_in_market(b.first_day, b.last_day, "RTM"):
        c = caps.get(dd)
        if c is None or c < 0.35 * ref:  # absent, or genuine collapse vs typical day
            dips.add(dd)
    bidder_dip_days[b.res] = dips

span_len = {
    b.res: len(_span_days_in_market(b.first_day, b.last_day, "RTM"))
    for b in bidder.itertuples(index=False)
}
span_first = {b.res: pd.Timestamp(b.first_day).date() for b in bidder.itertuples(index=False)}
span_last = {b.res: pd.Timestamp(b.last_day).date() for b in bidder.itertuples(index=False)}

# ---------------------------------------------------------------------
# DAM CROSS-CHECK structures: the same went-quiet dip-day logic applied to the
# DAY-AHEAD offers. Used to independently corroborate each RTM-based match —
# if the bidder ALSO goes quiet in the day-ahead market on the candidate plant's
# outage days, that is a second, market-independent line of evidence.
# ---------------------------------------------------------------------
bday_dam = con.execute("select res, day, max(cap) cap from rh_dam group by res, day").fetchdf()
bidder_dam = con.execute(
    "select res, min(day) first_day, max(day) last_day from rh_dam group by res"
).fetchdf()
present_cap_dam = defaultdict(dict)
for res, day, cap in bday_dam.itertuples(index=False):
    present_cap_dam[res][pd.Timestamp(day).date()] = cap
_ss_added_dam = merge_selfsched(present_cap_dam, "ss_day_dam")
log(f"  day-ahead days reclassified absent -> self-scheduled: {_ss_added_dam:,}")
bref_dam = {res: float(np.median(list(c.values()))) for res, c in present_cap_dam.items()}
dam_span = {
    r.res: (
        pd.Timestamp(r.first_day).date(),
        pd.Timestamp(r.last_day).date(),
        len(_span_days_in_market(r.first_day, r.last_day, "DAM")),
    )
    for r in bidder_dam.itertuples(index=False)
}
bidder_dip_days_dam = defaultdict(set)
for res, (d0, d1, _N) in dam_span.items():
    ref = bref_dam.get(res, 0)
    if not ref or ref <= 0:
        continue
    caps = present_cap_dam.get(res, {})
    for dd in _span_days_in_market(d0, d1, "DAM"):  # skip market-wide data gaps
        c = caps.get(dd)
        if c is None or c < 0.35 * ref:
            bidder_dip_days_dam[res].add(dd)

# ---------------------------------------------------------------------
# MAGNITUDE signal (leverages PARTIAL curtailments, not just full drop-outs):
# correlate a plant's daily curtailment fraction against the bidder's daily
# reduction in offered capacity. A plant losing 20% of PMAX should offer ~20%
# less — invisible to the binary dip test, but caught by this graded signal.
# ---------------------------------------------------------------------
# Plant: fraction of PMAX curtailed each day (forced OR planned), max concurrent.
oc = con.execute("select rid, ostart, oend, cmw, pmax from outg_reid where pmax > 0").fetchdf()
res_curt_frac = defaultdict(dict)  # rid -> {day: curtailed_fraction in (0,1]}
res_curt_mw = defaultdict(dict)  # rid -> {day: max concurrent curtailment MW}  (for hover)
for rid, s, e, cmw, pmax in oc.itertuples(index=False):
    mw = float(cmw) if cmw is not None else 0.0
    f = min(1.0, mw / pmax) if pmax else 0.0
    _d0 = pd.Timestamp(s).normalize()
    _d1 = max(_d0, (pd.Timestamp(e) - pd.Timedelta(seconds=1)).normalize())  # see build_res_days
    for d in pd.date_range(_d0, _d1, freq="D"):
        dd = d.date()
        if mw > res_curt_mw[rid].get(dd, -1.0):
            res_curt_mw[rid][dd] = mw
        if f > res_curt_frac[rid].get(dd, 0.0):
            res_curt_frac[rid][dd] = f

# Bidder: offered-capacity reduction per day over its active span (aligned array):
# 0 = offering its typical peak, 1 = offering nothing / absent.
span_days, bidder_reduction = {}, {}
for b in bidder.itertuples(index=False):
    typ = bref.get(b.res, 0)
    if not typ or typ <= 0:
        continue
    dd = [
        d.date()
        for d in pd.date_range(pd.Timestamp(b.first_day), pd.Timestamp(b.last_day), freq="D")
    ]
    caps = present_cap.get(b.res, {})
    span_days[b.res] = dd
    bidder_reduction[b.res] = np.array(
        [1.0 if caps.get(x) is None else float(np.clip(1 - caps[x] / typ, 0.0, 1.0)) for x in dd]
    )


def spearman(x, y):
    # rank correlation; NaN when either series is constant or too short.
    if len(x) < 5 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    rx = pd.Series(x).rank().values
    ry = pd.Series(y).rank().values
    c = np.corrcoef(rx, ry)[0, 1]
    return float(c) if np.isfinite(c) else float("nan")


def phi_coeff(dips, odays_span, N):
    # Matthews correlation between two binary day-vectors of length N.
    n11 = len(dips & odays_span)
    n10 = len(dips) - n11
    n01 = len(odays_span) - n11
    n00 = N - n11 - n10 - n01
    num = n11 * n00 - n10 * n01
    den = (n11 + n10) * (n11 + n01) * (n00 + n10) * (n00 + n01)
    return (num / (den**0.5)) if den > 0 else 0.0, n11


def dam_crosscheck(res, cand_rid, res_days):
    """Corroborate a match using the DAY-AHEAD offers: φ of the resource's DAM dip-days
    against the candidate plant's outage days, over the resource's DAM span. Returns
    (phi_dam, overlap_days, corroborates); NaN/False when the resource has no usable DAM
    dip pattern.

    `res_days` MUST be the same outage calendar the calling mode scored its RTM φ against
    — it used to be hard-coded to the forced+planned set, so forced-only matches were
    cross-checked against a calendar that included planned maintenance they never used.
    That mislabelled real corroborations as failures (e.g. HELMS PUMP-GEN UNIT 1: φ_dam
    0.224 against forced+planned vs 0.554 on its own forced-only calendar).

    Note this is a second, SEPARATE bid stream rather than fully independent evidence:
    both tests compare against the same plant outage calendar.
    """
    if res not in dam_span or cand_rid not in res_days:
        return float("nan"), 0, False
    d0, d1, N = dam_span[res]
    dips = bidder_dip_days_dam.get(res, set())
    if len(dips) < 3 or N < 30:
        return float("nan"), 0, False
    odays_span = {d for d in res_days[cand_rid] if d0 <= d <= d1}
    if not odays_span or len(odays_span) > 0.9 * N:
        return float("nan"), 0, False
    phi, inter = phi_coeff(dips, odays_span, N)
    return float(phi), int(inter), bool(phi >= 0.30 and inter >= 3)


def run_reident(res_days, magnitude=False, gated=True, topk=3):
    """Score bidders against PMAX-matching candidate resources.

    Binary modes (magnitude=False): confidence = 0.60*phi + 0.25*size + 0.15*type,
      admitting candidates whose timing overlap phi >= 0.30.
    Magnitude mode (magnitude=True): also correlate the plant's daily curtailment
      fraction against the bidder's daily offered-reduction (Spearman rho), admit on
      strong timing OR strong magnitude, and blend
      confidence = 0.35*phi + 0.30*rho + 0.25*size + 0.10*type.

    gated=True reproduces the published screens: only fingerprint-able bidders are
    scored and only admitted candidates are kept. gated=False scores EVERY bidder with
    a usable capacity and keeps its best `topk` candidates regardless of whether the
    admission tests pass, tagging each row with `admitted` and `fingerprintable`. That
    powers the per-bidder lookup panel, which must return the best available answer for
    any ID a user pastes in — including "the only thing matching here is size".
    Returns (sorted DF, n_fingerprintable)."""
    res_list = [
        (rid, res_meta[rid]["pmax"])
        for rid in res_days
        if rid in res_meta
        and res_meta[rid]["pmax"]
        and not np.isnan(res_meta[rid]["pmax"])
        and len(res_days[rid]) >= 4
    ]
    # Sort by PMAX, then by rid: ties must break deterministically. Python's sort is stable,
    # so without the rid the order fell through to however DuckDB happened to return the
    # outage rows that run, which then decided WHICH tied candidates survived cands[:topk].
    res_list.sort(key=lambda x: (x[1], x[0]))
    res_pmax = np.array([p for _, p in res_list]) if res_list else np.array([0.0])
    res_ids = [r for r, _ in res_list]
    matches, n_fp = [], 0
    for b in bidder.itertuples(index=False):
        cap = b.cap_ref
        if not cap or cap <= 0:
            continue
        dips = bidder_dip_days.get(b.res, set())
        N = span_len.get(b.res, 0)
        if not N or b.res not in span_first:
            continue
        if magnitude:
            # fingerprint-able if it has a clear dip pattern OR sustained partial reduction
            red_days = (
                int(np.count_nonzero(bidder_reduction[b.res] >= 0.10))
                if b.res in bidder_reduction
                else 0
            )
            fingerprintable = (
                b.res in span_days
                and N >= 30
                and len(dips) <= 0.85 * N
                and (len(dips) >= 3 or red_days >= 10)
            )
        else:
            # need a specific, non-degenerate dip pattern to fingerprint on
            fingerprintable = 3 <= len(dips) <= 0.7 * N and N >= 30
        if gated and not fingerprintable:
            continue
        n_fp += fingerprintable
        # Candidate PMAX window. Inverted on purpose: the published size gap (cap_diff_pct,
        # and cap_close below) divides by PMAX, so a window of cap*(1 +/- TOL) let matches
        # through at |cap-pmax|/pmax = TOL/(1-TOL) = 13.6% under a stated +/-12% rule.
        # pmax in [cap/(1+TOL), cap/(1-TOL)]  <=>  |cap-pmax|/pmax <= TOL, exactly.
        lo, hi = cap / (1 + CAP_TOL), cap / (1 - CAP_TOL)
        i0 = int(np.searchsorted(res_pmax, lo, side="left"))
        i1 = int(np.searchsorted(res_pmax, hi, side="right"))
        d0, d1 = span_first[b.res], span_last[b.res]
        sdays = span_days.get(b.res)
        yred = bidder_reduction.get(b.res)
        cands = []
        for j in range(i0, i1):
            rid = res_ids[j]
            pmax = res_pmax[j]
            # Exact tolerance test on the published denominator (the searchsorted window
            # above is only a fast pre-filter), plus a physical sanity check: a bidder that
            # has offered MORE than the plant's PMAX cannot be that plant. cap_ref is a P99,
            # so a bidder with a low P99 but a high peak used to slip through on size alone.
            if not pmax or abs(cap - pmax) / pmax > CAP_TOL:
                continue
            if b.cap_max and b.cap_max > pmax * (1 + CAP_TOL):
                continue
            odays = res_days.get(rid)
            if not odays:
                continue
            odays_span = {d for d in odays if d0 <= d <= d1}
            if (
                not odays_span or len(odays_span) > 0.9 * N
            ):  # down ~all span carries no timing signal
                continue
            phi, inter = phi_coeff(dips, odays_span, N)
            rho, n_curt = float("nan"), 0
            if magnitude and sdays is not None:
                cf = res_curt_frac.get(rid, {})
                xarr = np.fromiter((cf.get(dd, 0.0) for dd in sdays), dtype=float, count=len(sdays))
                n_curt = int(np.count_nonzero(xarr))
                if n_curt >= 10:  # balanced guard: enough curtailment days
                    rho = spearman(xarr, yred)
            cap_close = 1 - abs(cap - pmax) / (pmax if pmax else 1)
            stor_match = b.is_storage == res_meta[rid]["storage"]
            if magnitude:
                phi_ok = phi >= 0.30 and inter >= 3
                rho_ok = (not np.isnan(rho)) and rho >= 0.35  # balanced guard
                admitted = bool(phi_ok or rho_ok)
                if gated and not admitted:
                    continue
                conf = (
                    0.35 * max(phi, 0.0)
                    + 0.30 * (max(rho, 0.0) if not np.isnan(rho) else 0.0)
                    + 0.25 * cap_close
                    + (0.10 if stor_match else 0.0)
                )
            else:
                admitted = bool(phi >= 0.30 and inter >= 3)  # distinctive timing coincidence
                if gated and not admitted:
                    continue
                conf = 0.60 * phi + 0.25 * cap_close + (0.15 if stor_match else 0.0)
            recall = inter / len(dips) if dips else 0.0
            jacc = inter / len(dips | odays_span) if (dips or odays_span) else 0.0
            cands.append((rid, pmax, cap_close, recall, jacc, phi, conf, inter, rho, admitted))
        # Confidence, then rid. Candidates frequently tie (identical size match, no timing
        # signal), and the truncation below keeps only the first few — so an unstable tie
        # order silently changed the published match lists between otherwise identical runs.
        cands.sort(key=lambda x: (-x[6], x[0]))
        for rank, (
            rid,
            pmax,
            cap_close,
            recall,
            jacc,
            phi,
            conf,
            inter,
            rho,
            admitted,
        ) in enumerate(cands[:topk], start=1):
            m = res_meta[rid]
            rec = dict(
                res=b.res,
                sc=b.sc,
                is_storage=bool(b.is_storage),
                bidder_cap=round(float(cap), 2),
                cand_rid=rid,
                cand_name=m["rname"],
                cand_pmax=float(pmax),
                cap_diff_pct=round(abs(cap - pmax) / pmax * 100, 2),
                dip_days=len(dips),
                overlap_days=int(inter),
                recall=round(float(recall), 3),
                jaccard=round(float(jacc), 3),
                phi=round(float(phi), 3),
                confidence=round(float(conf), 3),
                rank=rank,
            )
            if magnitude:
                rec["rho"] = None if np.isnan(rho) else round(float(rho), 3)
            if not gated:
                rec["admitted"] = bool(admitted)
                rec["fingerprintable"] = bool(fingerprintable)
                rec["size_match"] = round(float(cap_close), 3)
                rec["type_match"] = bool(stor_match)
                rec["span_days"] = int(N)
            # day-ahead cross-check: does the bidder also go quiet in DAM on this
            # candidate's outage days? Scored against THIS mode's own outage calendar.
            phi_dam, ov_dam, corr_dam = dam_crosscheck(b.res, rid, res_days)
            rec["phi_dam"] = None if np.isnan(phi_dam) else round(phi_dam, 3)
            rec["overlap_days_dam"] = ov_dam
            rec["dam_corroborates"] = corr_dam
            if not gated:
                # Denominators for the day-ahead side, so the lookup panel can state the
                # cross-check symmetrically with the real-time one ("N of M quiet days")
                # instead of showing an overlap count with nothing to compare it against.
                rec["dip_days_dam"] = len(bidder_dip_days_dam.get(b.res, set()))
                rec["span_days_dam"] = int(dam_span[b.res][2]) if b.res in dam_span else 0
            matches.append(rec)
    mdf = pd.DataFrame(matches)
    if len(mdf):
        mdf = mdf.sort_values(
            ["confidence", "res", "cand_rid"], ascending=[False, True, True]
        ).reset_index(drop=True)
    return mdf, n_fp


log("  scoring fingerprints (forced; forced+planned; magnitude-aware)...")
mdf_forced, fp_forced = run_reident(res_days_forced)
mdf_combined, fp_comb = run_reident(res_days_combined)
mdf_magnitude, fp_mag = run_reident(res_days_combined, magnitude=True)
# Ungated top-5 per bidder for the lookup panel (Stage 5 writes it). Computed here so the
# daily outage overlay built below also covers plants that only appear as lookup candidates.
mdf_lookup, _fp_lookup = run_reident(res_days_combined, magnitude=True, gated=False, topk=5)

mdf_forced.to_parquet(f"{OUT}/reident_matches_forced.parquet", index=False)
mdf_combined.to_parquet(f"{OUT}/reident_matches_combined.parquet", index=False)
mdf_magnitude.to_parquet(f"{OUT}/reident_matches_magnitude.parquet", index=False)
bidder.to_parquet(f"{OUT}/bidder_profiles.parquet", index=False)

# hourly offered-capacity series for the drill-down (matched bidders only), so the
# chart can show hour-level break points instead of daily maxes.
matched_res = set()
for mdf in (mdf_forced, mdf_combined, mdf_magnitude):
    if len(mdf):
        matched_res |= set(mdf[mdf["rank"] == 1]["res"].unique())
mres_df = pd.DataFrame({"res": sorted(matched_res)})
con.register("mres_df", mres_df)
con.execute(f"""
copy (select r.res, r.h, r.cap from rh r join mres_df using (res) order by r.res, r.h)
to '{OUT}/bidder_hourly_cap.parquet' (format parquet)
""")
# day-ahead (DAM) counterpart of the same series, so the fingerprint drill-down can
# show the bidder's went-quiet pattern in BOTH markets side by side. Same matched
# bidders; a bidder with no DAM offers simply has no rows here.
con.execute(f"""
copy (select r.res, r.h, r.cap from rh_dam r join mres_df using (res) order by r.res, r.h)
to '{OUT}/bidder_hourly_cap_dam.parquet' (format parquet)
""")
n_dam_cap = con.execute(
    "select count(distinct res) from rh_dam r join mres_df using (res)"
).fetchone()[0]
log(
    f"  hourly cap series written for {len(matched_res):,} matched bidders "
    f"(RTM); {n_dam_cap:,} of them also have DAM offers"
)


def highconf(mdf):
    if not len(mdf):
        return 0
    return int((mdf[mdf["rank"] == 1]["confidence"] >= 0.6).sum())


hc_forced, hc_comb, hc_mag = (
    highconf(mdf_forced),
    highconf(mdf_combined),
    highconf(mdf_magnitude),
)
log(f"  fingerprint-able bidders: forced={fp_forced:,}, combined={fp_comb:,}, magnitude={fp_mag:,}")
log(
    f"  high-confidence (>=0.60) links: forced={hc_forced:,}, combined={hc_comb:,} (+{hc_comb - hc_forced}), "
    f"magnitude={hc_mag:,} (+{hc_mag - hc_forced} vs forced)"
)


def dam_corr_count(mdf):
    """High-confidence rank-1 matches also corroborated by the day-ahead cross-check."""
    if not len(mdf):
        return 0
    r1 = mdf[(mdf["rank"] == 1) & (mdf["confidence"] >= 0.6)]
    return int(r1["dam_corroborates"].fillna(False).sum())


dam_corr_forced, dam_corr_comb, dam_corr_mag = (
    dam_corr_count(mdf_forced),
    dam_corr_count(mdf_combined),
    dam_corr_count(mdf_magnitude),
)
log(
    f"  of which corroborated by day-ahead cross-check: forced={dam_corr_forced:,}, "
    f"combined={dam_corr_comb:,}, magnitude={dam_corr_mag:,}"
)

# daily outage overlay for the drill-down. Tag each candidate day forced vs
# planned (forced precedence); cover candidates from ALL result sets.
cand_ids = set()
for mdf in (mdf_forced, mdf_combined, mdf_magnitude, mdf_lookup):
    if len(mdf):
        cand_ids |= set(mdf["cand_rid"].unique())
rod = []
for rid in cand_ids:
    pmax = res_meta.get(rid, {}).get("pmax")
    mwd = res_curt_mw.get(rid, {})
    fdays = res_days_forced.get(rid, set())
    pdays = res_days_planned.get(rid, set())
    for d in sorted(fdays):
        rod.append((rid, d, "forced", mwd.get(d), pmax))
    for d in sorted(pdays - fdays):
        rod.append((rid, d, "planned", mwd.get(d), pmax))
pd.DataFrame(rod, columns=["rid", "day", "kind", "curt_mw", "pmax"]).to_parquet(
    f"{OUT}/resource_outage_daily.parquet", index=False
)

# =====================================================================
# Stage 5: per-bidder LOOKUP artifacts (the "Look up a bidder" panel)
#   Everything above is built for population-level screens: they keep only the top
#   scorers, or only bidders that cleared an admission gate. A lookup panel has the
#   opposite requirement — it must answer for ANY id a user pastes, including ids the
#   screens deliberately drop (interties, loads, bidders with no timing signal). These
#   tables are all small (thousands of rows), so they are cheap to carry.
# =====================================================================
log("Stage 5: per-bidder lookup tables...")

# 5a. Directory of EVERY bidder id in either file, whatever its resource type, so a
# pasted id always resolves — and the panel can say "this is an intertie, not screened"
# instead of showing an empty page.
_dir_parts = []
for market, path in MARKETS.items():
    _dir_parts.append(f"""
    select '{market}' as market,
           RESOURCEBID_SEQ                            as res,
           min(SCHEDULINGCOORDINATOR_SEQ)             as sc,
           min(RESOURCE_TYPE)                         as resource_type,
           count(distinct RESOURCE_TYPE)              as n_resource_types,
           count(distinct MARKETPRODUCTTYPE)          as n_products,
           string_agg(distinct MARKETPRODUCTTYPE, ',' order by MARKETPRODUCTTYPE) as products,
           count(*)                                   as n_rows,
           count(distinct substr(STARTTIME,1,10))      as n_days,
           min(substr(STARTTIME,1,10))                as first_day,
           max(substr(STARTTIME,1,10))                as last_day,
           coalesce(sum(case when MARKETPRODUCTTYPE='EN' and SCH_BID_XAXISDATA > 0
                             then 1 else 0 end), 0)   as n_priced_en_rows,
           coalesce(sum(case when coalesce(SELFSCHEDMW,0) > 0 then 1 else 0 end), 0)
                                                      as n_selfsched_rows,
           max(case when MARKETPRODUCTTYPE='EN' then SCH_BID_XAXISDATA end) as en_cap_max,
           median(case when MARKETPRODUCTTYPE='EN' and SCH_BID_XAXISDATA > 0
                       then SCH_BID_Y1AXISDATA end)   as med_en_price,
           max(coalesce(SELFSCHEDMW,0))               as selfsched_mw_max
    from read_parquet('{path}')
    group by RESOURCEBID_SEQ
    """)
con.execute(f"""
copy ({" union all ".join(_dir_parts)} order by res, market)
to '{OUT}/bidder_directory.parquet' (format parquet)
""")

# 5b. Named-plant catalog (name / PMAX / NQC / storage tag / outage-day counts). res_meta
# already holds this in memory for the fingerprint screens but was never published, so the
# lookup panel had no way to describe a candidate plant.
_cat = []
for rid, meta_ in res_meta.items():
    _cat.append(
        dict(
            rid=rid,
            rname=meta_["rname"],
            pmax=meta_["pmax"],
            nqc=meta_["nqc"],
            is_storage=bool(meta_["storage"]),
            forced_days=len(res_days_forced.get(rid, ())),
            planned_days=len(res_days_planned.get(rid, ())),
            outage_days=len(res_days_combined.get(rid, ())),
        )
    )
pd.DataFrame(_cat).sort_values("rname").to_parquet(f"{OUT}/plant_catalog.parquet", index=False)

# 5c. Top-5 candidate plants for EVERY bidder, ungated. Scored on the forced+planned
# calendar with the magnitude signal available, so each row carries timing (phi), size,
# type and curtailment-tracking (rho) evidence plus an `admitted` flag saying whether the
# published screens would have accepted it. A bidder whose only evidence is a capacity
# coincidence still gets rows — clearly marked as such, which is the honest answer.
if len(mdf_lookup):
    mdf_lookup = mdf_lookup.sort_values(["res", "rank"]).reset_index(drop=True)
mdf_lookup.to_parquet(f"{OUT}/bidder_candidates.parquet", index=False)
log(
    f"  lookup candidates: {len(mdf_lookup):,} rows for {mdf_lookup['res'].nunique():,} bidders "
    f"({int(mdf_lookup['admitted'].sum()):,} would clear the published gate)"
    if len(mdf_lookup)
    else "  lookup candidates: none"
)

# 5d. Day-ahead daily capacity for every bidder (the RTM twin already exists as
# bidder_daily_cap), so the profile can chart both markets for any id — not just the
# few hundred matched bidders covered by bidder_hourly_cap*.
con.execute(f"""
copy (
  select res, day, max(cap) as cap from rh_dam group by res, day order by res, day
) to '{OUT}/bidder_daily_cap_dam.parquet' (format parquet)
""")

# 5e. Monthly activity per bidder per market, for the profile's shape-over-time view.
con.execute(f"""
copy (
  select 'RTM' as market, res, date_trunc('month', h) as month, count(*) as hours,
         max(cap) as cap_max, avg(cap) as cap_avg, median(max_price) as med_price
  from rh group by res, date_trunc('month', h)
  union all
  select 'DAM' as market, res, date_trunc('month', h) as month, count(*) as hours,
         max(cap) as cap_max, avg(cap) as cap_avg, median(max_price) as med_price
  from rh_dam group by res, date_trunc('month', h)
  order by res, market, month
) to '{OUT}/bidder_monthly.parquet' (format parquet)
""")


# 5f. ANONYMITY CONTEXT from the CEC power-plant list (optional raw input).
#   How many REAL California plants could a bidder be, judging only by things visible in
#   the bid data — its size, and the technology implied by how it bids? That count is the
#   honest denominator for Screen 2's whole question: a bidder matching 25 real plants on
#   size is not identifiable from size, however confident a single candidate looks.
#
#   Deliberately NO name matching. The CEC list carries no CAISO resource id, so any
#   bidder->plant link would be a fuzzy string join: only ~50% of outage-file plant names
#   match at all, and a quarter of those disagree with PMAX by >50% (the outage file is
#   per-resource, CEC is per-plant). A wrong join would attach a real operator's name to
#   the wrong plant. We publish COUNTS ONLY, so no individual CEC row is ever asserted to
#   be a given bidder — and because the counts are precomputed here, the CSV stays a raw
#   input that the deployed app never needs.
PP_CSV = os.path.join(DATA, "powerplants.csv")
if os.path.exists(PP_CSV):
    log("Stage 5f: anonymity context from the CEC plant list...")
    _pp = pd.read_csv(PP_CSV)
    _pp = _pp[
        (_pp["Retired Plant"] == 0) & _pp["Capacity_Latest"].notna() & (_pp["Capacity_Latest"] > 0)
    ]
    # Technology implied by the bid data alone -- never by a plant name.
    #   storage : the bid carries state-of-charge limits (only storage does)
    #   solar   : offers essentially never appear overnight and cluster in daylight
    _shape = con.execute("""
      select res,
             sum(case when hour(h) between 8 and 17 then 1 else 0 end) as day_h,
             sum(case when hour(h) between 0 and 5  then 1 else 0 end) as night_h,
             count(*) as tot_h
      from rh group by res
    """).fetchdf()
    _b = bidder[["res", "cap_ref", "is_storage"]].merge(_shape, on="res", how="left")
    _b["solar_like"] = (_b["night_h"] / _b["tot_h"] < 0.02) & (_b["day_h"] / _b["tot_h"] > 0.55)
    _b["tech"] = np.where(
        _b["is_storage"].astype(bool), "storage", np.where(_b["solar_like"], "solar", "other")
    )
    _FUEL = {"storage": {"BAT"}, "solar": {"SUN", "HBD"}}
    _caps = {
        "any": np.sort(_pp["Capacity_Latest"].to_numpy(dtype=float)),
        "storage": np.sort(
            _pp.loc[_pp["PriEnergySource"].isin(_FUEL["storage"]), "Capacity_Latest"].to_numpy(
                dtype=float
            )
        ),
        "solar": np.sort(
            _pp.loc[_pp["PriEnergySource"].isin(_FUEL["solar"]), "Capacity_Latest"].to_numpy(
                dtype=float
            )
        ),
        "other": np.sort(
            _pp.loc[
                ~_pp["PriEnergySource"].isin(_FUEL["storage"] | _FUEL["solar"]), "Capacity_Latest"
            ].to_numpy(dtype=float)
        ),
    }

    def _n_within(arr, cap):
        # same inverted window as the candidate gate: |cap-plant|/plant <= CAP_TOL
        lo, hi = cap / (1 + CAP_TOL), cap / (1 - CAP_TOL)
        return int(np.searchsorted(arr, hi, "right") - np.searchsorted(arr, lo, "left"))

    # Coverage floor. The CEC list is a PLANT inventory: only 67 of its entries are under
    # 1 MW (its 5th percentile), so for a sub-MW bidder a count of 0 or 1 says the list does
    # not cover that size range — NOT that the bidder is distinctive. Without this the panel
    # would report 154 sub-MW bidders as "nearly identified by size alone", which is false.
    _cec_floor = float(np.quantile(_caps["any"], 0.05))
    _rows = []
    for r in _b.itertuples(index=False):
        cap = float(r.cap_ref or 0)
        if cap <= 0:
            continue
        _rows.append(
            dict(
                res=r.res,
                tech=r.tech,
                cap_ref=round(cap, 2),
                n_plants_size=_n_within(_caps["any"], cap),
                n_plants_size_tech=_n_within(_caps[r.tech], cap),
                n_plants_tech_total=int(len(_caps[r.tech])),
                below_cec_floor=bool(cap < _cec_floor),
            )
        )
    _anon = pd.DataFrame(_rows)
    _anon.to_parquet(f"{OUT}/bidder_anonymity.parquet", index=False)
    # Headline stats are quoted for bidders the CEC list actually covers; the sub-MW tail
    # would otherwise drag the median down and manufacture false "unique" cases.
    _cov = _anon[~_anon["below_cec_floor"]] if len(_anon) else _anon
    anon_stats = dict(
        cec_plants=int(len(_pp)),
        cec_floor_mw=round(_cec_floor, 2),
        bidders=int(len(_anon)),
        bidders_covered=int(len(_cov)),
        bidders_below_floor=int(len(_anon) - len(_cov)),
        median_size_matches=int(_cov["n_plants_size"].median()) if len(_cov) else 0,
        median_size_tech_matches=int(_cov["n_plants_size_tech"].median()) if len(_cov) else 0,
        unique_on_size_tech=int((_cov["n_plants_size_tech"] == 1).sum()) if len(_cov) else 0,
        thin_on_size_tech=int((_cov["n_plants_size_tech"].between(1, 5)).sum()) if len(_cov) else 0,
    )
    log(
        f"  CEC plants: {anon_stats['cec_plants']:,}; of the {anon_stats['bidders_covered']:,} "
        f"bidders at or above {anon_stats['cec_floor_mw']:g} MW, a typical one's size+technology "
        f"matches {anon_stats['median_size_tech_matches']} of them and only "
        f"{anon_stats['unique_on_size_tech']} are unique on that alone "
        f"({anon_stats['bidders_below_floor']:,} smaller bidders are below the list's coverage)"
    )
else:
    anon_stats = None
    log(f"  NOTE: {PP_CSV} not found — skipping anonymity context (Screen 3 hides that panel)")

# =====================================================================
# Dataset summary statistics (for the "Data behind this tool" panel)
# =====================================================================
log("Summarizing raw datasets...")


def _bid_dataset(key, name, path, what):
    # Each count is named for exactly the population it covers. These used to blur together
    # ("...that submit energy offers" equalled the file total, and the per-type breakdown
    # summed to MORE than the bidder count because ~9 IDs bid under several resource types),
    # which made the panel disagree with the dashboard's own headline bidder count.
    n_rows, n_bidders, n_en, n_gen_pos, dmin, dmax, n_days, med_en = con.execute(f"""
      select count(*), count(distinct RESOURCEBID_SEQ),
             count(distinct case when MARKETPRODUCTTYPE='EN' then RESOURCEBID_SEQ end),
             count(distinct case when MARKETPRODUCTTYPE='EN' and RESOURCE_TYPE='GENERATOR'
                                  and SCH_BID_XAXISDATA > 0 then RESOURCEBID_SEQ end),
             min(substr(STARTTIME,1,10)), max(substr(STARTTIME,1,10)),
             count(distinct substr(STARTTIME,1,10)),
             median(case when MARKETPRODUCTTYPE='EN' and SCH_BID_XAXISDATA > 0
                         then SCH_BID_Y1AXISDATA end)
      from read_parquet('{path}')
    """).fetchone()
    rtypes = con.execute(f"""
      select RESOURCE_TYPE, count(distinct RESOURCEBID_SEQ)
      from read_parquet('{path}') where MARKETPRODUCTTYPE='EN' group by 1 order by 2 desc
    """).fetchall()
    rtype_str = ", ".join(f"{rt.lower()} {n:,}" for rt, n in rtypes)
    if sum(n for _, n in rtypes) > n_en:
        rtype_str += f" (sums above {n_en:,}: some IDs bid under more than one type)"
    return dict(
        key=key,
        name=name,
        file=os.path.basename(path),
        what=what,
        stats=[
            ["Rows (individual price-step offers)", f"{n_rows:,}"],
            ["Distinct anonymous bidders in the file", f"{n_bidders:,}"],
            ["…with at least one energy offer", f"{n_en:,}"],
            [
                "…generators with a positive-MW energy offer (the population screened)",
                f"{n_gen_pos:,}",
            ],
            ["Resource types (any energy offer)", rtype_str],
            ["Typical energy offer price (median)", f"${med_en:,.0f}/MWh"],
            ["Date coverage", f"{dmin} → {dmax}"],
            ["Days covered", f"{n_days} of 365 (2025-03-09 absent from the source file)"],
        ],
    )


datasets = [
    _bid_dataset(
        "rtm_bids",
        "Real-time market bids (RTM)",
        BIDS,
        "Offers plants submit to sell power in the market that balances supply and demand "
        "minute-to-minute. Public, but with plant names stripped to anonymous ID numbers. "
        "This is the primary dataset both screens run on.",
    ),
    _bid_dataset(
        "dam_bids",
        "Day-ahead market bids (DAM)",
        DAMB,
        "The same kind of offers, but for the market run the day before delivery — the same "
        "market the price data below comes from. Used for the day-ahead view of Screen 1 and "
        "the day-ahead cross-check in Screen 2.",
    ),
]

# outages
o_rows, o_res, o_forced, o_planned, o_dmin, o_dmax, o_medmw = con.execute(f"""
  select count(*), count(distinct "RESOURCE ID"),
         sum(case when "OUTAGE TYPE"='FORCED' then 1 else 0 end),
         sum(case when "OUTAGE TYPE"='PLANNED' then 1 else 0 end),
         min(cast("CURTAILMENT START DATE TIME" as date)),
         max(cast("CURTAILMENT START DATE TIME" as date)),
         median("CURTAILMENT MW")
  from read_parquet('{OUTG}')
""").fetchone()
datasets.append(
    dict(
        key="outages",
        name="Generator outages",
        file=os.path.basename(OUTG),
        what="A log of when power plants were unavailable — either an unexpected breakdown "
        "(FORCED) or scheduled maintenance (PLANNED) — and how many megawatts each lost. Screen 1 "
        "uses forced outages as one way to gauge when the grid was short; Screen 2 uses the outage "
        "calendar to fingerprint anonymous bidders.",
        stats=[
            ["Outage records", f"{o_rows:,}"],
            ["Distinct named plants", f"{o_res:,}"],
            [
                "Forced vs planned records",
                f"{o_forced:,} forced / {o_planned:,} planned",
            ],
            ["Typical curtailment size (median)", f"{o_medmw:,.0f} MW"],
            ["Date coverage", f"{o_dmin} → {o_dmax}"],
        ],
    )
)

# day-ahead LMP prices (full scan of the large file — one pass)
p_rows, p_lmp_rows, p_nodes, p_dmin, p_dmax, p_days = con.execute(f"""
  select count(*), count(*) filter (where LMP_TYPE='LMP'), count(distinct NODE_ID),
         min(OPR_DT), max(OPR_DT), count(distinct OPR_DT)
  from read_parquet('{LMP}')
""").fetchone()
datasets.append(
    dict(
        key="lmp",
        name="Day-ahead prices (LMP)",
        file=os.path.basename(LMP),
        what="The actual price of electricity, set separately at every point on the grid (a "
        "'Locational Marginal Price'). We track the three regional trading hubs plus their average. "
        "This is the real market-price data — powering the Prices screen and the price-based / "
        "clearing-price parts of Screen 1.",
        stats=[
            # p_rows counts LMP *and* its three components (energy/congestion/loss) as
            # separate rows, so it is ~4x the number of actual prices. Publishing it alone
            # next to the node count implied an impossible density (nodes x hours is the
            # ceiling), so both numbers are named explicitly.
            ["Rows (LMP plus its 3 components)", f"{p_rows:,}"],
            ["Node-hour prices", f"{p_lmp_rows:,}"],
            ["Distinct pricing locations (nodes)", f"{p_nodes:,}"],
            ["Trading hubs used here", ", ".join(HUBS.keys()) + " (+ system average)"],
            ["Typical system price (median)", f"${price_stats[1]:,.0f}/MWh"],
            [
                "Average / peak system price",
                f"${price_stats[0]:,.0f} / ${price_stats[2]:,.0f}/MWh",
            ],
            ["Negative-price hours", f"{price_stats[3]:,}"],
            ["Date coverage", f"{p_dmin} → {p_dmax}"],
            ["Days covered", f"{p_days} of 365 (2025-03-09 absent from the source file)"],
        ],
    )
)

# real-time (RTM) LMP — the 5-minute hub series extracted from the ~50GB raw files
rtm_5min_rows, rtm_dmin, rtm_dmax, rtm_days = con.execute("""
  select count(*), min(ts), max(ts), count(distinct ts::date) from rtm_5min
""").fetchone()
# True SYSTEM 5-minute peak: the highest cross-hub mean in any single 5-minute interval.
# Distinct from rtm_stats[2], which is the highest hourly AVERAGE, and from max(peak5),
# which is only "the highest print at any one hub".
rtm_peak5 = con.execute("select round(max(lmp),2) from rtm_sys5").fetchone()[0]
datasets.append(
    dict(
        key="rtm_lmp",
        name="Real-time prices (RTM LMP)",
        file="2025-RTM-LMP/ (monthly + December per-hour parquets)",
        what="The price the grid actually settled at in real time, every 5 minutes — the "
        "counterpart to the day-ahead LMP. The raw files span every node (~50GB), so we keep only "
        "the three trading hubs. Powers the real-time line and DAM→RTM spread on the Prices screen, "
        "the intraday-volatility view, and the real-time price basis in Screen 1.",
        stats=[
            ["5-minute hub observations kept", f"{rtm_5min_rows:,}"],
            ["Trading hubs used here", ", ".join(HUBS.keys()) + " (+ system average)"],
            ["Typical system price (median)", f"${rtm_stats[1]:,.0f}/MWh"],
            ["Average system price", f"${rtm_stats[0]:,.0f}/MWh"],
            ["Peak hourly average", f"${rtm_stats[2]:,.0f}/MWh"],
            ["Peak 5-minute system price", f"${rtm_peak5:,.0f}/MWh"],
            [
                "DAM→RTM spread (mean ± sd)",
                f"${spread_stats[0]:,.1f} ± ${spread_stats[1]:,.1f}/MWh",
            ],
            ["Negative-price hours", f"{rtm_stats[3]:,}"],
            ["Date coverage", f"{rtm_dmin} → {rtm_dmax}"],
            ["Days covered", f"{rtm_days} of 365"],
        ],
    )
)

# CEC plant list — documented because it IS an outside dataset, even though only
# aggregate counts derived from it are published (see Stage 5f).
if anon_stats:
    datasets.append(
        dict(
            key="cec_plants",
            name="California power plants (CEC)",
            file="powerplants.csv",
            what="The California Energy Commission's public list of power plants: name, "
            "operator, county, fuel, nameplate capacity and coordinates. It carries NO CAISO "
            "resource ID, so it is never joined to a bidder by name. It is used for one thing "
            "only: counting how many real plants share a bidder's size and technology, which "
            "is the denominator for how identifiable that bidder actually is (Screen 3). No "
            "plant name, operator or location from this file is attributed to any bidder.",
            stats=[
                ["Plants (in service, with a capacity)", f"{anon_stats['cec_plants']:,}"],
                [
                    "Plants sharing a typical bidder's size",
                    f"{anon_stats['median_size_matches']} (median)",
                ],
                [
                    "...also sharing its technology",
                    f"{anon_stats['median_size_tech_matches']} (median)",
                ],
                [
                    "Bidders unique on size + technology alone",
                    f"{anon_stats['unique_on_size_tech']:,} of {anon_stats['bidders_covered']:,}",
                ],
                [
                    "Bidders too small for the list to cover",
                    f"{anon_stats['bidders_below_floor']:,} (under "
                    f"{anon_stats['cec_floor_mw']:g} MW)",
                ],
                ["Used for", "Anonymity context only — no names joined"],
            ],
        )
    )

# =====================================================================
# meta.json
# =====================================================================
overview = con.execute("""
select count(distinct res) n_res, min(day) tmin, max(day) tmax from rh
""").fetchone()
# Actual day coverage. 2025-03-09 (the spring DST switch) is absent from BOTH bid files and
# the DAM LMP file, so "full year" was never true; warn loudly if that ever changes shape.
n_bid_days = con.execute("select count(distinct day) from rh").fetchone()[0]
_span_days = (pd.Timestamp(overview[2]) - pd.Timestamp(overview[1])).days + 1
if n_bid_days != _span_days:
    log(
        f"  NOTE: {_span_days - n_bid_days} calendar day(s) in "
        f"{overview[1]}..{overview[2]} have no bids at all ({n_bid_days} of {_span_days} present)"
    )
meta = dict(
    generated_scope=(
        f"CAISO RTM 2025 ({n_bid_days} of 365 days; 2025-03-09 absent from both bid files "
        "and the day-ahead price file. The 5-minute real-time price file does cover it.)"
    ),
    n_bid_rows=int(con.execute(f"select count(*) from read_parquet('{BIDS}')").fetchone()[0]),
    # The rows the screens actually consume: positive-MW priced ENERGY steps from
    # generators. n_bid_rows above is every price step of every product in the file, so it
    # must not be labelled "offers analyzed" anywhere in the UI.
    n_bid_rows_analyzed=int(
        con.execute(f"""select count(*) from read_parquet('{BIDS}')
           where MARKETPRODUCTTYPE='EN' and RESOURCE_TYPE='GENERATOR'
             and SCH_BID_XAXISDATA is not null and SCH_BID_XAXISDATA > 0""").fetchone()[0]
    ),
    n_bid_days=int(n_bid_days),
    anonymity=anon_stats,
    n_bid_hours=int(con.execute("select count(distinct h) from rh").fetchone()[0]),
    n_resources=int(overview[0]),
    date_min=str(overview[1]),
    date_max=str(overview[2]),
    datasets=datasets,
    thresholds=dict(
        elevated_price=THR_ELEV,
        nearcap_price=THR_NEAR,
        tight_percentile=TIGHT_PCTL,
        tight_mw=round(float(tight_thr), 1),
        price_tight_percentile=PRICE_TIGHT_PCTL,
        price_tight_lmp=round(float(price_thr), 1),
        price_tight_lmp_rtm=round(float(price_thr_rtm), 1),
        cap_tolerance_pct=CAP_TOL * 100,
        min_tight_hours=MIN_ELEV_HOURS,
    ),
    withholding_scored=int(n_wh),
    # RTM keys kept for backward compatibility; full grid in withholding_scored_by.
    withholding_scored_outage=int(n_wh_by[("RTM", "outage")]),
    withholding_scored_price=int(n_wh_by[("RTM", "price")]),
    withholding_scored_by={f"{m}_{b}": int(n) for (m, b), n in n_wh_by.items()},
    markets=list(MARKETS.keys()),
    price=dict(
        hubs=list(HUBS.keys()),
        avg_sys_lmp=float(price_stats[0]),
        median_sys_lmp=float(price_stats[1]),
        peak_sys_lmp=float(price_stats[2]),
        neg_price_hours=int(price_stats[3]),
        price_hours=int(price_stats[4]),
        # real-time (RTM) counterpart at the same three hubs
        rtm_avg_sys_lmp=float(rtm_stats[0]),
        rtm_median_sys_lmp=float(rtm_stats[1]),
        rtm_peak_sys_lmp=float(rtm_stats[2]),
        rtm_neg_price_hours=int(rtm_stats[3]),
        rtm_price_hours=int(rtm_stats[4]),
        # day-ahead -> real-time spread (RTM minus DAM), system level, shared hours
        spread_mean=float(spread_stats[0]),
        spread_sd=float(spread_stats[1]),
        spread_max=float(spread_stats[2]),
        spread_min=float(spread_stats[3]),
        spread_hours=int(spread_stats[4]),
    ),
    demand=dict(
        dam_avg_mw=float(demand_stats[0]),
        dam_peak_mw=float(demand_stats[1]),
        dam_musttake_share_pct=float(demand_stats[2]),
    ),
    reident_candidate_bidders=int(mdf_forced["res"].nunique()) if len(mdf_forced) else 0,
    reident_candidate_bidders_combined=int(mdf_combined["res"].nunique())
    if len(mdf_combined)
    else 0,
    reident_candidate_bidders_magnitude=int(mdf_magnitude["res"].nunique())
    if len(mdf_magnitude)
    else 0,
    reident_highconf_links=hc_forced,
    reident_highconf_links_combined=hc_comb,
    reident_highconf_links_magnitude=hc_mag,
    reident_dam_corroborated=dam_corr_forced,
    reident_dam_corroborated_combined=dam_corr_comb,
    reident_dam_corroborated_magnitude=dam_corr_mag,
    assumptions=[
        "Screen 1 runs on two bid markets you can toggle: real-time (RTM, the default) and day-ahead (DAM). Because the price data is day-ahead, the DAM market lets Screen 1 compare offers against the ACTUAL day-ahead clearing price — a real impact test — while RTM keeps the original design.",
        "Screen 1 can define 'how short the grid was' three ways: by outages (how much plant capacity was offline — the original stand-in), by day-ahead prices (hours when the DAM market price spiked), or by real-time prices (hours whose AVERAGE 5-minute real-time price was in the top 10%). Because that is an hourly average, a single 5-minute spike inside an otherwise cheap hour does not by itself mark the hour scarce. Both price bases use the top 10% of hours at the three CAISO trading hubs; the outage basis needs no price data at all.",
        "The real clearing-price impact test (DAM market) measures the capacity a GENERATOR offered ABOVE the price that actually cleared that hour — capacity it effectively withheld from the day-ahead solution — comparing scarce vs. normal hours. It covers generators only (day-ahead demand and intertie bids are excluded, since a load bidding above the price is willingness-to-pay, not withheld supply) and is measured at the system/hub price level because bids carry no node identifier.",
        "There's no fuel-cost data, so 'holding back power' is still judged by comparing each plant to its own behavior in short vs. normal hours — the clearing price sharpens the 'high offer' threshold but does not prove intent.",
        "Outages come from CAISO's daily 'prior trade date' reports. A row with no end time means the outage was still ongoing as of that report's trade date, so we treat it as active through that date — not a one-hour blip. Outages that began before 2025 but were still active are clipped into the 2025 window, and the same ongoing outage re-listed across many daily reports is collapsed so it is counted once. CAISO also files one physical curtailment as many overlapping records — split across sub-intervals and re-issued under different outage IDs, each carrying the same megawatts — so for each hour we count only a plant's DEEPEST curtailment and then add across plants. Adding the records up instead would invent capacity that was never offline, most of all in November and December 2025.",
        "Each offer is a set of (amount, price) steps with prices that only go up; a plant's 'capacity' is the largest amount it offered.",
        "Unmasking (Screen 2) matches a bidder's quiet days against a plant's outage days (three methods: forced, forced+planned, magnitude-aware). Each match is cross-checked against the DAY-AHEAD offers: if the bidder also goes quiet in DAM on the plant's outage days, that is a second, market-independent line of evidence.",
    ],
)
with open(f"{OUT}/meta.json", "w") as f:
    json.dump(meta, f, indent=2)

log("DONE. Derived files in " + OUT)
for fn in sorted(os.listdir(OUT)):
    sz = os.path.getsize(os.path.join(OUT, fn)) / 1e6
    log(f"  {fn:34s} {sz:8.2f} MB")
