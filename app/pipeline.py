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

log("  building day-sets and scoring candidate matches...")
import pandas as pd, numpy as np
from collections import defaultdict

# Re-id leverages BOTH forced and planned outages. (Screen 1 'tightness'
# stays forced-only — planned outages aren't unexpected scarcity.) We build
# a dedicated outage table that keeps the outage TYPE, then score the
# fingerprint twice: forced-only, and the merged "unavailable" set
# (forced OR planned). The dashboard toggles between the two.
con.execute(f"""
create or replace table outg_reid as
select
  "RESOURCE ID"   as rid,
  "RESOURCE NAME" as rname,
  "OUTAGE TYPE"   as otype,
  date_trunc('hour', "CURTAILMENT START DATE TIME")                                as ostart,
  coalesce("CURTAILMENT END DATE TIME", "CURTAILMENT START DATE TIME" + interval 1 hour) as oend,
  "CURTAILMENT MW"   as cmw,
  "RESOURCE PMAX MW" as pmax,
  "NET QUALIFYING CAPACITY MW" as nqc
from read_parquet('{OUTG}')
where "OUTAGE TYPE" in ('FORCED','PLANNED')
  and "CURTAILMENT START DATE TIME" >= timestamp '2025-01-01'
  and "CURTAILMENT START DATE TIME" <  timestamp '2026-01-01'
""")

# resource metadata (name / PMAX / NQC / storage tag) from the union of
# forced+planned records, so planned-only resources are candidates too.
res_intervals = con.execute("""
select rid, any_value(rname) rname, max(pmax) pmax, max(nqc) nqc from outg_reid group by rid
""").fetchdf()

def name_is_storage(n):
    if not isinstance(n, str): return False
    n = n.upper()
    return any(k in n for k in ("STORAGE","BATTERY","BESS","ENERGY STORAGE"," ES", "_ES"))
res_meta = {}
for rid, rname, pmax, nqc in res_intervals[["rid","rname","pmax","nqc"]].itertuples(index=False):
    res_meta[rid] = dict(rname=rname, pmax=pmax, nqc=nqc, storage=name_is_storage(rname))

# expand outage intervals to daily coverage, split by type
def build_res_days(where_clause):
    rows = con.execute(f"select rid, ostart, oend from outg_reid {where_clause}").fetchdf()
    rd = defaultdict(set)
    for rid, s, e in rows.itertuples(index=False):
        d0 = pd.Timestamp(s).normalize(); d1 = pd.Timestamp(e).normalize()
        for d in pd.date_range(d0, d1, freq="D"):
            rd[rid].add(d.date())
    return rd

res_days_forced   = build_res_days("where otype='FORCED'")
res_days_planned  = build_res_days("where otype='PLANNED'")
res_days_combined = build_res_days("")   # forced OR planned

# bidder -> dip-days (mode-independent: derived from gaps in the bidder's OWN
# offers). A plant on ANY outage typically STOPS bidding (absent day) or
# offers collapsed capacity vs its TYPICAL day. Reference = median of present
# daily caps (robust); dip-days = {absent days in span} U {present days < 0.35*median}.
present_cap = defaultdict(dict)  # res -> {date: cap}
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

span_len = {b.res: (pd.Timestamp(b.last_day)-pd.Timestamp(b.first_day)).days + 1
            for b in bidder.itertuples(index=False)}
span_first = {b.res: pd.Timestamp(b.first_day).date() for b in bidder.itertuples(index=False)}
span_last  = {b.res: pd.Timestamp(b.last_day).date()  for b in bidder.itertuples(index=False)}

# ---------------------------------------------------------------------
# MAGNITUDE signal (leverages PARTIAL curtailments, not just full drop-outs):
# correlate a plant's daily curtailment fraction against the bidder's daily
# reduction in offered capacity. A plant losing 20% of PMAX should offer ~20%
# less — invisible to the binary dip test, but caught by this graded signal.
# ---------------------------------------------------------------------
# Plant: fraction of PMAX curtailed each day (forced OR planned), max concurrent.
oc = con.execute("select rid, ostart, oend, cmw, pmax from outg_reid where pmax > 0").fetchdf()
res_curt_frac = defaultdict(dict)   # rid -> {day: curtailed_fraction in (0,1]}
res_curt_mw   = defaultdict(dict)   # rid -> {day: max concurrent curtailment MW}  (for hover)
for rid, s, e, cmw, pmax in oc.itertuples(index=False):
    mw = float(cmw) if cmw is not None else 0.0
    f = min(1.0, mw / pmax) if pmax else 0.0
    for d in pd.date_range(pd.Timestamp(s).normalize(), pd.Timestamp(e).normalize(), freq="D"):
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
    dd = [d.date() for d in pd.date_range(pd.Timestamp(b.first_day), pd.Timestamp(b.last_day), freq="D")]
    caps = present_cap.get(b.res, {})
    span_days[b.res] = dd
    bidder_reduction[b.res] = np.array(
        [1.0 if caps.get(x) is None else float(np.clip(1 - caps[x]/typ, 0.0, 1.0)) for x in dd])

def spearman(x, y):
    # rank correlation; NaN when either series is constant or too short.
    if len(x) < 5 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    rx = pd.Series(x).rank().values; ry = pd.Series(y).rank().values
    c = np.corrcoef(rx, ry)[0, 1]
    return float(c) if np.isfinite(c) else float("nan")

def phi_coeff(dips, odays_span, N):
    # Matthews correlation between two binary day-vectors of length N.
    n11 = len(dips & odays_span)
    n10 = len(dips) - n11
    n01 = len(odays_span) - n11
    n00 = N - n11 - n10 - n01
    num = n11*n00 - n10*n01
    den = (n11+n10)*(n11+n01)*(n00+n10)*(n00+n01)
    return (num/(den**0.5)) if den > 0 else 0.0, n11

def run_reident(res_days, magnitude=False):
    """Score every fingerprint-able bidder against PMAX-matching candidate resources.

    Binary modes (magnitude=False): confidence = 0.60*phi + 0.25*size + 0.15*type,
      admitting candidates whose timing overlap phi >= 0.30.
    Magnitude mode (magnitude=True): also correlate the plant's daily curtailment
      fraction against the bidder's daily offered-reduction (Spearman rho), admit on
      strong timing OR strong magnitude, and blend
      confidence = 0.35*phi + 0.30*rho + 0.25*size + 0.10*type.
    Returns (sorted DF, n_fingerprintable)."""
    res_list = [(rid, res_meta[rid]["pmax"]) for rid in res_days
                if rid in res_meta and res_meta[rid]["pmax"]
                and not np.isnan(res_meta[rid]["pmax"]) and len(res_days[rid]) >= 4]
    res_list.sort(key=lambda x: x[1])
    res_pmax = np.array([p for _, p in res_list]) if res_list else np.array([0.0])
    res_ids  = [r for r, _ in res_list]
    matches, n_fp = [], 0
    for b in bidder.itertuples(index=False):
        cap = b.cap_ref
        if not cap or cap <= 0: continue
        dips = bidder_dip_days.get(b.res, set())
        N = span_len[b.res]
        if magnitude:
            # fingerprint-able if it has a clear dip pattern OR sustained partial reduction
            if b.res not in span_days or N < 30 or len(dips) > 0.85 * N:
                continue
            red_days = int(np.count_nonzero(bidder_reduction[b.res] >= 0.10))
            if len(dips) < 3 and red_days < 10:
                continue
        else:
            # need a specific, non-degenerate dip pattern to fingerprint on
            if len(dips) < 3 or N < 30 or len(dips) > 0.7 * N:
                continue
        n_fp += 1
        lo, hi = cap*(1-CAP_TOL), cap*(1+CAP_TOL)
        i0 = int(np.searchsorted(res_pmax, lo)); i1 = int(np.searchsorted(res_pmax, hi))
        d0, d1 = span_first[b.res], span_last[b.res]
        sdays = span_days.get(b.res); yred = bidder_reduction.get(b.res)
        cands = []
        for j in range(i0, i1):
            rid = res_ids[j]; pmax = res_pmax[j]
            odays = res_days.get(rid)
            if not odays: continue
            odays_span = {d for d in odays if d0 <= d <= d1}
            if not odays_span or len(odays_span) > 0.9 * N:   # down ~all span carries no timing signal
                continue
            phi, inter = phi_coeff(dips, odays_span, N)
            rho, n_curt = float("nan"), 0
            if magnitude and sdays is not None:
                cf = res_curt_frac.get(rid, {})
                xarr = np.fromiter((cf.get(dd, 0.0) for dd in sdays), dtype=float, count=len(sdays))
                n_curt = int(np.count_nonzero(xarr))
                if n_curt >= 10:                              # balanced guard: enough curtailment days
                    rho = spearman(xarr, yred)
            cap_close = 1 - abs(cap-pmax)/(pmax if pmax else 1)
            stor_match = (b.is_storage == res_meta[rid]["storage"])
            if magnitude:
                phi_ok = (phi >= 0.30 and inter >= 3)
                rho_ok = (not np.isnan(rho)) and rho >= 0.35   # balanced guard
                if not (phi_ok or rho_ok):
                    continue
                conf = (0.35*max(phi, 0.0) + 0.30*(max(rho, 0.0) if not np.isnan(rho) else 0.0)
                        + 0.25*cap_close + (0.10 if stor_match else 0.0))
            else:
                if phi < 0.30 or inter < 3:                   # require distinctive timing coincidence
                    continue
                conf = 0.60*phi + 0.25*cap_close + (0.15 if stor_match else 0.0)
            recall = inter/len(dips) if dips else 0.0
            jacc   = inter/len(dips | odays_span) if (dips or odays_span) else 0.0
            cands.append((rid, pmax, cap_close, recall, jacc, phi, conf, inter, rho))
        cands.sort(key=lambda x: -x[6])
        for rank,(rid,pmax,cap_close,recall,jacc,phi,conf,inter,rho) in enumerate(cands[:3], start=1):
            m = res_meta[rid]
            rec = dict(
                res=b.res, sc=b.sc, is_storage=bool(b.is_storage), bidder_cap=round(float(cap),2),
                cand_rid=rid, cand_name=m["rname"], cand_pmax=float(pmax),
                cap_diff_pct=round(abs(cap-pmax)/pmax*100,2),
                dip_days=len(dips), overlap_days=int(inter),
                recall=round(float(recall),3), jaccard=round(float(jacc),3),
                phi=round(float(phi),3), confidence=round(float(conf),3), rank=rank)
            if magnitude:
                rec["rho"] = None if np.isnan(rho) else round(float(rho), 3)
            matches.append(rec)
    mdf = pd.DataFrame(matches)
    if len(mdf):
        mdf = mdf.sort_values(["confidence"], ascending=False).reset_index(drop=True)
    return mdf, n_fp

log("  scoring fingerprints (forced; forced+planned; magnitude-aware)...")
mdf_forced,    fp_forced = run_reident(res_days_forced)
mdf_combined,  fp_comb   = run_reident(res_days_combined)
mdf_magnitude, fp_mag    = run_reident(res_days_combined, magnitude=True)

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
log(f"  hourly cap series written for {len(matched_res):,} matched bidders")

def highconf(mdf):
    if not len(mdf): return 0
    return int((mdf[mdf["rank"]==1]["confidence"] >= 0.6).sum())
hc_forced, hc_comb, hc_mag = highconf(mdf_forced), highconf(mdf_combined), highconf(mdf_magnitude)
log(f"  fingerprint-able bidders: forced={fp_forced:,}, combined={fp_comb:,}, magnitude={fp_mag:,}")
log(f"  high-confidence (>=0.60) links: forced={hc_forced:,}, combined={hc_comb:,} (+{hc_comb-hc_forced}), "
    f"magnitude={hc_mag:,} (+{hc_mag-hc_forced} vs forced)")

# daily outage overlay for the drill-down. Tag each candidate day forced vs
# planned (forced precedence); cover candidates from ALL result sets.
cand_ids = set()
for mdf in (mdf_forced, mdf_combined, mdf_magnitude):
    if len(mdf):
        cand_ids |= set(mdf["cand_rid"].unique())
rod = []
for rid in cand_ids:
    pmax = res_meta.get(rid, {}).get("pmax")
    mwd  = res_curt_mw.get(rid, {})
    fdays = res_days_forced.get(rid, set())
    pdays = res_days_planned.get(rid, set())
    for d in sorted(fdays):
        rod.append((rid, d, "forced", mwd.get(d), pmax))
    for d in sorted(pdays - fdays):
        rod.append((rid, d, "planned", mwd.get(d), pmax))
pd.DataFrame(rod, columns=["rid","day","kind","curt_mw","pmax"]).to_parquet(
    f"{OUT}/resource_outage_daily.parquet", index=False)

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
    reident_candidate_bidders=int(mdf_forced["res"].nunique()) if len(mdf_forced) else 0,
    reident_candidate_bidders_combined=int(mdf_combined["res"].nunique()) if len(mdf_combined) else 0,
    reident_candidate_bidders_magnitude=int(mdf_magnitude["res"].nunique()) if len(mdf_magnitude) else 0,
    reident_highconf_links=hc_forced,
    reident_highconf_links_combined=hc_comb,
    reident_highconf_links_magnitude=hc_mag,
    assumptions=[
        "The data doesn't include actual electricity prices, so we use hourly power-plant outages as a stand-in for how short the grid was.",
        "There's no fuel-cost data, so 'holding back power' is judged by comparing each plant to its own behavior in short vs. normal hours — not against what the power actually cost to make.",
        "When a forced outage has no recorded end time, we treat it as lasting one hour (the typical length).",
        "Each offer is a set of (amount, price) steps with prices that only go up; a plant's 'capacity' is the largest amount it offered.",
        "Unmasking (Screen 2) matches a bidder's quiet days against a plant's outage days; you can compare three methods — forced outages only, forced + planned, or magnitude-aware (which also correlates the size of partial curtailments against how much the bidder scaled back its offers). The 'grid is short' scarcity measure (Screen 1) always uses forced outages only.",
    ],
)
with open(f"{OUT}/meta.json","w") as f:
    json.dump(meta, f, indent=2)

log("DONE. Derived files in " + OUT)
for fn in sorted(os.listdir(OUT)):
    sz = os.path.getsize(os.path.join(OUT, fn))/1e6
    log(f"  {fn:34s} {sz:8.2f} MB")
