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
import os, json, textwrap
import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "caiso-data"))
OUT  = os.path.join(HERE, "data", "derived")
SCRATCH = os.environ.get("CAISO_SCRATCH", os.path.join(HERE, ".cache"))
os.makedirs(OUT, exist_ok=True)
os.makedirs(SCRATCH, exist_ok=True)

BIDS = os.path.join(DATA, "2025-RTM-BIDS.parquet")
OUTG = os.path.join(DATA, "2025-OUTAGES.parquet")

# ---- tunable thresholds (documented in the dashboard "Method" panel) ----
THR_ELEV   = 250.0   # $/MWh: capacity offered at/above this is "elevated" (well above ~$32 median)
THR_NEAR   = 900.0   # $/MWh: at/above this is "near-cap" ($1000 bid cap) -> classic withholding zone
TIGHT_PCTL = 0.90    # hours with forced-outage MW above this percentile are "system-tight"
CAP_TOL    = 0.12    # re-ident: |bidder_cap - resource_PMAX| / PMAX must be <= this
MIN_ELEV_HOURS = 200 # withholding: min tight-hour presence to be scored
TOP_DRILL  = 60      # how many top resources get a stored daily drill-down series

def log(m): print(f"[pipeline] {m}", flush=True)

con = duckdb.connect()
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
  any_value(SCHEDULINGCOORDINATOR_SEQ)     as sc,
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
group by RESOURCEBID_SEQ, cast(STARTTIME as timestamp), cast(substr(STARTTIME,1,10) as date)
""")
n_rh = con.execute("select count(*) from rh").fetchone()[0]
log(f"  resource-hours: {n_rh:,}")

# =====================================================================
# Stage 1: system tightness — forced-outage MW active per hour
# =====================================================================
log("Stage 1: computing hourly system tightness from forced outages...")
con.execute(f"""
create or replace table outg as
select
  "RESOURCE ID"   as rid,
  "RESOURCE NAME" as rname,
  date_trunc('hour', "CURTAILMENT START DATE TIME")                                as ostart,
  coalesce("CURTAILMENT END DATE TIME", "CURTAILMENT START DATE TIME" + interval 1 hour) as oend,
  "CURTAILMENT MW"   as cmw,
  "RESOURCE PMAX MW" as pmax,
  "NET QUALIFYING CAPACITY MW" as nqc
from read_parquet('{OUTG}')
where "OUTAGE TYPE"='FORCED'
  and "CURTAILMENT START DATE TIME" >= timestamp '2025-01-01'
  and "CURTAILMENT START DATE TIME" <  timestamp '2026-01-01'
""")

con.execute("create or replace table hours as select distinct h from rh order by h")
con.execute("""
create or replace table tight as
select hh.h as h,
       coalesce(sum(o.cmw),0) as tight_mw,
       count(o.rid)          as n_out
from hours hh
left join outg o on (hh.h >= o.ostart and hh.h < o.oend)
group by hh.h
""")
tight_thr = con.execute(f"select quantile_cont(tight_mw,{TIGHT_PCTL}) from tight").fetchone()[0]
log(f"  system-tight threshold (P{int(TIGHT_PCTL*100)} of hourly forced-outage MW): {tight_thr:,.0f} MW")

# =====================================================================
# Stage 2: market overview (hourly + daily + monthly product mix)
# =====================================================================
log("Stage 2: market overview tables...")
con.execute(f"""
create or replace table market_hourly as
select r.h as h, cast(r.h as date) as day,
       count(distinct r.res) as n_res,
       sum(r.cap)            as total_cap_mw,
       sum(r.elev_mw)        as elev_mw,
       sum(r.near_mw)        as near_mw,
       median(r.max_price)   as med_maxprice,
       t.tight_mw            as tight_mw,
       (t.tight_mw >= {tight_thr}) as is_tight
from rh r join tight t using (h)
group by r.h, t.tight_mw
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
         sum(case when is_tight then 1 else 0 end) as tight_hours
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
# Stage 3: ECONOMIC-WITHHOLDING screen
#   withholding_index = (elevated share of offered MW in tight hours)
#                     - (elevated share in normal hours)
#   Positive & large => shifts capacity to high prices exactly when system is short.
# =====================================================================
log("Stage 3: economic-withholding screen...")
con.execute(f"""
create or replace table rh2 as
select r.*, t.is_tight
from rh r
join (select h, (tight_mw >= {tight_thr}) as is_tight from tight) t using (h)
""")
con.execute(f"""
create or replace table wh as
with agg as (
  select res, any_value(sc) as sc, any_value(is_storage) as is_storage,
    count(*) as en_hours,
    sum(case when is_tight then 1 else 0 end) as tight_hours,
    sum(cap)     as cap_all,
    sum(elev_mw) as elev_all,
    sum(near_mw) as near_all,
    sum(case when is_tight then cap     else 0 end) as cap_t,
    sum(case when is_tight then elev_mw else 0 end) as elev_t,
    sum(case when is_tight then near_mw else 0 end) as near_t,
    sum(case when not is_tight then cap     else 0 end) as cap_n,
    sum(case when not is_tight then elev_mw else 0 end) as elev_n,
    median(max_price) as med_price,
    max(cap) as cap_max
  from rh2 group by res
)
select *,
  (elev_t/nullif(cap_t,0)) as hi_share_tight,
  (elev_n/nullif(cap_n,0)) as hi_share_normal,
  (elev_t/nullif(cap_t,0)) - (elev_n/nullif(cap_n,0)) as withholding_index,
  near_t as nearcap_mwh_tight
from agg
where tight_hours >= {MIN_ELEV_HOURS} and cap_t > 0
""")
con.execute(f"""
copy (
  select res, sc, is_storage, cap_max, en_hours, tight_hours, med_price,
         hi_share_tight, hi_share_normal, withholding_index, nearcap_mwh_tight,
         row_number() over (order by withholding_index desc, nearcap_mwh_tight desc) as rank
  from wh order by withholding_index desc
) to '{OUT}/withholding_resource.parquet' (format parquet)
""")
n_wh = con.execute("select count(*) from wh").fetchone()[0]
log(f"  scored {n_wh:,} resources")

# daily drill-down for the worst offenders
con.execute(f"""
create or replace table top_wh as
select res from wh order by withholding_index desc, nearcap_mwh_tight desc limit {TOP_DRILL}
""")
con.execute(f"""
copy (
  select r.res, r.day,
         sum(r.cap) as cap, sum(r.elev_mw) as elev_mw, sum(r.near_mw) as near_mw,
         sum(r.elev_mw)/nullif(sum(r.cap),0) as hi_share,
         max(case when t.tight_mw >= {tight_thr} then 1 else 0 end) as had_tight_hour
  from rh r join top_wh using (res)
  join tight t using (h)
  group by r.res, r.day order by r.res, r.day
) to '{OUT}/withholding_daily.parquet' (format parquet)
""")

# =====================================================================
# Stage 4: RE-IDENTIFICATION / fingerprinting
# =====================================================================
log("Stage 4: re-identification / fingerprinting...")

# 4a. bidder profiles (capacity fingerprint + storage tag)
bidder = con.execute("""
select res,
       any_value(sc) as sc,
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

# resource outage day-sets + capacity
resday = con.execute("""
select rid, any_value(rname) as rname,
       max(pmax) as pmax, max(nqc) as nqc,
       list(distinct cast(ostart as date)) as odays_start
from outg group by rid
""").fetchdf()
# expand each outage to the set of covered days
res_intervals = con.execute("""
select rid, any_value(rname) rname, max(pmax) pmax, max(nqc) nqc from outg group by rid
""").fetchdf()

log("  building day-sets and scoring candidate matches...")
import pandas as pd, numpy as np
from collections import defaultdict

# resource -> set of outage days (expand intervals to daily coverage)
outg_rows = con.execute("select rid, ostart, oend from outg").fetchdf()
res_days = defaultdict(set)
for rid, s, e in outg_rows.itertuples(index=False):
    d0 = pd.Timestamp(s).normalize(); d1 = pd.Timestamp(e).normalize()
    for d in pd.date_range(d0, d1, freq="D"):
        res_days[rid].add(d.date())

# bidder -> dip-days. A resource on forced outage typically STOPS bidding
# (absent day) or offers collapsed capacity relative to its TYPICAL day.
# Reference = median of present daily caps (robust to occasional highs), so
# dip-days = {absent days in span} U {present days with cap < 0.35*median}.
present_cap = defaultdict(dict)  # res -> {date: cap}  (coerce keys to datetime.date)
for res, day, cap in bday.itertuples(index=False):
    present_cap[res][pd.Timestamp(day).date()] = cap
bref = {res: float(np.median(list(caps.values()))) for res, caps in present_cap.items()}

bidder_dip_days = defaultdict(set)
for b in bidder.itertuples(index=False):
    ref = bref.get(b.res, 0)
    if not ref or ref <= 0:
        continue
    span = pd.date_range(pd.Timestamp(b.first_day), pd.Timestamp(b.last_day), freq="D")
    caps = present_cap.get(b.res, {})
    dips = set()
    for d in span:
        dd = d.date()
        c = caps.get(dd)
        if c is None or c < 0.35 * ref:   # absent, or genuine collapse vs typical day
            dips.add(dd)
    bidder_dip_days[b.res] = dips

# name-based storage heuristic for resources
def name_is_storage(n):
    if not isinstance(n, str): return False
    n = n.upper()
    return any(k in n for k in ("STORAGE","BATTERY","BESS","ENERGY STORAGE"," ES", "_ES"))
res_meta = {}
for rid, rname, pmax, nqc in res_intervals[["rid","rname","pmax","nqc"]].itertuples(index=False):
    res_meta[rid] = dict(rname=rname, pmax=pmax, nqc=nqc, storage=name_is_storage(rname))

# candidate matching: capacity tolerance filter, then day-set overlap (Jaccard + recall)
res_list = [(rid, m["pmax"]) for rid, m in res_meta.items()
            if m["pmax"] and not np.isnan(m["pmax"]) and len(res_days.get(rid, ()))>=4]
res_list.sort(key=lambda x: x[1])
res_pmax = np.array([p for _, p in res_list])
res_ids  = [r for r, _ in res_list]

span_len = {b.res: (pd.Timestamp(b.last_day)-pd.Timestamp(b.first_day)).days + 1
            for b in bidder.itertuples(index=False)}
span_first = {b.res: pd.Timestamp(b.first_day).date() for b in bidder.itertuples(index=False)}
span_last  = {b.res: pd.Timestamp(b.last_day).date()  for b in bidder.itertuples(index=False)}

def phi_coeff(dips, odays_span, N):
    # Matthews correlation between two binary day-vectors of length N.
    n11 = len(dips & odays_span)
    n10 = len(dips) - n11
    n01 = len(odays_span) - n11
    n00 = N - n11 - n10 - n01
    num = n11*n00 - n10*n01
    den = (n11+n10)*(n11+n01)*(n00+n10)*(n00+n01)
    return (num/(den**0.5)) if den > 0 else 0.0, n11

matches = []
_dbg = dict(considered=0, passed_guard=0, cap_cands=0, pairs=0, phi_ok=0, maxphi=0.0)
for b in bidder.itertuples(index=False):
    cap = b.cap_ref
    if not cap or cap <= 0: continue
    _dbg["considered"] += 1
    dips = bidder_dip_days.get(b.res, set())
    N = span_len[b.res]
    # need a specific, non-degenerate dip pattern to fingerprint on
    if len(dips) < 3 or N < 30 or len(dips) > 0.7 * N:
        continue
    _dbg["passed_guard"] += 1
    lo, hi = cap*(1-CAP_TOL), cap*(1+CAP_TOL)
    i0 = int(np.searchsorted(res_pmax, lo)); i1 = int(np.searchsorted(res_pmax, hi))
    if i1 > i0: _dbg["cap_cands"] += 1
    d0, d1 = span_first[b.res], span_last[b.res]
    cands = []
    for j in range(i0, i1):
        rid = res_ids[j]; pmax = res_pmax[j]
        odays = res_days.get(rid)
        if not odays: continue
        odays_span = {d for d in odays if d0 <= d <= d1}
        if not odays_span or len(odays_span) > 0.9 * N:   # resource down ~all year carries no timing signal
            continue
        _dbg["pairs"] += 1
        phi, inter = phi_coeff(dips, odays_span, N)
        _dbg["maxphi"] = max(_dbg["maxphi"], phi)
        if phi < 0.30 or inter < 3:                       # require distinctive timing coincidence
            continue
        _dbg["phi_ok"] += 1
        recall    = inter/len(dips)
        jacc      = inter/len(dips | odays_span)
        cap_close = 1 - abs(cap-pmax)/(pmax if pmax else 1)
        stor_match = (b.is_storage == res_meta[rid]["storage"])
        conf = 0.60*phi + 0.25*cap_close + (0.15 if stor_match else 0.0)
        cands.append((rid, pmax, cap_close, recall, jacc, phi, conf, inter))
    cands.sort(key=lambda x: -x[6])
    for rank,(rid,pmax,cap_close,recall,jacc,phi,conf,inter) in enumerate(cands[:3], start=1):
        m = res_meta[rid]
        matches.append(dict(
            res=b.res, sc=b.sc, is_storage=bool(b.is_storage), bidder_cap=round(float(cap),2),
            cand_rid=rid, cand_name=m["rname"], cand_pmax=float(pmax),
            cap_diff_pct=round(abs(cap-pmax)/pmax*100,2),
            dip_days=len(dips), overlap_days=int(inter),
            recall=round(float(recall),3), jaccard=round(float(jacc),3),
            phi=round(float(phi),3), confidence=round(float(conf),3), rank=rank))

log(f"  fingerprint-able bidders: {_dbg['passed_guard']:,}; candidate pairs scored: {_dbg['pairs']:,}")
mdf = pd.DataFrame(matches)
if len(mdf):
    mdf = mdf.sort_values(["confidence"], ascending=False).reset_index(drop=True)
mdf.to_parquet(f"{OUT}/reident_matches.parquet", index=False)
bidder.to_parquet(f"{OUT}/bidder_profiles.parquet", index=False)

# daily outage flags for the resources that appear as match candidates
# (lets the dashboard overlay a bidder's drop-outs against the named resource's outages)
if len(mdf):
    cand_ids = set(mdf["cand_rid"].unique())
    rod = []
    for rid in cand_ids:
        for d in sorted(res_days.get(rid, ())):
            rod.append((rid, d))
    pd.DataFrame(rod, columns=["rid","day"]).to_parquet(f"{OUT}/resource_outage_daily.parquet", index=False)

n_unique = 0
if len(mdf):
    top1 = mdf[mdf["rank"]==1]
    n_unique = int((top1["confidence"]>=0.6).sum())
log(f"  candidate matches: {len(mdf):,}; high-confidence (>=0.60) top-1 links: {n_unique:,}")

# =====================================================================
# meta.json
# =====================================================================
overview = con.execute("""
select count(distinct res) n_res, min(day) tmin, max(day) tmax from rh
""").fetchone()
meta = dict(
    generated_scope="CAISO RTM 2025 (full year)",
    n_bid_rows=int(con.execute(f"select count(*) from read_parquet('{BIDS}')").fetchone()[0]),
    n_resources=int(overview[0]),
    date_min=str(overview[1]), date_max=str(overview[2]),
    thresholds=dict(elevated_price=THR_ELEV, nearcap_price=THR_NEAR,
                    tight_percentile=TIGHT_PCTL, tight_mw=round(float(tight_thr),1),
                    cap_tolerance_pct=CAP_TOL*100, min_tight_hours=MIN_ELEV_HOURS),
    withholding_scored=int(n_wh),
    reident_candidate_bidders=int(mdf["res"].nunique()) if len(mdf) else 0,
    reident_highconf_links=n_unique,
    assumptions=[
        "The data doesn't include actual electricity prices, so we use hourly power-plant outages as a stand-in for how short the grid was.",
        "There's no fuel-cost data, so 'holding back power' is judged by comparing each plant to its own behavior in short vs. normal hours — not against what the power actually cost to make.",
        "When a forced outage has no recorded end time, we treat it as lasting one hour (the typical length).",
        "Each offer is a set of (amount, price) steps with prices that only go up; a plant's 'capacity' is the largest amount it offered.",
    ],
)
with open(f"{OUT}/meta.json","w") as f:
    json.dump(meta, f, indent=2)

log("DONE. Derived files in " + OUT)
for fn in sorted(os.listdir(OUT)):
    sz = os.path.getsize(os.path.join(OUT, fn))/1e6
    log(f"  {fn:34s} {sz:8.2f} MB")
