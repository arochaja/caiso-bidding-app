#!/usr/bin/env python3
"""Local outage-price spike screen — the geographic half of the panel.

WHY THIS EXISTS, AND WHY IT IS NOT THE "SYSTEM-TIGHT" SIGNAL THE REST OF THE
TOOL USES.  Screen 1 calls an hour scarce when statewide forced-outage MW and
the statewide price are both in their top decile.  That is a fine description
of a hard afternoon, but it is a poor test of any ONE plant: a heat wave lifts
every node at once, so the whole fleet is "scarce" together and nothing is
attributable.  This module applies the strict, geographic reading instead — the
price NEAR a plant has to pull away from the rest of the state while that
plant's own capacity is sitting offline.  A statewide event therefore cannot by
itself flag anything, which is the entire point.

The screen is a port of viz/build_outage_hours.py and viz/build_outage_rtm.py
(the price/outage animations), whose thresholds were tuned against the 2025 map
and are reproduced here unchanged so the panel and the films agree.

WHAT IT CAN AND CANNOT REACH.  Outage records carry REAL CAISO resource ids
(``ALTA3A_2_CPCE5``), whose substation prefix is what makes geolocation
possible at all.  Bid records carry a bare anonymous integer (``366805``), no
node, no zone, no name, and GHG_AREA is null in all 17.7M rows.  So geography
can say WHEN a local separation happened and AT WHICH real plant — it can never
say which anonymous bidder stood near it.  The event set produced here is
therefore a set of HOURS, and the bid-side stage scores every bidder against
those hours.  Nothing in this file, or downstream of it, claims proximity
between a bidder and an outage.

Products (all under app/data/derived/):
  local_spike_geo.parquet     one row per outage resource: coords + which rung
                              of the matching ladder placed it
  local_spike_events.parquet  one row per episode: plant, market, hours, depth,
                              peak local price and premium
  local_spike_hours.parquet   the hourly/5-minute spine with an is_event flag,
                              which is what the bid-side stage joins on

Heavy intermediates (node price surfaces) are cached under CAISO_SCRATCH so a
re-run does not re-read the 3 GB day-ahead file or the 51 GB real-time set.
"""

import json
import os
import re
import warnings

import duckdb
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "caiso-data"))
OUT = os.path.join(HERE, "data", "derived")
SCRATCH = os.environ.get("CAISO_SCRATCH", os.path.join(HERE, ".cache"))

OUTG = os.path.join(DATA, "2025-OUTAGES-v2.parquet")
PP_CSV = os.path.join(DATA, "powerplants.csv")
LOCS = os.path.join(DATA, "LMPLocations_vs_FullList.xls")
LMP = os.path.join(DATA, "2025-DAM-LMP-full.parquet")
RTM_GLOBS = [
    os.path.join(DATA, "2025-RTM-LMP", "RTM_2025-*.parquet"),
    os.path.join(DATA, "2025-RTM-LMP", "december parts", "*.parquet"),
]

# ---- screen thresholds (identical to viz/, do not drift) -------------------
NEAR_K = 5  # nodes averaged to form the "local price" around a plant
NEAR_KM = 100.0  # ...but only nodes this close count
WINDOW_H = 48  # hours after an outage starts in which we will look
PRE_H = 72  # hours before it, used as the plant's own baseline
MIN_MW = 50.0  # ignore small curtailments; they cannot move a hub
MIN_JUMP = 10.0  # $/MWh the premium must rise vs the plant's own pre-window
GAP_H = 6  # a lull this short is the same episode, not a new one

# The percentile and price floor are the two knobs that DO differ by market.
# Real-time local premiums are far noisier than day-ahead ones, so the SAME bar
# is cleared far more often for reasons that are mostly noise: at the day-ahead
# thresholds the real-time screen flags 8.6% of the year against day-ahead's 5%
# (viz/README.md, "the real-time film: what differs"). Scoring bids against an
# event set that size would measure the market's ordinary churn, so the
# real-time bar is raised — the plant's own premium must reach its top 1% rather
# than its top 5%, and the absolute local price $150 rather than $75 — until the
# two markets flag a comparable slice of the year. As built: day-ahead 463 event
# hours (5.3%), real-time 331 (3.8%). The two counts are meant to be read side
# by side, which they cannot be if one bar is four times looser than the other.
DAM_PCTL, DAM_MIN_LMP = 95.0, 75.0
RTM_PCTL, RTM_MIN_LMP = 99.0, 150.0

CA_BOX = (-124.6, -114.0, 32.4, 42.1)  # anything outside is a bad match, not a plant


def log(m):
    print(f"[spike_local] {m}", flush=True)


# =====================================================================
# Geolocation of outage resources
# =====================================================================
def _norm(s):
    """Collapse a plant name to a comparison key: letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


# Boilerplate that appears in CEC or CAISO names but carries no identity.
# This list does the real work: "ORMOND BEACH GEN STA. UNIT 2" and "Ormond
# Beach Generating Station" only converge once all of it is gone.
_STOP = re.compile(
    r"\b(POWER|PLANT|ENERGY|CENTER|CENTRE|STATION|STA|GENERATING|GENERATION|"
    r"GEN|PROJECT|FACILITY|LLC|INC|LP|CO|COMPANY|CORP|CITY|OF|THE|CAISO|"
    r"UNIT|UNITS|AGGREGATE|AGGREGATED|COMBINED|CYCLE|COGENERATION|COGEN|"
    r"SOLAR|WIND|FARM|STORAGE|BESS|BATTERY|HYDRO|PUMPED|PUMP|PSP|"
    r"PH|GT|CT|CC|CTG|STG|NSPIN|DYN|SCE|PGE|SDGE)\b"
)
# A trailing unit designator: "... UNIT 2", "... 3", "... II". Two resources at
# the same plant differ only here, and both belong at the same coordinate.
_UNIT_TAIL = re.compile(r"\b([0-9]+[A-Z]?|[IVX]{1,4})$")


def _tokens(s):
    """Identity-bearing words of a name, for fuzzy comparison."""
    t = _STOP.sub(" ", re.sub(r"[^A-Z0-9 ]", " ", str(s).upper()))
    t = " ".join(w for w in t.split() if len(w) > 2)
    return _UNIT_TAIL.sub("", t).strip()


def _in_ca(lat, lon):
    return (
        pd.notna(lat)
        and pd.notna(lon)
        and CA_BOX[0] <= lon <= CA_BOX[1]
        and CA_BOX[2] <= lat <= CA_BOX[3]
    )


def build_geo(con):
    """Place outage resources on the map via a four-rung ladder.

    Neither reference table covers everything and they key on different things,
    so each resource records WHICH rung placed it. A wrong coordinate is worse
    than a missing one — every rung is guarded, and anything landing outside
    California is rejected rather than drawn.
    """
    from rapidfuzz import fuzz, process

    res = con.sql(f"""
        select "RESOURCE ID"                 as rid,
               max("RESOURCE NAME")          as rname,
               max("RESOURCE PMAX MW")       as pmax,
               max("CURTAILMENT MW")         as max_curt,
               count(distinct "OUTAGE MRID") as n_outages
        from read_parquet('{OUTG}')
        where "OUTAGE TYPE" in ('FORCED','PLANNED')
        group by 1
    """).df()

    # reference A: CEC plant list (name -> coordinate)
    pp = con.sql(f"""
        select PlantName, County, Capacity_Latest as cap, PriEnergySource as fuel,
               x as lon, y as lat, "Retired Plant" as retired
        from read_csv_auto('{PP_CSV}')
        where x is not null and y is not null
    """).df()
    pp["k"] = pp.PlantName.map(_norm)
    pp["tok"] = pp.PlantName.map(_tokens)

    # reference B: CAISO pricing nodes (node id -> coordinate). A node id is
    # SUBSTATION_VOLTAGE_UNIT; the first two parts identify the physical
    # substation, which is where the generator interconnects.
    loc = pd.read_excel(LOCS, sheet_name=0).dropna(subset=["latitude", "longitude"])
    loc["pfx"] = loc["name"].str.extract(r"^([A-Z0-9]+_\d)_")
    loc["sub"] = loc["name"].str.split("_").str[0]
    by_pfx = loc.dropna(subset=["pfx"]).groupby("pfx")[["latitude", "longitude"]].mean()
    by_sub = loc.groupby("sub")[["latitude", "longitude"]].mean()

    res["k"] = res.rname.map(_norm)
    res["tok"] = res.rname.map(_tokens)
    res["pfx"] = res.rid.str.extract(r"^([A-Z0-9]+_\d)_")
    res["sub"] = res.rid.str.split("_").str[0]

    pp_exact = pp.drop_duplicates("k").set_index("k")
    pool = pp[pp.tok.str.len() > 0].reset_index(drop=True)
    pool_keys = pool.tok.tolist()

    def fuzzy_plant(tok, pmax):
        """Best CEC plant for these identity tokens, or None.

        Guarded three ways, because this is the only rung that can invent a
        location: token_sort_ratio (NOT token_set_ratio, which scores a
        one-word query as a perfect match against any name containing that
        word), a shared-longest-token requirement, and a preference for
        operating plants whose nameplate is closest to the resource's PMAX.
        """
        hits = process.extract(
            tok, pool_keys, scorer=fuzz.token_sort_ratio, score_cutoff=88, limit=25
        )
        if not hits:
            return None
        anchor = max(tok.split(), key=len)  # longest word = most distinctive
        cands = [pool.iloc[i] for _, _, i in hits if anchor in pool_keys[i].split()]
        if not cands:
            return None

        def rank(c):
            gap = abs(c.cap - pmax) if pd.notna(c.cap) and pd.notna(pmax) else 1e9
            return (int(c.retired or 0), gap)

        best = min(cands, key=rank)
        return best if _in_ca(best.lat, best.lon) else None

    lats, lons, srcs, audit = [], [], [], []
    for r in res.itertuples():
        lat = lon = src = None

        # rung 1 — exact plant name. Highest confidence: it IS the plant.
        if r.k in pp_exact.index:
            row = pp_exact.loc[r.k]
            if _in_ca(row.lat, row.lon):
                lat, lon, src = row.lat, row.lon, "plant_name"

        # rung 2 — substation+voltage from the resource id: not the plant
        # itself but the busbar it connects to, within a few km.
        if lat is None and pd.notna(r.pfx) and r.pfx in by_pfx.index:
            row = by_pfx.loc[r.pfx]
            if _in_ca(row.latitude, row.longitude):
                lat, lon, src = row.latitude, row.longitude, "node_prefix"

        # rung 3 — fuzzy plant name on identity tokens only. "Alta Wind 1" vs
        # "Alta Wind Energy Center 1": same plant, different boilerplate.
        if lat is None and r.tok:
            best = fuzzy_plant(r.tok, r.pmax)
            if best is not None:
                lat, lon, src = best.lat, best.lon, "plant_fuzzy"
                audit.append((r.rid, r.rname, best.PlantName, r.pmax, best.cap))

        # rung 4 — substation token alone: several voltages of one switchyard
        # averaged. Coarsest, but still the right neighbourhood.
        if lat is None and r.sub in by_sub.index:
            row = by_sub.loc[r.sub]
            if _in_ca(row.latitude, row.longitude):
                lat, lon, src = row.latitude, row.longitude, "substation"

        lats.append(lat)
        lons.append(lon)
        srcs.append(src or "unplaced")

    res["lat"], res["lon"], res["geo_source"] = lats, lons, srcs
    res = res.drop(columns=["k", "tok"])

    placed = res.geo_source != "unplaced"
    mw = res.pmax.fillna(0)
    cov = {
        "n_resources": int(len(res)),
        "n_placed": int(placed.sum()),
        "pct_placed": round(100 * placed.mean(), 1),
        "pct_mw_placed": round(100 * mw[placed].sum() / mw.sum(), 1),
        "by_source": {k: int(v) for k, v in res.geo_source.value_counts().items()},
    }
    log(
        f"  geolocated {cov['n_placed']}/{cov['n_resources']} outage resources "
        f"({cov['pct_placed']}%), {cov['pct_mw_placed']}% of PMAX; "
        + ", ".join(f"{k} {v}" for k, v in cov["by_source"].items())
    )
    # The fuzzy rung is written out in full so it can be eyeballed.
    pd.DataFrame(
        audit, columns=["rid", "outage_name", "matched_cec_plant", "pmax", "cec_cap"]
    ).to_csv(os.path.join(OUT, "local_spike_geo_fuzzy_audit.csv"), index=False)
    res.to_parquet(os.path.join(OUT, "local_spike_geo.parquet"), index=False)
    return res, cov


# =====================================================================
# Node price surfaces
# =====================================================================
# Both markets are reduced to the same shape: a (steps x nodes) float32 matrix
# on a gap-free UTC spine, plus the node coordinates. UTC, not Pacific: the
# local calendar repeats an hour every November and skips one every March, and
# a screen that compares an hour against the 72 hours before it must not have
# its clock jump underneath it. The conversion back to the app's Pacific
# wall-clock convention happens once, at the very end, where the bid tables are
# joined.
#
# These are expensive (a 2.9 GB read for day-ahead, 51 GB for real-time), so
# both are cached under CAISO_SCRATCH and only rebuilt when missing.

DAM_CACHE = os.path.join(SCRATCH, "spike_dam_grid.npz")
RTM_GRID = os.path.join(SCRATCH, "spike_rtm_grid.npy")
RTM_META = os.path.join(SCRATCH, "spike_rtm_meta.npz")


def _utc_spine(freq):
    """Pacific-local 2025 stored as the matching UTC instants.

    Anchoring the year on UTC directly would open on 31 Dec 2024 and cut off at
    16:00 on New Year's Eve; a California market runs on a California calendar.
    The stored values are the UTC instants of those local hours, so the spine is
    gap-free even across the two DST discontinuities.
    """
    local = pd.date_range(
        "2025-01-01 00:00",
        "2025-12-31 23:55" if freq == "5min" else "2025-12-31 23:00",
        freq=freq,
        tz="America/Los_Angeles",
    )
    return pd.DatetimeIndex(local.tz_convert("UTC").tz_localize(None))


def _geocoded_nodes(con, present_ids):
    loc = pd.read_excel(LOCS, sheet_name=0).dropna(subset=["latitude", "longitude"])
    loc = loc.drop_duplicates("name")
    loc = loc[loc.name.isin(present_ids)].sort_values("name").reset_index(drop=True)
    return loc


def build_dam_surface(con):
    """Hourly day-ahead LMP for every geocoded node. ~2.9 GB read, once."""
    if os.path.exists(DAM_CACHE):
        log(f"  day-ahead surface: cached ({os.path.basename(DAM_CACHE)})")
        return
    log("  building day-ahead node price surface (reads the full-year LMP file)...")
    present = set(
        con.sql(f"""
        select distinct NODE_ID from read_parquet('{LMP}')
        where LMP_TYPE='LMP' and MARKET_RUN_ID='DAM'
        """)
        .df()
        .NODE_ID
    )
    nodes = _geocoded_nodes(con, present)
    log(f"    {len(nodes)} geocoded nodes carry 2025 day-ahead prices")
    con.register("dam_nodes", nodes[["name"]])
    # AT TIME ZONE 'UTC' pins the instant. Do NOT use strptime(...,'%z')::timestamp:
    # DuckDB renders that in the SESSION timezone, so on a Pacific machine it
    # silently returns local time labelled as UTC and puts every hour 8 out.
    df = con.sql(f"""
        select p.NODE_ID as node_id,
               (p.INTERVALSTARTTIME_GMT::TIMESTAMPTZ AT TIME ZONE 'UTC') as h_utc,
               avg(p.MW) as lmp
        from read_parquet('{LMP}') p
        join dam_nodes n on n.name = p.NODE_ID
        where p.LMP_TYPE='LMP' and p.MARKET_RUN_ID='DAM'
        group by 1, 2
    """).df()

    spine = _utc_spine("h")
    hi = {h: i for i, h in enumerate(spine)}
    ni = {n: i for i, n in enumerate(nodes.name)}
    grid = np.full((len(spine), len(nodes)), np.nan, dtype=np.float32)
    keep = df.h_utc.isin(hi)
    grid[df.h_utc[keep].map(hi).values, df.node_id[keep].map(ni).values] = df.lmp[
        keep
    ].values.astype(np.float32)
    # CAISO published no day-ahead LMP at all for trade date 2025-03-09. Those
    # hours stay NaN rather than being dropped, so the calendar is not silently
    # compressed and the 72-hour baseline window keeps its true span.
    blank = int((~np.isfinite(grid)).all(axis=1).sum())
    log(
        f"    matrix {grid.shape}, {100 * np.isfinite(grid).mean():.1f}% populated"
        f"{f', {blank} hours with no published price' if blank else ''}"
    )
    np.savez_compressed(
        DAM_CACHE,
        grid=grid,
        lat=nodes.latitude.values.astype(np.float32),
        lon=nodes.longitude.values.astype(np.float32),
        node_id=nodes.name.values.astype(str),
        spine=spine.values.astype("datetime64[ns]"),
    )


def _rtm_groups():
    """The monthly parquets, plus December's per-hour parts as one group."""
    import glob as _glob

    groups = [(os.path.basename(p), [p]) for p in sorted(_glob.glob(RTM_GLOBS[0]))]
    dec = sorted(_glob.glob(RTM_GLOBS[1]))
    if dec:
        groups.append(("december parts", dec))
    return groups


def build_rtm_surface(con):
    """5-minute real-time LMP for every geocoded node. ~51 GB read, once.

    105,120 intervals x ~2,088 nodes is 878 MB as float32, which does not fit
    comfortably in 8 GB alongside everything else, so it is written straight to
    a memory-mapped .npy and filled one month at a time.
    """
    if os.path.exists(RTM_GRID) and os.path.exists(RTM_META):
        log(f"  real-time surface: cached ({os.path.basename(RTM_GRID)})")
        return
    groups = _rtm_groups()
    log(f"  building real-time node price surface from {len(groups)} source groups...")
    # One month carries every node, so the universe resolves without a full scan.
    present = set(
        con.sql(f"""
        select distinct NODE_ID from read_parquet({groups[0][1]!r}) where LMP_TYPE='LMP'
        """)
        .df()
        .NODE_ID
    )
    nodes = _geocoded_nodes(con, present)
    log(f"    {len(nodes)} geocoded nodes carry real-time prices")
    con.register("rtm_nodes", nodes[["name"]])
    ni = {n: i for i, n in enumerate(nodes.name)}

    spine = _utc_spine("5min")
    ti = pd.Series(np.arange(len(spine), dtype=np.int64), index=spine)
    grid = np.lib.format.open_memmap(
        RTM_GRID, mode="w+", dtype=np.float32, shape=(len(spine), len(nodes))
    )
    grid[:] = np.nan
    # Hourly subsample kept in RAM purely for coverage stats and a per-node
    # median; scanning the 878 MB memmap for either would cost minutes.
    sub = np.full((8760, len(nodes)), np.nan, dtype=np.float32)

    for label, files in groups:
        # INTERVALSTARTTIME_GMT is a NAIVE TIMESTAMP already in GMT here — the
        # mirror image of the day-ahead file. Applying ::TIMESTAMPTZ to it would
        # resolve it in the session timezone and leave the series 7-8 hours out.
        df = con.sql(f"""
            select p.NODE_ID as node_id, p.INTERVALSTARTTIME_GMT as ts, avg(p.VALUE) as lmp
            from read_parquet({files!r}) p
            join rtm_nodes n on n.name = p.NODE_ID
            where p.LMP_TYPE='LMP'
            group by 1, 2
        """).df()
        t = ti.reindex(pd.DatetimeIndex(df.ts)).values
        keep = ~np.isnan(t)
        rows = t[keep].astype(np.int64)
        cols = df.node_id[keep].map(ni).values.astype(np.int64)
        vals = df.lmp[keep].values.astype(np.float32)
        grid[rows, cols] = vals
        on_hour = rows % 12 == 0
        sub[rows[on_hour] // 12, cols[on_hour]] = vals[on_hour]
        log(f"    {label}: {len(df):,} rows, {len(rows):,} placed")
        del df, t, rows, cols, vals
    grid.flush()

    node_med = np.nanmedian(sub, axis=0).astype(np.float32)
    node_med = np.nan_to_num(node_med, nan=float(np.nanmedian(node_med)))
    log(f"    matrix {grid.shape}, ~{100 * np.isfinite(sub).mean():.1f}% populated")
    np.savez_compressed(
        RTM_META,
        lat=nodes.latitude.values.astype(np.float32),
        lon=nodes.longitude.values.astype(np.float32),
        node_id=nodes.name.values.astype(str),
        spine=spine.values.astype("datetime64[ns]"),
        node_med=node_med,
    )


# =====================================================================
# The screen
# =====================================================================
def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def _outage_segments(con, rids):
    """Collapsed forced/planned segments at placed resources, on the UTC clock.

    The collapse key is (MRID, type, start) with the FURTHEST end — the rule
    Stage 1 of the pipeline already uses, and the reason the tightness series is
    not inflated sevenfold. CAISO files one physical curtailment as chained
    sub-intervals, under many MRIDs, and again in every later day's report.
    """
    seg = con.sql(f"""
        select "OUTAGE MRID"                 as mrid,
               min("RESOURCE ID")            as rid,
               "OUTAGE TYPE"                 as otype,
               "CURTAILMENT START DATE TIME" as ostart,
               max("CURTAILMENT END FILLED") as oend,
               max("CURTAILMENT MW")         as cmw
        from read_parquet('{OUTG}')
        where "OUTAGE TYPE" in ('FORCED','PLANNED')
        group by "OUTAGE MRID", "OUTAGE TYPE", "CURTAILMENT START DATE TIME"
    """).df()
    seg = seg[seg.rid.isin(set(rids))].copy()
    # CAISO files these as Pacific wall-clock; the spine is UTC instants. The
    # fall-back hour is genuinely ambiguous once a year — take the first pass.
    for c in ("ostart", "oend"):
        seg[c] = (
            pd.to_datetime(seg[c])
            .dt.tz_localize("America/Los_Angeles", ambiguous=True, nonexistent="shift_forward")
            .dt.tz_convert("UTC")
            .dt.tz_localize(None)
        )
    seg = seg.dropna(subset=["ostart", "oend"])
    return seg[seg.oend > seg.ostart]


def _forced_depth(seg, ridx, n_hours, n_r, h0):
    """Hourly per-resource curtailment depth, forced and planned kept apart.

    np.maximum, never a sum: a resource contributes its DEEPEST active
    curtailment for the hour. Summing across its own overlapping segments
    invents capacity that was never offline.
    """

    def hour_index(ts):
        return int(np.floor((ts - h0) / np.timedelta64(1, "h")))

    forced = np.zeros((n_hours, n_r), dtype=np.float32)
    planned = np.zeros((n_hours, n_r), dtype=np.float32)
    for s in seg.itertuples():
        i0 = max(0, hour_index(s.ostart))
        i1 = min(n_hours, hour_index(s.oend) + 1)
        if i1 <= i0 or pd.isna(s.cmw):
            continue
        j = ridx[s.rid]
        tgt = forced if s.otype == "FORCED" else planned
        np.maximum(tgt[i0:i1, j], np.float32(s.cmw), out=tgt[i0:i1, j])
    return forced, planned


def _local_price(grid, nlat, nlon, geo, n_t, resource_major_path=None, chunk=4096):
    """Local price series per plant: mean of the up-to-K priced nodes within
    NEAR_KM, built RESOURCE-MAJOR.

    Resource-major matters for real time: the screen walks one plant's whole
    year at a time, and a column read out of a row-major 878 MB memmap would
    rescan the whole file once per plant.

    Missing node prices are skipped (nanmean), not back-filled with the node's
    own annual median as the animation does. Filling injects a synthetic price
    into a comparison whose entire purpose is to detect a real one; where every
    neighbour is missing the plant simply has no local price for that step.
    """
    n_r = len(geo)
    d = _haversine_km(
        geo.lat.values[:, None], geo.lon.values[:, None], nlat[None, :], nlon[None, :]
    )
    order = np.argsort(d, axis=1)[:, :NEAR_K]
    near_ok = np.take_along_axis(d, order, axis=1) <= NEAR_KM
    log(f"    {int(near_ok.any(1).sum())}/{n_r} plants have a priced node within {NEAR_KM:.0f} km")

    if resource_major_path:
        local = np.lib.format.open_memmap(
            resource_major_path, mode="w+", dtype=np.float32, shape=(n_r, n_t)
        )
    else:
        local = np.full((n_r, n_t), np.nan, dtype=np.float32)
    system = np.empty(n_t, dtype=np.float32)

    # All-NaN slices are expected and meaningful: CAISO published no day-ahead
    # price at all on 2025-03-09, and some nodes drop out for stretches. NaN is
    # the correct answer there, so the warning is noise.
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.filterwarnings("ignore", r"All-NaN slice|Mean of empty slice", RuntimeWarning)
        for a in range(0, n_t, chunk):
            b = min(a + chunk, n_t)
            block = np.asarray(grid[a:b])
            system[a:b] = np.nanmedian(block, axis=1)
            for j in range(n_r):
                cols = order[j][near_ok[j]]
                local[j, a:b] = np.nanmean(block[:, cols], axis=1) if len(cols) else np.nan
            del block
    if resource_major_path:
        local.flush()
    return local, system, near_ok


def _screen(market, geo, local, system, forced, seg, spine, steps_per_hour, pctl, min_lmp):
    """Flag plant-steps, then collapse runs of them into episodes.

    All four conditions must hold at once (viz/README.md "What 'tied to a local
    price spike' means"):
      1. a forced outage of >= MIN_MW at that plant began within WINDOW_H, and
         the capacity is STILL offline at the flagged step;
      2. the local premium over the statewide median is in that plant's OWN
         top (100-pctl)% for the year;
      3. that premium is >= MIN_JUMP above the plant's own average premium over
         the preceding PRE_H hours, so a location that is simply always
         expensive does not flag every hour;
      4. the absolute local price is >= min_lmp.

    Condition 2 is the one that makes this a LOCAL screen: the bar is the
    plant's own distribution, measured against the rest of the state, so a heat
    wave that lifts every node at once clears nothing.
    """
    n_r, n_t = len(geo), len(spine)
    ridx = {r: i for i, r in enumerate(geo.rid)}
    h0 = spine[0]
    W, P = WINDOW_H * steps_per_hour, PRE_H * steps_per_hour
    GAP = GAP_H * steps_per_hour

    starts = seg[(seg.otype == "FORCED") & (seg.cmw >= MIN_MW)].copy()
    # One physical event is filed under many MRIDs; collapse to resource+start.
    starts = starts.groupby(["rid", "ostart"], as_index=False).cmw.max()
    starts["j"] = starts.rid.map(ridx)
    by_res = {j: g for j, g in starts.groupby("j")}

    flagged_any = np.zeros(n_t, dtype=bool)
    n_flagged_plants = np.zeros(n_t, dtype=np.int16)
    events, thr_used = [], []

    for j in range(n_r):
        if j not in by_res:
            continue
        loc_j = np.asarray(local[j])
        prem_j = loc_j - system
        finite = np.isfinite(prem_j)
        if not finite.any():
            continue
        thr = float(np.percentile(prem_j[finite], pctl))
        thr_used.append(thr)
        forced_j = forced[:, j]

        hits = np.zeros(n_t, dtype=bool)
        for s in by_res[j].itertuples():
            i0 = int(np.floor((s.ostart - h0) / np.timedelta64(1, "h"))) * steps_per_hour
            w0, w1 = max(0, i0), min(n_t, i0 + W)
            p0, p1 = max(0, i0 - P), max(0, i0)
            if w1 <= w0 or p1 <= p0:
                continue
            base = np.nanmean(prem_j[p0:p1])
            if not np.isfinite(base):
                continue
            # the capacity must still be offline at the flagged step; without
            # this an outage that ended before the spike would be credited it
            off = forced_j[np.arange(w0, w1) // steps_per_hour] > 0
            win = prem_j[w0:w1]
            hits[w0:w1] |= (
                np.isfinite(win)
                & (win >= thr)
                & (loc_j[w0:w1] >= min_lmp)
                & (win >= base + MIN_JUMP)
                & off
            )
        if not hits.any():
            continue
        flagged_any |= hits
        n_flagged_plants += hits.astype(np.int16)

        # Runs of flagged steps are ONE episode. CAISO re-files an ongoing
        # outage with a fresh start time every hour, so iterating filings
        # instead of episodes turned a single Gilroy Cogen event into twelve.
        idx = np.nonzero(hits)[0]
        for run in np.split(idx, np.nonzero(np.diff(idx) > GAP)[0] + 1):
            a, b = int(run[0]), int(run[-1])
            # the hours that actually flagged, not the span they bracket
            hrs_run = np.unique(run // steps_per_hour)
            depth = forced[hrs_run, j]
            events.append(
                {
                    "market": market,
                    "rid": geo.rid.iloc[j],
                    "rname": geo.rname.iloc[j],
                    "lat": float(geo.lat.iloc[j]),
                    "lon": float(geo.lon.iloc[j]),
                    "geo_source": geo.geo_source.iloc[j],
                    "start_utc": spine[a],
                    "end_utc": spine[b],
                    "curt_mw": float(np.nanmax(depth)),
                    # MIN_MW gates the outage START; the still-offline test is
                    # only "> 0", so an event can flag while a token remnant is
                    # down. Kept faithful to the animation's screen, but the
                    # depth at the flagged hours is published so the panel can
                    # filter on it instead of taking every flag at face value.
                    "offline_mw_min": float(np.nanmin(depth)),
                    "pmax": float(geo.pmax.iloc[j]) if pd.notna(geo.pmax.iloc[j]) else np.nan,
                    "n_steps": int(len(run)),
                    "n_hours": int(len(np.unique(run // steps_per_hour))),
                    "peak_local_lmp": float(np.nanmax(loc_j[run])),
                    "peak_premium": float(np.nanmax(prem_j[run])),
                    "mean_premium": float(np.nanmean(prem_j[run])),
                    "prem_threshold": thr,
                }
            )

    ev = pd.DataFrame(events)
    # The screen runs on UTC, but every bid table in this app is keyed on
    # Pacific wall-clock, so each event also carries the local window the
    # dashboard uses to pull the offer curves submitted inside it.
    if len(ev):
        for _c in ("start", "end"):
            ev[f"{_c}_local"] = (
                pd.DatetimeIndex(ev[f"{_c}_utc"])
                .tz_localize("UTC")
                .tz_convert("America/Los_Angeles")
                .tz_localize(None)
            )
        ev.insert(0, "event_id", ev.market + "-" + ev.index.map("{:04d}".format))
    # Hour-grain spine, which is the grain the bid tables live on. A real-time
    # hour counts as an event hour when ANY of its twelve intervals flagged.
    n_hours = n_t // steps_per_hour
    hour_flag = flagged_any.reshape(n_hours, steps_per_hour).any(axis=1)
    hour_n = n_flagged_plants.reshape(n_hours, steps_per_hour).max(axis=1)
    hspine = spine[::steps_per_hour]
    hours = pd.DataFrame(
        {
            "market": market,
            "h_utc": hspine,
            # the app's whole bid/price layer is keyed on Pacific wall-clock
            "h": pd.DatetimeIndex(hspine)
            .tz_localize("UTC")
            .tz_convert("America/Los_Angeles")
            .tz_localize(None),
            "is_event": hour_flag,
            "n_plants": hour_n,
        }
    )
    log(
        f"  {market}: {len(ev)} events at {ev.rid.nunique() if len(ev) else 0} plants, "
        f"{int(hour_flag.sum())} event hours of {n_hours:,} "
        f"({100 * hour_flag.mean():.1f}% of the year); "
        f"median per-plant premium bar ${np.median(thr_used) if thr_used else float('nan'):,.0f}/MWh"
    )
    return ev, hours


# =====================================================================
# Driver
# =====================================================================
def run(markets=("DAM", "RTM"), con=None):
    """Build the geolocation, the price surfaces, and the screen for each market.

    Returns (events, hours, meta). Both frames carry a `market` column and are
    written to data/derived/ for the pipeline's bid-side stage to join on.
    """
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(SCRATCH, exist_ok=True)
    own = con is None
    if own:
        con = duckdb.connect()
        con.execute("PRAGMA disable_progress_bar")
        con.execute("PRAGMA threads=6")
        con.execute("PRAGMA memory_limit='4GB'")
        con.execute(f"PRAGMA temp_directory='{os.path.join(SCRATCH, 'duck_tmp')}'")

    log("geolocating outage resources...")
    geo_all, cov = build_geo(con)
    geo = geo_all[geo_all.geo_source != "unplaced"].reset_index(drop=True)
    seg = _outage_segments(con, geo.rid)
    log(f"  {len(seg):,} collapsed outage segments at placed resources")

    ev_frames, hr_frames, meta = [], [], {"geo_coverage": cov, "markets": {}}

    if "DAM" in markets:
        build_dam_surface(con)
        z = np.load(DAM_CACHE, allow_pickle=True)
        spine = pd.DatetimeIndex(z["spine"])
        ridx = {r: i for i, r in enumerate(geo.rid)}
        forced, _planned = _forced_depth(seg, ridx, len(spine), len(geo), spine[0])
        log("  day-ahead: building local price per plant...")
        local, system, _ = _local_price(z["grid"], z["lat"], z["lon"], geo, len(spine))
        ev, hrs = _screen("DAM", geo, local, system, forced, seg, spine, 1, DAM_PCTL, DAM_MIN_LMP)
        ev_frames.append(ev)
        hr_frames.append(hrs)
        meta["markets"]["DAM"] = _market_meta(ev, hrs, DAM_PCTL, DAM_MIN_LMP, len(geo))
        del local, system, forced, z

    if "RTM" in markets:
        build_rtm_surface(con)
        z = np.load(RTM_META, allow_pickle=True)
        grid = np.load(RTM_GRID, mmap_mode="r")
        spine = pd.DatetimeIndex(z["spine"])
        ridx = {r: i for i, r in enumerate(geo.rid)}
        forced, _planned = _forced_depth(seg, ridx, len(spine) // 12, len(geo), spine[0])
        log("  real-time: building local price per plant (streamed, resource-major)...")
        local, system, _ = _local_price(
            grid,
            z["lat"],
            z["lon"],
            geo,
            len(spine),
            resource_major_path=os.path.join(SCRATCH, "spike_rtm_local.npy"),
        )
        ev, hrs = _screen("RTM", geo, local, system, forced, seg, spine, 12, RTM_PCTL, RTM_MIN_LMP)
        ev_frames.append(ev)
        hr_frames.append(hrs)
        meta["markets"]["RTM"] = _market_meta(ev, hrs, RTM_PCTL, RTM_MIN_LMP, len(geo))
        del local, system, forced, grid, z

    events = pd.concat(ev_frames, ignore_index=True)
    hours = pd.concat(hr_frames, ignore_index=True)
    events.to_parquet(os.path.join(OUT, "local_spike_events.parquet"), index=False)
    hours.to_parquet(os.path.join(OUT, "local_spike_hours.parquet"), index=False)
    with open(os.path.join(OUT, "local_spike_meta.json"), "w") as f:
        json.dump(meta, f, indent=2, default=str)
    if own:
        con.close()
    return events, hours, meta


def _market_meta(ev, hrs, pctl, min_lmp, n_plants_screened):
    return {
        "n_events": int(len(ev)),
        "n_plants": int(ev.rid.nunique()) if len(ev) else 0,
        "n_plants_screened": int(n_plants_screened),
        "n_event_hours": int(hrs.is_event.sum()),
        "n_hours": int(len(hrs)),
        "pct_year": round(100 * float(hrs.is_event.mean()), 2),
        "prem_pctl": pctl,
        "min_lmp": min_lmp,
        "min_mw": MIN_MW,
        "min_jump": MIN_JUMP,
        "window_h": WINDOW_H,
        "pre_h": PRE_H,
        "near_k": NEAR_K,
        "near_km": NEAR_KM,
        "gap_h": GAP_H,
        "months": (ev.start_utc.dt.month.value_counts().sort_index().to_dict() if len(ev) else {}),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--market",
        choices=["dam", "rtm", "both"],
        default="both",
        help="which market to screen (day-ahead is cheap; real-time reads 51 GB)",
    )
    a = ap.parse_args()
    mk = {"dam": ("DAM",), "rtm": ("RTM",), "both": ("DAM", "RTM")}[a.market]
    run(markets=mk)
