#!/usr/bin/env python3
"""
CAISO Market Surveillance Dashboard
-----------------------------------
Interactive auditor tool over CAISO 2025 Real-Time Market bids + outages.

Screens:
  • Economic-withholding / bid-anomaly   (statistical peer benchmark)
  • Re-identification / fingerprinting     (anon RESOURCEBID_SEQ -> named resource)

Run:  streamlit run dashboard.py
(Data is pre-computed by pipeline.py into ./data/derived/)
"""

import json
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from auth import require_login
from plotly.subplots import make_subplots

HERE = os.path.dirname(os.path.abspath(__file__))
DER = os.path.join(HERE, "data", "derived")

# ---- validated dataviz palette (fixed categorical order; sequential blue) ----
CAT = [
    "#2a78d6",
    "#1baf7a",
    "#eda100",
    "#008300",
    "#4a3aa7",
    "#e34948",
    "#e87ba4",
    "#eb6834",
]
BLUE, AQUA, YELLOW, GREEN, VIOLET, RED, MAGENTA, ORANGE = CAT
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
STATUS = dict(good="#0ca30c", warning="#fab219", serious="#ec835a", critical="#d03b3b")
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, SURF = "#e1e0d9", "#fcfcfb"

# ---- plain-language names for CAISO market-product codes (legend labels) ----
PRODUCT_NAMES = {
    "EN": "Energy (EN)",
    "SR": "Spinning reserve (SR)",
    "NR": "Non-spinning reserve (NR)",
    "RU": "Regulation up (RU)",
    "RD": "Regulation down (RD)",
    "RMU": "Regulation mileage up (RMU)",
    "RMD": "Regulation mileage down (RMD)",
    "GHG": "GHG allowance cost (GHG)",
    "LFU": "Flexible ramp up (LFU)",
    "LFD": "Flexible ramp down (LFD)",
}


def product_label(code):
    return PRODUCT_NAMES.get(code, str(code))


st.set_page_config(
    page_title="CAISO Market Surveillance",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------- authentication ----------
# Blocks all rendering below until a valid shared username/password is entered.
# Credentials come from Streamlit secrets ([auth] section); see auth.py.
require_login()


# ---------- data loading ----------
# Cache key includes the file's mtime, so regenerating derived data (e.g. re-running
# pipeline.py) automatically busts the cache instead of serving a stale schema.
@st.cache_data(show_spinner=False)
def _read_parquet(path, _mtime):
    return pd.read_parquet(path)


def load(name):
    p = os.path.join(DER, name)
    return _read_parquet(p, os.path.getmtime(p))


@st.cache_data(show_spinner=False)
def _read_meta(path, _mtime):
    with open(path) as f:
        return json.load(f)


def load_meta():
    p = os.path.join(DER, "meta.json")
    return _read_meta(p, os.path.getmtime(p))


def have(name):
    return os.path.exists(os.path.join(DER, name))


def load_wh(basis="outage", market="RTM"):
    """Withholding results for one (market, basis), ranked. Tolerates older files
    that lack the `market`/`basis` columns."""
    df = load("withholding_resource.parquet")
    if "market" in df.columns:
        df = df[df["market"] == market]
    if "basis" in df.columns:
        df = df[df["basis"] == basis]
    return df.sort_values("withholding_index", ascending=False).reset_index(drop=True)


def load_wd(basis="outage", market="RTM"):
    df = load("withholding_daily.parquet")
    if "market" in df.columns:
        df = df[df["market"] == market]
    if "basis" in df.columns:
        df = df[df["basis"] == basis]
    return df


if not have("meta.json"):
    st.error("Derived data not found. Run `python pipeline.py` first.")
    st.stop()

META = load_meta()


def meta_num(key, *fallback_keys, default=0):
    """Read a numeric meta field, tolerating a meta.json older than this code.

    Deployments can briefly serve new code against a previously-checked-out meta.json,
    and anyone who pulls without re-running pipeline.py is in the same position. A
    hard META['new_key'] in the sidebar takes the WHOLE app down before a single page
    renders, so every field this file added must degrade instead of raising.
    """
    for k in (key, *fallback_keys):
        v = META.get(k)
        if isinstance(v, (int, float)):
            return v
    return default


# per-hub colors for the price screen (consistent across charts)
HUB_COLOR = {"SP15": "#2a78d6", "NP15": "#1baf7a", "ZP26": "#eda100", "SYS": "#0b0b0b"}
HUB_LABEL = {
    "SP15": "SP15 · Southern California",
    "NP15": "NP15 · Northern California",
    "ZP26": "ZP26 · Central California",
    "SYS": "System average",
}


# ---------- styling helpers ----------
def style(fig, height=360, legend=True, ytitle=None, xtitle=None):
    fig.update_layout(
        template="plotly_white",
        height=height,
        paper_bgcolor=SURF,
        plot_bgcolor=SURF,
        font=dict(family="system-ui,-apple-system,Segoe UI,sans-serif", color=INK2, size=13),
        margin=dict(l=10, r=16, t=30, b=10),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            x=0,
            title_text="",
            bgcolor="rgba(0,0,0,0)",
        )
        if legend
        else dict(),
        showlegend=legend,
        hoverlabel=dict(bgcolor="white", font_size=12),
    )
    fig.update_xaxes(
        showgrid=False,
        linecolor="#c3c2b7",
        ticks="outside",
        tickcolor=GRID,
        color=MUTED,
        title_text=xtitle or "",
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor=GRID,
        zeroline=False,
        color=MUTED,
        title_text=ytitle or "",
        title_font=dict(size=12),
    )
    return fig


def kpi(col, label, value, help=None, tone=None):
    color = STATUS.get(tone, INK)
    col.markdown(
        f"""<div style="background:{SURF};border:1px solid rgba(11,11,11,.08);
        border-radius:12px;padding:14px 16px;">
        <div style="font-size:12px;color:{MUTED};text-transform:uppercase;letter-spacing:.04em">{label}</div>
        <div style="font-size:26px;font-weight:700;color:{color};line-height:1.15;margin-top:4px">{value}</div>
        <div style="font-size:12px;color:{MUTED};margin-top:2px">{help or ""}</div></div>""",
        unsafe_allow_html=True,
    )


def section(title, subtitle=None):
    st.markdown(f"### {title}")
    if subtitle:
        st.caption(subtitle)


# ---------- sidebar ----------
st.sidebar.markdown("## ⚡ CAISO Market Watch")
st.sidebar.caption("Spotting two problems in California's wholesale electricity market")
PAGE = st.sidebar.radio(
    "View",
    [
        "Overview",
        "Prices · What power cost",
        "Demand · Who wanted power",
        "Screen 1 · Holding back power",
        "Screen 2 · Unmasking bidders",
        "Screen 3 · Look up one bidder",
        "How this works & caveats",
    ],
    label_visibility="collapsed",
)
st.sidebar.markdown("---")
st.sidebar.markdown("**What am I looking at?**")
st.sidebar.caption(
    "Power plants across California offer to sell electricity, hour by hour. "
    "Those offers are public but stripped of names. This tool reads 364 days "
    "of them and checks for two things: plants that may be **gaming prices**, and "
    "how easily the **anonymity can be undone**."
)
st.sidebar.markdown("---")
st.sidebar.metric(
    "Price steps analyzed", f"{meta_num('n_bid_rows_analyzed', 'n_bid_rows') / 1e6:.1f} million"
)
st.sidebar.metric("Anonymous bidders", f"{META['n_resources']:,}")
st.sidebar.caption(
    f"Period: {META.get('n_bid_days', 364)} days of {META['date_min'][:4]}  "
    f"({META['date_min']} → {META['date_max']}; 2025-03-09 absent from the source files)"
)

# =====================================================================
# PAGE 1 — MARKET OVERVIEW
# =====================================================================
if PAGE == "Overview":
    st.markdown("## Overview")
    st.caption(
        "A one-year snapshot of California's wholesale electricity market — the backdrop "
        "for the two problem-spotting screens in the sidebar."
    )

    st.info(
        "**New here?** California's grid operator, **CAISO**, runs a market where power plants "
        "offer to sell electricity hour by hour. When many plants break down at once, supply gets "
        "**tight** and prices can spike. The two screens in the sidebar look for plants that may "
        "exploit those tight moments (Screen 1), and test whether the data's anonymity can be "
        "reverse-engineered (Screen 2). "
        "👉 The **“How this works & caveats”** page (bottom of the sidebar) defines every term, "
        "lists the datasets with their statistics, and works each metric out with an example."
    )

    md = load("market_daily.parquet")
    md["day"] = pd.to_datetime(md["day"])
    wh = load_wh("outage")

    c = st.columns(5)
    kpi(
        c[0],
        "Price steps analyzed",
        f"{meta_num('n_bid_rows_analyzed', 'n_bid_rows') / 1e6:.1f} M",
        "individual (megawatt, price) steps inside hourly energy offers, 364 days",
    )
    kpi(
        c[1],
        "Anonymous bidders",
        f"{META['n_resources']:,}",
        "generators submitting priced energy offers",
    )
    flagged = int((wh["withholding_index"] > 0.05).sum())
    kpi(
        c[2],
        "Possible price-gaming",
        f"{flagged:,}",
        "plants flagged for a closer look",
        tone="serious" if flagged else None,
    )
    hc = META.get("reident_highconf_links", 0)
    kpi(
        c[3],
        "Anonymity cracked",
        f"{hc:,}",
        "bidders confidently named using forced outages alone "
        f"({META.get('reident_highconf_links_combined', 0)} with planned outages too, "
        f"{META.get('reident_highconf_links_magnitude', 0)} magnitude-aware)",
        tone="critical" if hc else None,
    )
    kpi(
        c[4],
        "Tightest moment",
        f"{md['peak_tight_mw'].max() / 1000:.1f} GW",
        "most capacity offline at once from forced (unplanned) outages — scheduled "
        "maintenance excluded (1 GW ≈ 750k homes)",
    )

    st.markdown("")
    left, right = st.columns([3, 2])

    with left:
        section(
            "How often power was offered at near-maximum prices",
            "The market caps prices at about \\$1,000 per megawatt-hour. This is the share of all "
            f"offered power priced within reach of that cap (≥ \\${META['thresholds']['nearcap_price']:.0f}). "
            "Spikes mean lots of capacity was parked at prices so high it was unlikely to actually be "
            "used — a pattern worth watching.",
        )
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=md["day"],
                y=md["near_share"] * 100,
                mode="lines",
                name="Priced near cap",
                line=dict(color=BLUE, width=2),
                fill="tozeroy",
                fillcolor="rgba(42,120,214,0.10)",
                hovertemplate="%{x|%b %d}<br>Priced near the cap: %{y:.1f}% of offered power<extra></extra>",
            )
        )
        style(fig, height=340, legend=False, ytitle="% of offered power")
        st.plotly_chart(fig, use_container_width=True)

    with right:
        section(
            "How short the grid got each day",
            "When plants break down unexpectedly ('forced outages'), supply tightens. This tracks "
            "the most capacity offline at any point each day — our stand-in for grid stress.",
        )
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=md["day"],
                y=md["peak_tight_mw"] / 1000,
                mode="lines",
                name="Peak offline GW",
                line=dict(color=ORANGE, width=2),
                fill="tozeroy",
                fillcolor="rgba(235,104,52,0.10)",
                hovertemplate="%{x|%b %d}<br>Most offline that day: %{y:.2f} GW<extra></extra>",
            )
        )
        thr = META["thresholds"]["tight_mw"] / 1000
        fig.add_hline(
            y=thr,
            line=dict(color=STATUS["serious"], width=1, dash="dot"),
            annotation_text=f"'grid is short' line = {thr:.1f} GW (top 10% of hours)",
            annotation_font_color=MUTED,
            annotation_font_size=11,
        )
        style(fig, height=340, legend=False, ytitle="GW of capacity offline")
        st.plotly_chart(fig, use_container_width=True)

    section(
        "What kinds of offers were submitted, month by month",
        "'Energy' is plain electricity and dominates. Most of the rest are backup and grid-stability "
        "services (called ancillary services) — reserves kept on standby in case a plant suddenly "
        "fails. 'GHG' is different: the greenhouse-gas allowance cost bid on power imported into "
        "California. Bars count individual price steps, not whole offers, and products differ in how "
        "many steps they use — so compare each product's shape over time rather than the bar heights "
        "against each other.",
    )
    pm = load("product_monthly.parquet")
    pm["month"] = pd.to_datetime(pm["month"])
    order = pm.groupby("product")["n_bids"].sum().sort_values(ascending=False).index.tolist()
    fig = go.Figure()
    for i, prod in enumerate(order[:8]):
        d = pm[pm["product"] == prod]
        fig.add_trace(
            go.Bar(
                x=d["month"],
                y=d["n_bids"],
                name=product_label(prod),
                marker_color=CAT[i % 8],
                hovertemplate=f"{product_label(prod)}<br>%{{x|%b %Y}}: %{{y:,}} price steps<extra></extra>",
            )
        )
    fig.update_layout(barmode="stack", bargap=0.25)
    style(fig, height=340, ytitle="price steps per month")
    st.plotly_chart(fig, use_container_width=True)

# =====================================================================
# PAGE 1b — PRICES / MARKET CONDITIONS
# =====================================================================
elif PAGE == "Prices · What power cost":
    st.markdown("## What power actually cost")
    st.caption(
        "The real price of electricity across California in 2025, at the three CAISO "
        "**trading hubs** — in both markets: the **day-ahead** price set the day before, and "
        "the **real-time** price the grid actually settled at every 5 minutes. Their gap is the "
        "market's own scarcity signal — and each now powers a version of Screen 1 (see the toggle there)."
    )

    if not have("price_hourly.parquet"):
        st.warning("No price data available. Re-run `python pipeline.py`.")
        st.stop()

    with st.expander("What is a 'locational price' (LMP)?  — in plain terms", expanded=False):
        st.markdown("""
The grid operator sets a separate electricity price at every point on the network — a **Locational Marginal Price (LMP)**, in dollars per megawatt-hour. Each hub price is really three parts added together:

- **Energy** — the base cost of generating one more megawatt-hour, the same across the whole system.
- **Congestion** — an add-on (or discount) when the transmission lines to that area are full. This is what makes Northern and Southern California prices diverge.
- **Losses** — a small adjustment for power lost as heat over the wires (usually slightly negative).

There are two prices for every hour. The **day-ahead (DAM)** price is set the afternoon before, one value per hour. The **real-time (RTM)** price is set every **5 minutes** as the grid actually balances — so it spikes far higher and dips far lower than the smooth day-ahead value. We track three regional **hubs** — **SP15** (south), **NP15** (north), **ZP26** (central) — plus their average as a single "system" price. When a system price spikes into its top 10% of hours, we call it **price-scarce** — the price-based cousin of the outage-based "grid is short" signal.
""")

    p = META.get("price", {})
    thr = META["thresholds"]

    market_label = st.radio(
        "Market",
        ["Day-ahead (DAM)", "Real-time (RTM)"],
        horizontal=True,
        help="Day-ahead is the price locked in the day before (one value per hour). "
        "Real-time is what the grid actually settled at, every 5 minutes — far spikier. "
        "The 'day-ahead vs real-time' section below always shows both.",
    )
    mkt = "DAM" if market_label.startswith("Day") else "RTM"
    # market-specific headline stats (RTM keys mirror the DAM ones in meta)
    stat = (
        dict(
            median=p.get("median_sys_lmp", 0),
            peak=p.get("peak_sys_lmp", 0),
            neg=p.get("neg_price_hours", 0),
            line=thr.get("price_tight_lmp", 0),
        )
        if mkt == "DAM"
        else dict(
            median=p.get("rtm_median_sys_lmp", 0),
            peak=p.get("rtm_peak_sys_lmp", 0),
            neg=p.get("rtm_neg_price_hours", 0),
            line=thr.get("price_tight_lmp_rtm", 0),
        )
    )
    c = st.columns(4)
    kpi(
        c[0],
        "Typical power price",
        f"${stat['median']:.0f}/MWh",
        f"median system {market_label.split()[0].lower()} price across the year",
    )
    kpi(
        c[1],
        "Highest hour",
        f"${stat['peak']:.0f}/MWh",
        "priciest system hour in 2025 (real-time peaks dwarf day-ahead)"
        if mkt == "RTM"
        else "priciest system hour in 2025",
        tone="serious",
    )
    kpi(
        c[2],
        "Negative-price hours",
        f"{stat['neg']:,}",
        "hours power was so plentiful the price went below zero",
    )
    kpi(
        c[3],
        "'Price-scarce' line",
        f"${stat['line']:.0f}/MWh",
        f"top {int((1 - thr.get('price_tight_percentile', 0.9)) * 100)}% of hours by {mkt} price",
    )

    pd_daily = load("price_daily.parquet")
    pd_daily["day"] = pd.to_datetime(pd_daily["day"])
    ph = load("price_hourly.parquet")
    ph["h"] = pd.to_datetime(ph["h"])
    # views below follow the market toggle; the DAM-vs-RTM section uses both
    pdm = pd_daily[pd_daily["market"] == mkt]
    phm = ph[ph["market"] == mkt]

    left, right = st.columns([3, 2])
    with left:
        section(
            "Daily average price, region by region",
            "Where the three hubs pull apart, transmission congestion is splitting the state into "
            "cheaper and pricier zones.",
        )
        fig = go.Figure()
        for hub in ["SP15", "NP15", "ZP26"]:
            d = pdm[pdm["hub"] == hub]
            fig.add_trace(
                go.Scatter(
                    x=d["day"],
                    y=d["avg_lmp"],
                    mode="lines",
                    name=HUB_LABEL[hub],
                    line=dict(color=HUB_COLOR[hub], width=1.6),
                    hovertemplate=f"{hub}<br>%{{x|%b %d}}: $%{{y:.0f}}/MWh<extra></extra>",
                )
            )
        style(fig, height=360, ytitle="$/MWh (daily average)")
        st.plotly_chart(fig, use_container_width=True)

    with right:
        hubsel = st.selectbox(
            "Price-duration curve for…",
            ["SYS", "SP15", "NP15", "ZP26"],
            format_func=lambda h: HUB_LABEL[h],
        )
        section(
            "How often price was high",
            "Every hour in the dataset, sorted priciest-first. The steep tail on the left is "
            "scarcity; the dip below zero on the right is oversupply.",
        )
        s = phm[phm["hub"] == hubsel]["lmp"].sort_values(ascending=False).reset_index(drop=True)
        pctile = (s.index + 1) / len(s) * 100 if len(s) else s.index
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=pctile,
                y=s,
                mode="lines",
                name=hubsel,
                line=dict(color=HUB_COLOR[hubsel], width=2),
                fill="tozeroy",
                fillcolor="rgba(42,120,214,0.10)",
                hovertemplate="%{x:.0f}% of hours were at or above $%{y:.0f}/MWh<extra></extra>",
            )
        )
        fig.add_hline(
            y=stat["line"],
            line=dict(color=STATUS["serious"], width=1, dash="dot"),
            annotation_text=f"price-scarce line ${stat['line']:.0f}",
            annotation_font_color=MUTED,
            annotation_font_size=11,
        )
        style(fig, height=360, legend=False, xtitle="share of hours (%)", ytitle="$/MWh")
        st.plotly_chart(fig, use_container_width=True)

    section(
        "What makes up the price, month by month",
        "The system price split into its three parts. Energy dominates; the small congestion and "
        "loss pieces are what make one region differ from another.",
    )
    phs = phm[phm["hub"] == "SYS"].copy()
    phs["month"] = phs["h"].dt.to_period("M").dt.to_timestamp()
    comp = phs.groupby("month")[["energy", "congestion", "loss"]].mean().reset_index()
    fig = go.Figure()
    for name, col, color in [
        ("Energy", "energy", BLUE),
        ("Congestion", "congestion", ORANGE),
        ("Losses", "loss", AQUA),
    ]:
        fig.add_trace(
            go.Bar(
                x=comp["month"],
                y=comp[col],
                name=name,
                marker_color=color,
                hovertemplate=f"{name}<br>%{{x|%b %Y}}: $%{{y:.1f}}/MWh<extra></extra>",
            )
        )
    fig.update_layout(barmode="relative", bargap=0.3)
    style(fig, height=320, ytitle="$/MWh (monthly average)")
    st.plotly_chart(fig, use_container_width=True)

    # -----------------------------------------------------------------
    # NEW: day-ahead vs real-time — the spread
    # -----------------------------------------------------------------
    section(
        "Day-ahead vs real-time: the spread",
        "The day-ahead price is a forecast locked in the afternoon before; the real-time price is "
        "what the grid actually settled at. When real-time runs far above day-ahead, the grid was "
        "tighter than the market expected — the classic signal that supply was scarce (or withheld).",
    )
    sys_dam = pd_daily[(pd_daily["market"] == "DAM") & (pd_daily["hub"] == "SYS")][
        ["day", "avg_lmp"]
    ].rename(columns={"avg_lmp": "DAM"})
    sys_rtm = pd_daily[(pd_daily["market"] == "RTM") & (pd_daily["hub"] == "SYS")][
        ["day", "avg_lmp"]
    ].rename(columns={"avg_lmp": "RTM"})
    sp = sys_dam.merge(sys_rtm, on="day", how="inner")
    sp["spread"] = sp["RTM"] - sp["DAM"]

    sc = st.columns(3)
    kpi(
        sc[0],
        "Real-time vs day-ahead",
        f"{p.get('spread_mean', 0):+.1f} $/MWh",
        "average gap (real-time minus day-ahead) across the year — near zero when the "
        "forecast is good",
    )
    kpi(
        sc[1],
        "Hour-to-hour swing",
        f"±${p.get('spread_sd', 0):.0f}/MWh",
        "standard deviation of the hourly real-time-minus-day-ahead gap (the chart below "
        "averages those hours into days, which smooths the swing considerably)",
    )
    kpi(
        sc[2],
        "Biggest single-hour gap",
        f"+${p.get('spread_max', 0):.0f}/MWh",
        "the hour real-time most exceeded day-ahead — a real-time scarcity spike the "
        "day-ahead price never saw",
        tone="serious",
    )

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.62, 0.38],
        vertical_spacing=0.07,
        subplot_titles=(
            "Daily average system price — both markets",
            "Daily mean spread (real-time minus day-ahead)",
        ),
    )
    fig.add_trace(
        go.Scatter(
            x=sp["day"],
            y=sp["DAM"],
            mode="lines",
            name="Day-ahead (DAM)",
            line=dict(color=BLUE, width=1.6),
            hovertemplate="Day-ahead<br>%{x|%b %d}: $%{y:.0f}/MWh<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=sp["day"],
            y=sp["RTM"],
            mode="lines",
            name="Real-time (RTM)",
            line=dict(color=RED, width=1.6),
            hovertemplate="Real-time<br>%{x|%b %d}: $%{y:.0f}/MWh<extra></extra>",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Bar(
            x=sp["day"],
            y=sp["spread"],
            name="Spread",
            showlegend=False,
            marker_color=[STATUS["serious"] if v >= 0 else BLUE for v in sp["spread"]],
            hovertemplate="%{x|%b %d}: %{y:+.0f} $/MWh<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_hline(y=0, line=dict(color=MUTED, width=1), row=2, col=1)
    style(fig, height=460)
    fig.update_yaxes(title_text="$/MWh", title_font=dict(size=12), row=1, col=1)
    fig.update_yaxes(title_text="spread $/MWh", title_font=dict(size=12), row=2, col=1)
    for ann in fig.layout.annotations:
        ann.font.size = 12
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Orange bars = real-time ran richer than day-ahead (grid tighter than forecast); "
        "blue bars = real-time came in cheaper (oversupply). The tallest orange spikes are the "
        "days worth pairing against the withholding screen."
    )

    # -----------------------------------------------------------------
    # NEW: intraday volatility (5-minute real-time)
    # -----------------------------------------------------------------
    if have("rtm_5min.parquet"):
        section(
            "Inside a single day: the 5-minute real-time price",
            "The day-ahead price is one flat step per hour. Real-time re-prices every 5 minutes — so "
            "a calm day-ahead forecast can hide violent intraday swings. Pick one of the year's most "
            "volatile days to see the gap.",
        )
        r5 = load("rtm_5min.parquet")
        r5["ts"] = pd.to_datetime(r5["ts"])
        r5sys = r5[r5["hub"] == "SYS"].copy()
        r5sys["d"] = r5sys["ts"].dt.date
        rng = (
            r5sys.groupby("d")["lmp"].agg(lambda s: s.max() - s.min()).sort_values(ascending=False)
        )
        top_days = list(rng.head(10).index)
        daysel = st.selectbox(
            "Volatile day to inspect",
            top_days,
            format_func=lambda d: f"{d:%b %d}  —  swing ${rng[d]:.0f}/MWh",
        )
        oneday = r5sys[r5sys["d"] == daysel].sort_values("ts")
        dam_day = ph[
            (ph["market"] == "DAM") & (ph["hub"] == "SYS") & (ph["h"].dt.date == daysel)
        ].sort_values("h")
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=oneday["ts"],
                y=oneday["lmp"],
                mode="lines",
                name="Real-time (5-min)",
                line=dict(color=RED, width=1.6, shape="hv"),
                hovertemplate="Real-time<br>%{x|%H:%M}: $%{y:.0f}/MWh<extra></extra>",
            )
        )
        if len(dam_day):
            fig.add_trace(
                go.Scatter(
                    x=dam_day["h"],
                    y=dam_day["lmp"],
                    mode="lines",
                    name="Day-ahead (hourly)",
                    line=dict(color=BLUE, width=1.6, dash="dot", shape="hv"),
                    hovertemplate="Day-ahead<br>%{x|%H:%M}: $%{y:.0f}/MWh<extra></extra>",
                )
            )
        style(fig, height=340, ytitle="$/MWh")
        st.plotly_chart(fig, use_container_width=True)
        _hourly_swing = (
            oneday.assign(_h=oneday["ts"].dt.floor("h"))
            .groupby("_h")["lmp"]
            .agg(lambda x: x.max() - x.min())
            .median()
        )
        st.caption(
            f"On this day the real-time price spans **\\${rng[daysel]:,.0f}**/MWh between its "
            f"cheapest and priciest five-minute interval, and the typical single hour moves "
            f"**\\${_hourly_swing:,.0f}**/MWh within itself — swings the once-a-day day-ahead price "
            "cannot represent. (The day list above is ranked by that daily span; only the very "
            "top days exceed \\$1,000.)"
        )

    with st.expander("The 25 priciest hours of the year (and download all prices)"):
        top = (
            phm[phm["hub"] == "SYS"]
            .nlargest(25, "lmp")[["h", "lmp", "energy", "congestion", "loss"]]
            .copy()
        )
        top = top.rename(
            columns={
                "h": "Hour",
                "lmp": "System $/MWh",
                "energy": "Energy",
                "congestion": "Congestion",
                "loss": "Losses",
            }
        )
        st.dataframe(top, width="stretch", height=320, hide_index=True)
        st.caption(
            f"Hourly hub prices — SP15, NP15, ZP26 and the system average — "
            f"{ph['h'].min():%b %d %Y} to {ph['h'].max():%b %d %Y}, both markets, one row per "
            "market/hub/hour. 2025-03-09 is absent from the day-ahead source file."
        )
        st.download_button(
            "⬇ Download hourly prices (CSV)",
            # `peak` is dropped: for the RTM system row it is a max ACROSS hubs of each hub's
            # own 5-minute high, which is not a system price and no view here plots it.
            ph.drop(columns=["peak"]).to_csv(index=False),
            "caiso_hub_lmp.csv",
            "text/csv",
        )

# =====================================================================
# PAGE — DEMAND SIDE (who wanted power, vs how thin supply was)
# =====================================================================
elif PAGE == "Demand · Who wanted power":
    st.markdown("## Who wanted power — and when supply ran thin")
    st.caption(
        "The other half of the market: the **demand** side. Buyers (utilities and load-serving "
        "entities) bid to *purchase* power hour by hour. This panel tracks how much demand was "
        "bid across 2025, and lays it against how short **supply** got — the two forces that set "
        "the price."
    )

    if not have("demand_daily.parquet"):
        st.warning("No demand data available. Re-run `python pipeline.py`.")
        st.stop()

    market_label = st.radio(
        "Market",
        ["Day-ahead (DAM)", "Real-time (RTM)"],
        horizontal=True,
        help="Demand is set in the day-ahead market. Only a handful of load resources re-bid in "
        "real time, and the real-time file carries no self-schedule field at all — so the "
        "must-take band there is structurally empty rather than measured.",
    )
    market = "DAM" if market_label.startswith("Day-ahead") else "RTM"

    with st.expander("How to read this panel — in plain terms", expanded=False):
        st.markdown("""
Every hour, buyers submit **demand bids** to CAISO — how many megawatts they want and the most they'll pay. Those bids come in two flavours:

- **Self-scheduled (must-take)** — a fixed quantity the buyer wants *regardless of price*. Price-insensitive load.
- **Bid as a price curve (economic)** — megawatts submitted with a willingness-to-pay attached. In principle this is the demand that can flex; in practice about **95%** of these megawatts are bid flat at or above **\$1,000/MWh**, far above any price seen in 2025, so they behave as must-take too.

We add both up across every buyer to get the system's **demand bid** each hour, then chart each day's **peak** (the tightest demand hour) across the year.

Hours come from each bid's own interval fields. A self-scheduled row stamps its 24-hour window in `STARTTIME` and the actual hour in `TIMEINTERVALSTART`, so keying on the former would pile a whole day's must-take demand into hour 00.

On the same timeline we overlay **supply tightness** — the most plant capacity forced offline at once that day (the same scarcity signal the screens use). Where a high demand day meets a thin-supply day is where prices are most likely to move.
""")

    dd = load("demand_daily.parquet")
    dd = dd[dd["market"] == market].copy()
    dd["day"] = pd.to_datetime(dd["day"])
    dd = dd.sort_values("day")

    md = load("market_daily.parquet")
    md["day"] = pd.to_datetime(md["day"])

    if market == "RTM":
        st.info(
            "**Demand is a day-ahead activity.** In real time, load is essentially carried over "
            "from the day-ahead schedule and barely re-bid — so real-time demand bids average only "
            f"~{dd['demand_avg_mw'].mean() / 1000:.1f} GW across the hours that had any bid at "
            f"all, versus ~{META.get('demand', {}).get('dam_avg_mw', 0) / 1000:.1f} GW day-ahead. "
            "The real-time figure also contains no self-schedule component, because that field is "
            "unpopulated in the real-time file. This view is shown for completeness; the day-ahead "
            "market is where demand really lives."
        )

    peak_dem = dd["demand_peak_mw"].max()
    avg_dem = dd["demand_avg_mw"].mean()
    mt_share = (
        dd["self_avg_mw"].mean() / dd["demand_avg_mw"].mean() * 100
        if dd["demand_avg_mw"].mean()
        else 0
    )
    peak_tight = md["peak_tight_mw"].max() / 1000

    c = st.columns(4)
    kpi(
        c[0],
        "Peak demand bid",
        f"{peak_dem / 1000:.1f} GW",
        "most demand bid in a single hour",
    )
    kpi(
        c[1],
        "Typical demand bid",
        f"{avg_dem / 1000:.1f} GW",
        (
            f"average across the {META.get('n_bid_hours', 8736):,} day-ahead hours in the data"
            if market == "DAM"
            else "average across the real-time hours in which any load actually re-bid — "
            "not every hour of the year"
        ),
    )
    kpi(
        c[2],
        "Must-take share",
        f"{mt_share:.0f}%",
        "share of demand MW submitted as a fixed self-schedule rather than as a price curve",
    )
    kpi(
        c[3],
        "Thinnest supply",
        f"{peak_tight:.1f} GW",
        "most capacity forced offline at once — unplanned outages only (1 GW ≈ 750k homes)",
    )

    st.markdown("")
    section(
        "Demand bid vs. how thin supply got",
        "Blue is each day's **peak demand bid** (left axis). Orange is that day's **supply "
        "tightness** — the most capacity forced offline at once (right axis). When a tall blue "
        "day lines up with a tall orange day, buyers were competing for the scarcest supply.",
    )
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Scatter(
            x=dd["day"],
            y=dd["demand_peak_mw"] / 1000,
            mode="lines",
            name="Peak demand bid",
            line=dict(color=BLUE, width=2),
            fill="tozeroy",
            fillcolor="rgba(42,120,214,0.10)",
            hovertemplate="%{x|%b %d}<br>Peak demand bid: %{y:.1f} GW<extra></extra>",
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=md["day"],
            y=md["peak_tight_mw"] / 1000,
            mode="lines",
            name="Supply offline (tightness)",
            line=dict(color=ORANGE, width=1.6),
            hovertemplate="%{x|%b %d}<br>Capacity offline: %{y:.1f} GW<extra></extra>",
        ),
        secondary_y=True,
    )
    style(fig, height=380)
    fig.update_yaxes(title_text="Peak demand bid (GW)", secondary_y=False)
    fig.update_yaxes(
        title_text="Supply offline (GW)",
        secondary_y=True,
        showgrid=False,
        color=MUTED,
        title_font=dict(size=12),
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("")
    left, right = st.columns([3, 2])
    with left:
        section(
            "What kind of demand was it?",
            "Splitting each day's average demand into **self-scheduled** (a fixed quantity bought "
            "at any price) and **bid as a price curve**. Note that roughly 95% of the price-curve "
            "megawatts are bid flat at or above \\$1,000/MWh — above every price seen in 2025 — so "
            "they are price-insensitive in practice. Genuinely sloped, flexible demand is a small "
            "fraction of even the green band.",
        )
        fig2 = go.Figure()
        fig2.add_trace(
            go.Scatter(
                x=dd["day"],
                y=dd["self_avg_mw"] / 1000,
                mode="lines",
                name="Must-take (self-scheduled)",
                line=dict(color=MUTED, width=0),
                stackgroup="d",
                fillcolor="rgba(137,135,129,0.45)",
                hovertemplate="%{x|%b %d}<br>Must-take: %{y:.2f} GW<extra></extra>",
            )
        )
        fig2.add_trace(
            go.Scatter(
                x=dd["day"],
                y=dd["econ_avg_mw"] / 1000,
                mode="lines",
                name="Bid as a price curve (economic)",
                line=dict(color=AQUA, width=0),
                stackgroup="d",
                fillcolor="rgba(27,175,122,0.55)",
                hovertemplate="%{x|%b %d}<br>Price curve: %{y:.2f} GW<extra></extra>",
            )
        )
        style(fig2, height=340, ytitle="GW (daily average)")
        st.plotly_chart(fig2, use_container_width=True)
    with right:
        section(
            "The takeaway",
            None,
        )
        if market == "DAM":
            st.markdown(
                f"""
Across the year, **{mt_share:.0f}%** of day-ahead demand arrived as a fixed **self-schedule** — a
quantity the buyer takes no matter the price. The other **{100 - mt_share:.0f}%** arrived as a
**price curve**, and about 95% of *that* is bid flat at or above \\$1,000/MWh, so it never actually
flexed in 2025 either.

Because so little demand can genuinely ease off when supply is short, the market leans hard on the
**supply** side to keep prices in check — which is exactly why the withholding screens focus there.
"""
            )
        else:
            st.markdown(
                """
The real-time load file carries no self-schedule field, so the must-take / price-curve split
cannot be computed for this market. Read the day-ahead view for the demand mix.
"""
            )
        st.download_button(
            "⬇ Download daily demand (CSV)",
            dd.to_csv(index=False),
            f"caiso_demand_daily_{market.lower()}_2025.csv",
            "text/csv",
        )

# =====================================================================
# PAGE 2 — ECONOMIC WITHHOLDING
# =====================================================================
elif PAGE == "Screen 1 · Holding back power":
    st.markdown("## Screen 1 · Are any plants holding back power?")
    st.caption(
        "A plant 'holds back' by pricing its electricity so high it won't be used — shrinking "
        "supply to push prices up. This screen flags plants that do this **more when the grid is "
        "already short**, which is the tell-tale sign. (Analysts call this *economic withholding*.) "
        "It needs no secret cost data."
    )

    t = META["thresholds"]
    mkt_col, basis_col = st.columns(2)
    has_dam = "DAM" in META.get("markets", ["RTM"])
    with mkt_col:
        market_label = st.radio(
            "Bid market",
            ["Real-time (RTM)", "Day-ahead (DAM)"] if has_dam else ["Real-time (RTM)"],
            index=0,
            horizontal=True,
            help="RTM = real-time-market offers (the original screen). DAM = day-ahead offers, "
            "the SAME market as the LMP price data — so the day-ahead view can also test offers "
            "against the ACTUAL clearing price (see the impact panel below).",
        )
    market = "DAM" if market_label.startswith("Day-ahead") else "RTM"
    with basis_col:
        _basis_opts = {
            "Outages (capacity offline)": "outage",
            "Day-ahead price spikes": "price",
            "Real-time price spikes": "price_rtm",
        }
        # keys look like "RTM_outage" / "DAM_price_rtm" — basis is everything after
        # the market prefix. Only offer bases actually present in the derived data.
        _scored_bases = {k.split("_", 1)[1] for k in META.get("withholding_scored_by", {})}
        _basis_labels = [
            lbl for lbl, b in _basis_opts.items() if not _scored_bases or b in _scored_bases
        ]
        basis_label = st.radio(
            "Measure 'when the grid is short' by…",
            _basis_labels,
            index=0,
            horizontal=True,
            help="Outages: hours with the most capacity on a forced/unplanned outage — planned "
            "maintenance excluded (needs no price data). "
            "Day-ahead price spikes: top 10% of hours by the day-ahead market price. "
            "Real-time price spikes: top 10% of hours by the 5-minute real-time price — catches "
            "intra-hour scarcity the day-ahead price flattens. Scores differ because each defines "
            "'short' differently.",
        )
    basis = _basis_opts[basis_label]

    with st.expander("How this screen works — in plain terms", expanded=False):
        n_scored = META.get("withholding_scored_by", {}).get(
            f"{market}_{basis}",
            META.get(f"withholding_scored_{basis}", META.get("withholding_scored", 0)),
        )
        mkt_note = (
            "You're viewing the **day-ahead (DAM)** market — the same market as the price data, so "
            "the impact panel below also scores offers against the **actual clearing price**."
            if market == "DAM"
            else "You're viewing the **real-time (RTM)** market (the original screen)."
        )
        if basis == "price":
            short_def = (
                f"the {int((1 - t.get('price_tight_percentile', 0.9)) * 100)}% of hours with the "
                f"**highest day-ahead price** (system price at or above **\\${t.get('price_tight_lmp', 0):.0f}/MWh**). "
                "This is the market's own scarcity signal — see the **Prices** screen."
            )
        elif basis == "price_rtm":
            short_def = (
                f"the {int((1 - t.get('price_tight_percentile', 0.9)) * 100)}% of hours with the "
                f"**highest real-time price** (5-minute system price averaged to the hour at or above "
                f"**\\${t.get('price_tight_lmp_rtm', 0):.0f}/MWh**). Real-time captures intra-hour scarcity "
                "spikes the day-ahead price flattens — see the **Prices** screen."
            )
        else:
            short_def = (
                f"the {int((1 - t['tight_percentile']) * 100)}% of hours with the most capacity on a "
                f"**forced (unplanned)** outage — at least **{t['tight_mw']:,.0f} MW** of unexpected "
                "curtailment. Scheduled maintenance is excluded, because it is not unexpected "
                "scarcity. This is a stand-in for scarcity that needs no price data at all."
            )
        # How many plants in this market are flagged under MORE than one scarcity basis?
        # Computed live, because the copy below makes a claim about it.
        _flag = {}
        for _b in _scored_bases:
            _w = load_wh(_b, market)
            if len(_w):
                _flag[_b] = set(_w[_w["withholding_index"] > 0.05]["res"])
        _multi_basis = len(
            {r for a in _flag for b in _flag if a < b for r in (_flag[a] & _flag[b])}
        )
        # The outage basis is strongly seasonal: its short hours cluster in the second half of
        # the year, so the "normal" baseline is partly a different season. Quantified live.
        _season_caveat = ""
        if basis == "outage" and have("market_hourly.parquet"):
            _mh = load("market_hourly.parquet")
            _tm = pd.to_datetime(_mh[_mh["is_tight"]]["h"]).dt.month
            _late = 100 * _tm.isin([9, 10, 11, 12]).mean()
            _early = int(_tm.isin([1, 2, 3, 4]).sum())
            _season_caveat = (
                "- **Caveat on this basis.** Forced outages cluster late in the year: "
                f"{_late:.0f}% of the short hours fall in September–December and {_early} fall "
                "in January–April. The 'normal' comparison is therefore partly a *different "
                "season*, so a plant that simply offers higher in Q4 can score positive. "
                "Cross-check any outage-basis lead against the two price bases.\n"
            )
        # median energy offer price for the market on screen, read from the dataset cards
        _ds_key = "dam_bids" if market == "DAM" else "rtm_bids"
        _med_offer = next(
            (
                v.replace("$", "").replace("/MWh", "")
                for d in META.get("datasets", [])
                if d.get("key") == _ds_key
                for k, v in d.get("stats", [])
                if k.startswith("Typical energy offer price")
            ),
            "35",
        )
        st.markdown(f"""
{mkt_note}

The idea: a plant gaming the market will price its power very high **especially when the grid is short**, because that's when the tactic pays off. So we compare each plant against *itself* — its behavior in short hours vs. normal hours.

- **"Grid is short" hours** = {short_def}
- For each plant we measure the share of its offered power priced steeply high — **≥ \\${t["elevated_price"]:.0f} per megawatt-hour** — separately during short hours and normal hours. (For context, the median energy *offer* price in this market is about \\${_med_offer}/MWh.)
- **Withholding score = (high-priced share when short) − (high-priced share when normal).** A big positive number means the plant moves more of its capacity to prices unlikely to clear precisely when the grid is short. Whether that could actually move the market price also depends on the plant's size and pivotality, which this screen does not test — some flagged plants are very small.
- *Worked example:* a plant prices **20%** of its power steeply high in normal hours but **60%** when the grid is short → score = 0.60 − 0.20 = **0.40**. A plant that behaves the same either way scores near 0.
- We only score plants active in at least {t["min_tight_hours"]} short hours, so a single unusual hour cannot drive the score ({n_scored:,} plants qualify on this basis). There is no statistical significance test — treat a high score on few hours with care.
- **Important:** this flags *suspicious behavior*, not proven wrongdoing — a lead, not a verdict. The "short" definitions are complementary, and {_multi_basis} plants in this market are flagged under more than one — those are the strongest leads.
{_season_caveat}""")

    wh = load_wh(basis, market)
    wd = load_wd(basis, market)
    wd["day"] = pd.to_datetime(wd["day"])

    if len(wh) == 0:
        st.info("No plants qualified for this market/basis combination.")
        st.stop()

    # DAM-only: the real clearing-price impact test (offers above the actual price)
    if market == "DAM" and "impact_index" in wh.columns and wh["impact_index"].notna().any():
        section(
            "Real clearing-price impact test",
            "Because day-ahead offers and day-ahead prices are the same market, we can go beyond the "
            f"fixed \\${t['elevated_price']:.0f} cutoff and measure capacity each plant offered **above "
            "the price that actually cleared** that hour — capacity it effectively withheld from the "
            "day-ahead solution — and whether it did so **more when the grid was short**.",
        )
        imp = wh.sort_values("impact_index", ascending=False).reset_index(drop=True)
        ic = st.columns(3)
        kpi(
            ic[0],
            "Withheld above clearing — highest-impact plant",
            f"{imp.iloc[0]['withheld_mwh_tight']:,.0f} MWh",
            f"in short hours, plant #{int(imp.iloc[0]['res'])}",
            tone="critical",
        )
        kpi(
            ic[1],
            "Above-clearing when short vs normal",
            f"+{imp.iloc[0]['impact_index'] * 100:.0f} pts",
            "top plant's shift toward pricing above clearing when short",
            tone="serious",
        )
        kpi(
            ic[2],
            "Plants shifting more capacity above clearing when short",
            f"{int((wh['impact_index'] > 0.05).sum()):,}",
            "impact index above 0.05 — i.e. the shift between short and normal hours, not "
            "simply pricing above clearing at all",
        )
        top_i = imp.head(15).iloc[::-1]
        fig = go.Figure(
            go.Bar(
                x=top_i["impact_index"],
                y=top_i["res"].astype(str),
                orientation="h",
                marker_color=VIOLET,
                marker_line_width=0,
                customdata=top_i[["withheld_mwh_tight", "withholding_index"]].values,
                hovertemplate="Plant #%{y}<br>Impact index (above clearing, short − normal): %{x:.3f}"
                "<br>Withheld above clearing in short hours: %{customdata[0]:,.0f} MWh"
                "<br>(fixed-$ withholding score: %{customdata[1]:.3f})<extra></extra>",
            )
        )
        fig.update_yaxes(type="category", title_text="anonymous plant ID")
        style(fig, height=380, legend=False, xtitle="clearing-price impact index")
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Generators only (day-ahead demand and intertie bids excluded), measured at the system/hub "
            "clearing price (bids carry no node ID). Offering above the clearing price isn't proof of "
            "gaming — but doing it **disproportionately when scarce** is the behavioral signature, now "
            "measured against the real price instead of a fixed cutoff."
        )
        st.markdown("---")

    flagged = int((wh["withholding_index"] > 0.05).sum())
    c = st.columns(4)
    kpi(
        c[0],
        "Plants analyzed",
        f"{len(wh):,}",
        "had enough short-hour activity to score",
    )
    kpi(c[1], "Flagged for review", f"{flagged:,}", "score above 0.05", tone="serious")
    kpi(
        c[2],
        "Strong signals",
        f"{int((wh['withholding_index'] > 0.20).sum()):,}",
        "score above 0.20",
        tone="critical",
    )
    kpi(
        c[3],
        "Highest score",
        f"{wh['withholding_index'].max():.2f}",
        f"anonymous plant #{int(wh.iloc[0]['res'])}",
    )

    left, right = st.columns(2)
    with left:
        section(
            "The 20 most suspicious plants",
            "Ranked by withholding score — higher means the plant prices high more specifically when the grid is short.",
        )
        top = wh.head(20).iloc[::-1]
        fig = go.Figure(
            go.Bar(
                x=top["withholding_index"],
                y=top["res"].astype(str),
                orientation="h",
                marker_color=BLUE,
                marker_line_width=0,
                customdata=top[["nearcap_mwh_tight", "cap_max"]].values,
                hovertemplate="Anonymous plant #%{y}<br>Withholding score: %{x:.3f}"
                "<br>Near-max-price power in short hours: %{customdata[0]:,.0f} MWh"
                "<br>Largest amount offered: %{customdata[1]:.0f} MW<extra></extra>",
            )
        )
        fig.update_yaxes(type="category", title_text="anonymous plant ID")
        style(fig, height=460, legend=False, xtitle="withholding score")
        st.plotly_chart(fig, use_container_width=True)

    with right:
        section(
            "Does the plant behave differently when the grid is short?",
            "Each dot is a plant. The higher above the diagonal, the more it shifts to high prices during "
            "short hours specifically. Bigger dots = more near-max-price power offered when the grid was short.",
        )
        d = wh.copy()
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=d["hi_share_normal"] * 100,
                y=d["hi_share_tight"] * 100,
                mode="markers",
                marker=dict(
                    size=(d["nearcap_mwh_tight"].clip(lower=1) ** 0.5) / 30 + 6,
                    color=d["withholding_index"],
                    colorscale=[[0, SEQ_BLUE[0]], [1, SEQ_BLUE[5]]],
                    showscale=True,
                    colorbar=dict(title="score", thickness=10, len=0.6),
                    line=dict(width=0.5, color="white"),
                ),
                customdata=d[["res", "withholding_index"]].values,
                hovertemplate="Anonymous plant #%{customdata[0]}<br>High-priced share, short hours: %{y:.0f}%"
                "<br>High-priced share, normal hours: %{x:.0f}%<br>Withholding score: %{customdata[1]:.3f}<extra></extra>",
                name="",
            )
        )
        fig.add_shape(
            type="line",
            x0=0,
            y0=0,
            x1=100,
            y1=100,
            line=dict(color=MUTED, width=1, dash="dash"),
        )
        style(
            fig,
            height=460,
            legend=False,
            xtitle="high-priced share — normal hours (%)",
            ytitle="high-priced share — short hours (%)",
        )
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    section(
        "Look inside one plant",
        "Day by day, how much of this plant's offered power was priced high. Red dots mark days "
        "when this plant was offering during a short ('tight') hour — so a short day the plant sat "
        "out has no dot. Breaks in the line are days it submitted no energy offers at all.",
    )
    ids = wd["res"].unique().tolist()
    sel = st.selectbox(
        "Pick one of the top-scoring plants",
        ids,
        key=f"wh_pick_{market}_{basis}",
        format_func=lambda r: (
            f"Plant #{int(r)}  —  rank {int(wh[wh.res == r]['rank'].iloc[0])}, withholding score {wh[wh.res == r]['withholding_index'].iloc[0]:.3f}"
        ),
    )
    d = wd[wd["res"] == sel].sort_values("day")
    row = wh[wh.res == sel].iloc[0]
    k = st.columns(4)
    if row["withholding_index"] > 0:
        kpi(
            k[0],
            "Suspicion rank",
            f"#{int(row['rank'])}",
            "lower = higher withholding score; many plants tie at 0 and their order is arbitrary",
        )
    else:
        kpi(
            k[0],
            "Suspicion rank",
            "not ranked",
            "never priced above the high-price cutoff in a short hour",
        )
    kpi(
        k[1],
        "Withholding score",
        f"{row['withholding_index']:.3f}",
        tone="critical" if row["withholding_index"] > 0.2 else "serious",
    )
    kpi(k[2], "High-priced share, short hours", f"{row['hi_share_tight'] * 100:.0f}%")
    kpi(k[3], "High-priced share, normal hours", f"{row['hi_share_normal'] * 100:.0f}%")

    # Reindex onto every day in the plant's span so absent days render as GAPS. Plotly would
    # otherwise draw a straight line across months the plant never bid in.
    _dcal = (
        d.set_index("day")
        .reindex(pd.date_range(d["day"].min(), d["day"].max(), freq="D"))
        .rename_axis("day")
        .reset_index()
        if len(d)
        else d.assign(day=[])
    )
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=_dcal["day"],
            y=_dcal["hi_share"] * 100,
            mode="lines+markers",
            line=dict(color=BLUE, width=1.5),
            marker=dict(size=3, color=BLUE),
            connectgaps=False,
            name="High-priced share",
            hovertemplate="%{x|%b %d}: %{y:.0f}% priced high<extra></extra>",
        )
    )
    td = d[d["had_tight_hour"] == 1]
    fig.add_trace(
        go.Scatter(
            x=td["day"],
            y=td["hi_share"] * 100,
            mode="markers",
            marker=dict(color=STATUS["critical"], size=6),
            name="Grid was short this day",
            hovertemplate="%{x|%b %d} (grid short): %{y:.0f}% priced high<extra></extra>",
        )
    )
    style(fig, height=340, ytitle="% of offered power priced high")
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("See every plant's score (and download the data)"):
        show = wh[
            [
                "rank",
                "res",
                "sc",
                "cap_max",
                "tight_hours",
                "hi_share_tight",
                "hi_share_normal",
                "withholding_index",
                "nearcap_mwh_tight",
                "is_storage",
            ]
        ].copy()
        show = show.rename(
            columns={
                "rank": "Rank",
                "res": "Plant ID",
                "sc": "Bidder company ID",
                "cap_max": "Max offered (MW)",
                "tight_hours": "Short hours active",
                "hi_share_tight": "High-priced share (short)",
                "hi_share_normal": "High-priced share (normal)",
                "withholding_index": "Withholding score",
                "nearcap_mwh_tight": "Near-max power, short (MWh)",
                "is_storage": "Battery?",
            }
        )
        st.dataframe(show, width="stretch", height=320, hide_index=True)
        st.caption("Each row is one anonymous plant. Higher withholding score = more suspicious.")
        st.download_button(
            "⬇ Download these results (CSV)",
            wh.to_csv(index=False),
            f"caiso_withholding_2025_{market}_{basis}.csv",
            "text/csv",
            key=f"wh_dl_{market}_{basis}",
        )

# =====================================================================
# PAGE 3 — RE-IDENTIFICATION
# =====================================================================
elif PAGE == "Screen 2 · Unmasking bidders":
    st.markdown("## Screen 2 · Can we unmask the anonymous bidders?")
    st.caption(
        "The market publishes every plant's offers but hides *which* plant made them. This screen "
        "tests how well that anonymity holds up — matching an anonymous bidder to a real, named power "
        "plant using two public clues: **how much power it offers** and **when it goes quiet**. "
        "(Analysts call this *re-identification*.)"
    )

    with st.expander("How this screen works — in plain terms", expanded=False):
        t = META["thresholds"]
        st.markdown(f"""
Anonymous bidders have ID numbers, not names. But two public facts can give them away:

1. **Size clue.** Each bidder has a working ceiling — we use the **99th percentile** of its hourly offered capacity, so one freak hour doesn't set it. Real named plants publish their maximum capacity (called **PMAX**). If a bidder's ceiling is within **±{t["cap_tolerance_pct"]:.0f}%** of a named plant's capacity, that plant becomes a candidate. Bidders that have offered *more* than a plant's PMAX are ruled out for that plant, since no plant can exceed its own PMAX.
2. **Timing clue.** When a plant is on an outage it stops bidding. We build each anonymous bidder's calendar of "went-quiet" days and compare it to each named plant's outage calendar. When the two calendars line up unusually well, that's a fingerprint. "Went quiet" means no priced offer curve *and* no fixed self-schedule — a resource running on a self-schedule is counted as present at those megawatts, not as quiet.

We score the timing overlap with a standard statistic — the **Matthews correlation (φ)** — which runs from **−1** (the two calendars line up *worse* than chance) through **0** (no better than chance) to **1** (a perfect match). Plants that are always on, or always off, score near 0, so only genuinely *distinctive* patterns count. The forced and forced+planned methods admit a match only at φ ≥ 0.30; the magnitude-aware method can admit one on curtailment-size tracking alone, so its φ — and the day-ahead cross-check φ, which is never gated — can come out negative. **A negative φ is evidence against the match.** Note that confidence treats a negative φ as 0 rather than penalising it.

**Overall confidence = 60% timing match + 25% size match + 15% same type (battery vs. not).** A high-confidence match is a strong lead worth verifying — not courtroom proof.

*Worked example:* a bidder's went-quiet days line up with a plant's outages at **φ = 0.8**; the bidder's ceiling is within **2%** of the plant's capacity (size match ≈ 0.98); both are batteries (type match = 1). Confidence = 0.60·0.8 + 0.25·0.98 + 0.15·1 = **0.88** — a strong lead.

**Forced vs. planned outages.** A plant goes quiet during *any* outage — unexpected (**forced**) or scheduled (**planned**). Use the toggle below to match timing against forced outages only, or forced + planned. Adding planned outages stops penalizing a bidder for going quiet during scheduled maintenance, and can surface plants whose 2025 outages were mostly planned.

**Magnitude-aware (partial curtailments).** Most outages aren't full shut-downs — the plant loses only *part* of its capacity, and a bidder on a partial outage offers *proportionally* less. The third method correlates the **size** of a plant's daily curtailment against **how much the bidder scaled back its offers** (a rank correlation, ρ), then blends it in: *confidence = 0.35·timing + 0.30·size-tracking + 0.25·capacity + 0.10·type*. This reaches plants that only ever partially derate — invisible to the on/off methods. Because timing is weighted less here, the three methods are best read as **complementary**, not strictly ranked.

**Day-ahead cross-check.** Each match is scored a second time wherever the bidder has a usable **day-ahead** pattern — roughly half do; many bidders submit no day-ahead offers at all. If the bidder *also* goes quiet in day-ahead on the same plant's outage days, the match is corroborated by a second, separate bid stream. Both tests compare against the same plant outage calendar, so this is corroboration rather than fully independent evidence. The "Day-ahead ✓" column and the panel under each match flag it — a corroborated match is a materially stronger lead.
""")

    if not have("reident_matches_forced.parquet"):
        st.warning("No unmasking results available.")
        st.stop()

    mode = st.radio(
        "Match a bidder's quiet days against…",
        [
            "Forced outages only",
            "Forced + planned outages",
            "Magnitude-aware (uses partial curtailments)",
        ],
        index=0,
        horizontal=True,
        help="'Forced only' = unexpected breakdowns. 'Forced + planned' also counts scheduled maintenance. "
        "'Magnitude-aware' additionally correlates the SIZE of partial curtailments against how much the "
        "bidder scaled back its offers — reaching plants that only ever partially derate.",
    )
    is_mag = mode.startswith("Magnitude")
    use_planned = (
        mode != "Forced outages only"
    )  # combined & magnitude both use forced+planned bands
    fname = (
        "reident_matches_magnitude.parquet"
        if is_mag
        else "reident_matches_combined.parquet"
        if use_planned
        else "reident_matches_forced.parquet"
    )
    m = load(fname)
    r1 = m[m["rank"] == 1].copy()
    prof = load("bidder_profiles.parquet")

    hcf = META.get("reident_highconf_links", 0)
    hcc = META.get("reident_highconf_links_combined", hcf)
    hcm = META.get("reident_highconf_links_magnitude", hcf)
    trio = (
        f"Confident unmaskings (≥60%) — **forced:** {hcf} · **+planned:** {hcc} · "
        f"**magnitude-aware:** {hcm}."
    )
    st.caption(
        trio
        + (
            " Magnitude-aware reaches a much wider pool (plants that only partially derate), but weights timing "
            "less — so its count isn't directly comparable; the methods are complementary."
            if is_mag
            else " Adding planned outages stops penalizing a bidder for going quiet during scheduled maintenance."
        )
    )

    # day-ahead cross-check: independent corroboration from the DAM offers
    has_xcheck = "dam_corroborates" in r1.columns
    n_conf = int((r1["confidence"] >= 0.6).sum())
    n_corr = (
        int(r1[r1["confidence"] >= 0.6]["dam_corroborates"].fillna(False).sum())
        if has_xcheck
        else 0
    )

    c = st.columns(4)
    kpi(
        c[0],
        "Bidders with a usable pattern",
        f"{r1['res'].nunique():,}",
        "have a distinctive went-quiet pattern",
    )
    kpi(
        c[1],
        "Confidently unmasked",
        f"{n_conf:,}",
        "60%+ confidence",
        tone="critical",
    )
    kpi(
        c[2],
        "Very strong matches",
        f"{int((r1['confidence'] >= 0.75).sum()):,}",
        "75%+ confidence",
        tone="critical",
    )
    if has_xcheck:
        n_testable = int(((r1["confidence"] >= 0.6) & r1["phi_dam"].notna()).sum())
        kpi(
            c[3],
            "Corroborated by day-ahead",
            f"{n_corr:,} / {n_testable:,} testable",
            f"of the {n_conf:,} confident matches, {n_testable:,} have a day-ahead pattern the "
            "check can evaluate; the rest mostly submit no day-ahead offers at all",
            tone="good" if n_corr else None,
        )
    else:
        kpi(
            c[3],
            "Real plants identified",
            f"{r1[r1.confidence >= 0.6]['cand_name'].nunique():,}",
            "named at 60%+ confidence",
        )

    left, right = st.columns([2, 3])
    with left:
        section(
            "How confident are the matches?",
            "Each anonymous bidder's single best match. Bars right of the dotted line clear our 60% 'confident' bar.",
        )
        fig = go.Figure(
            go.Histogram(
                x=r1["confidence"],
                nbinsx=24,
                marker_color=BLUE,
                marker_line_width=0,
                hovertemplate="%{x:.0%} confidence<br>%{y} bidders<extra></extra>",
            )
        )
        fig.add_vline(
            x=0.6,
            line=dict(color=STATUS["critical"], width=1.5, dash="dot"),
            annotation_text="confident (60%)",
            annotation_font_color=MUTED,
            annotation_font_size=11,
        )
        style(
            fig,
            height=360,
            legend=False,
            xtitle="match confidence",
            ytitle="number of bidders",
        )
        st.plotly_chart(fig, use_container_width=True)

    with right:
        section(
            "The strongest unmaskings",
            "Each anonymous bidder → the real plant it most likely is.",
        )
        cols = [
            "res",
            "bidder_cap",
            "cand_name",
            "cand_pmax",
            "cap_diff_pct",
            "phi",
            "overlap_days",
            "confidence",
        ]
        rename = {
            "res": "Anon bidder",
            "bidder_cap": "Offered cap, P99 (MW)",
            "cand_name": "Likely real plant",
            "cand_pmax": "Plant capacity (MW)",
            "cap_diff_pct": "Size gap %",
            "phi": "Timing match (φ)",
            "overlap_days": "Matching days",
            "confidence": "Confidence",
        }
        if is_mag and "rho" in r1.columns:
            cols.insert(6, "rho")  # show size-tracking next to timing
            rename["rho"] = "Size-tracking (ρ)"
        if has_xcheck:
            cols.insert(len(cols) - 1, "dam_corroborates")  # just before Confidence
            rename["dam_corroborates"] = "Day-ahead ✓"
        show = r1.sort_values("confidence", ascending=False).head(25)[cols].rename(columns=rename)
        st.dataframe(show, width="stretch", height=360, hide_index=True)

    st.markdown("---")
    section(
        "See the fingerprint for one match",
        "The giveaway: an anonymous bidder's offered capacity collapses — to below about a third of "
        "its typical day — on a distinctive subset of the days its matched plant was derated. It need "
        "not be every outage day, and the drop need not be to zero.",
    )
    r1s = r1.sort_values("confidence", ascending=False)
    sel = st.selectbox(
        "Pick a match",
        r1s["res"].tolist(),
        key=f"reid_pick_{fname}",
        format_func=lambda r: (
            f"Bidder #{int(r)} → {r1s[r1s.res == r]['cand_name'].iloc[0]}  "
            f"({r1s[r1s.res == r]['confidence'].iloc[0]:.0%} confidence)"
        ),
    )
    link = r1s[r1s.res == sel].iloc[0]

    k = st.columns(5)
    kpi(k[0], "Anonymous bidder", f"#{int(link['res'])}")
    kpi(k[1], "Likely real plant", link["cand_name"])
    kpi(
        k[2],
        "Size match",
        f"{link['bidder_cap']:.1f} / {link['cand_pmax']:.1f} MW",
        "bidder's P99 offered MW / plant PMAX",
    )
    has_rho = is_mag and "rho" in link.index and pd.notna(link.get("rho"))
    if has_rho:
        kpi(
            k[3],
            "Timing φ · Size-track ρ",
            f"{link['phi']:.2f} · {link['rho']:.2f}",
            "−1 to 1 each; ρ uses partial curtailments",
            tone="critical" if max(link["phi"], link["rho"]) > 0.6 else "serious",
        )
    else:
        kpi(
            k[3],
            "Timing match (φ)",
            f"{link['phi']:.2f}",
            "−1 to 1; 0 = chance, 1 = perfect, negative = worse than chance",
            tone="critical" if link["phi"] > 0.6 else "serious",
        )
    kpi(k[4], "Overall confidence", f"{link['confidence']:.0%}", tone="critical")

    if has_xcheck:
        corr = bool(link.get("dam_corroborates"))
        phid = link.get("phi_dam")
        if corr:
            st.success(
                "✓ **Corroborated in the day-ahead market.** This bidder also goes quiet in its "
                f"day-ahead offers on {link['cand_name']}'s outage days (day-ahead timing φ = "
                f"{phid:.2f}) — a second, independent line of evidence beyond the real-time match."
            )
        elif pd.notna(phid):
            st.caption(
                f"Day-ahead cross-check: weaker in the day-ahead market (day-ahead timing φ = {phid:.2f}); "
                "the match rests mainly on the real-time pattern."
            )
        else:
            st.caption(
                "Day-ahead cross-check: this bidder has no usable day-ahead went-quiet pattern, so the "
                "day-ahead market can neither corroborate nor contradict the match."
            )

    # The bidder's offered-capacity series in BOTH markets: real-time (RTM, the
    # primary re-identification signal) and day-ahead (DAM, the corroborating one).
    # We stack them on a SHARED date axis so the went-quiet days line up vertically
    # across markets against the same outage bands.
    bhr = load("bidder_hourly_cap.parquet")
    bd = bhr[bhr["res"] == sel].copy()
    bd["h"] = pd.to_datetime(bd["h"])
    bd = bd.sort_values("h")

    bd_dam = bd.iloc[0:0].copy()
    if have("bidder_hourly_cap_dam.parquet"):
        bhr_dam = load("bidder_hourly_cap_dam.parquet")
        bd_dam = bhr_dam[bhr_dam["res"] == sel].copy()
        if len(bd_dam):
            bd_dam["h"] = pd.to_datetime(bd_dam["h"])
            bd_dam = bd_dam.sort_values("h")

    gran = st.radio(
        "Break points",
        ["Hourly", "Daily"],
        index=0,
        horizontal=True,
        key="reid_granularity",
        help="Hourly shows one point per hour — the top of that hour's offer curve, across all "
        "price steps and resubmissions. Daily shows each day's highest hourly point.",
    )

    def prep_series(df):
        """Return (x, y, hovertemplate, marker_size) at the chosen granularity."""
        if gran == "Daily":
            agg = df.assign(day=df["h"].dt.floor("D")).groupby("day", as_index=False)["cap"].max()
            return (
                agg["day"],
                agg["cap"],
                "%{x|%b %d}: %{y:.1f} MW (day's peak offer)<extra></extra>",
                4,
            )
        return (
            df["h"],
            df["cap"],
            "%{x|%b %d, %H:%M}: %{y:.1f} MW offered<extra></extra>",
            3,
        )

    def y_bounds(yvals):
        if len(yvals):
            y0, y1 = float(yvals.min()), float(yvals.max())
        else:
            y0, y1 = 0.0, 1.0
        pad = max(0.5, (y1 - y0) * 0.08)
        return y0 - pad, y1 + pad

    DAY_MS = 86400000  # 1 day width for the outage bars

    # the matched plant's outage days — same in both markets; drawn on both rows.
    forced = planned = None
    if have("resource_outage_daily.parquet"):
        rod = load("resource_outage_daily.parquet")
        od = rod[rod["rid"] == link["cand_rid"]].copy()
        od["day"] = pd.to_datetime(od["day"])
        forced = od[od["kind"] == "forced"]
        planned = od[od["kind"] == "planned"] if use_planned else od.iloc[0:0]

    dam_title = (
        "Day-ahead market (DAM)"
        if len(bd_dam)
        else "Day-ahead market (DAM) — no day-ahead priced offers from this bidder"
    )
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.11,
        subplot_titles=("Real-time market (RTM)", dam_title),
    )

    def add_market(row, df, show_legend):
        """Draw one market's outage bands + bidder offer line into subplot `row`."""
        xvals, yvals, hover, mk = prep_series(df)
        y0, y1 = y_bounds(yvals)

        def outage_bars(bdf, color, name, kindlabel):
            if bdf is None or not len(bdf):
                return
            pct = (bdf["curt_mw"] / bdf["pmax"] * 100).where(bdf["pmax"] > 0)
            cust = [[mw, p] for mw, p in zip(bdf["curt_mw"].fillna(0), pct.fillna(-1))]
            fig.add_trace(
                go.Bar(
                    x=bdf["day"],
                    y=[y1 - y0] * len(bdf),
                    base=y0,
                    width=DAY_MS,
                    marker=dict(color=color, line=dict(width=0)),
                    name=name,
                    legendgroup=name,
                    showlegend=show_legend,
                    customdata=cust,
                    hovertemplate=(
                        "%{x|%b %d, %Y}<br>"
                        + kindlabel
                        + " outage<br>Curtailed: %{customdata[0]:.0f} MW"
                        " (%{customdata[1]:.0f}% of PMAX)<extra></extra>"
                    ),
                ),
                row=row,
                col=1,
            )

        outage_bars(forced, "rgba(208,59,59,0.22)", "Matched plant — forced outage", "Forced")
        outage_bars(planned, "rgba(237,161,0,0.26)", "Matched plant — planned outage", "Planned")

        if len(yvals):
            fig.add_trace(
                go.Scatter(
                    x=xvals,
                    y=yvals,
                    mode="lines+markers",
                    name="Anonymous bidder — offered capacity",
                    legendgroup="bidder",
                    showlegend=show_legend,
                    line=dict(color=BLUE, width=1.3),
                    marker=dict(size=mk, color=BLUE, line=dict(width=0)),
                    hovertemplate=hover,
                ),
                row=row,
                col=1,
            )
            fig.add_hline(
                y=float(yvals.median()),
                line=dict(color=MUTED, width=1, dash="dot"),
                row=row,
                col=1,
            )
        fig.update_yaxes(range=[y0, y1], title_text="offered capacity (MW)", row=row, col=1)

    add_market(1, bd, show_legend=True)
    add_market(2, bd_dam, show_legend=False)
    fig.update_layout(bargap=0)
    style(fig, height=620, ytitle=None)

    _dots = (
        " Each blue dot is one hour the bidder submitted a priced offer curve; gaps are hours with "
        "no priced offer (the bidder may still have been running on a self-schedule)."
        if gran == "Hourly"
        else " Each blue dot is a day the bidder submitted priced offers (its peak that day); gaps "
        "are days with no priced offer."
    ) + " Hover an outage band to see how many MW the plant was curtailed."
    _bands = (
        "🟥 Red = forced (unexpected) outages · 🟧 Amber = planned (scheduled) outages. "
        if use_planned
        else "🟥 Red bands mark days the matched real plant was on a forced (unexpected) outage. "
    )
    if not len(bd_dam):
        _xc = (
            "This bidder submits no day-ahead priced offers, so only the real-time panel carries "
            "signal. "
        )
    elif not bool(link.get("dam_corroborates")):
        _xc = (
            "The day-ahead panel does **not** reproduce the pattern, so this match rests on the "
            "real-time series alone. "
        )
    else:
        _xc = (
            "The bidder's offered capacity drops on the plant's outage days in **both** markets. "
            "That is a second, separate bid stream — though both tests use the same plant outage "
            "calendar, so it is corroboration rather than independent evidence. "
        )
    st.caption(
        _bands
        + "**Top = real-time (RTM), bottom = day-ahead (DAM)**, sharing one date axis. "
        + _xc
        + _dots
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("See every candidate match (and download the data)"):
        st.dataframe(m, width="stretch", height=320)
        _label = (
            "magnitude-aware" if is_mag else "forced + planned" if use_planned else "forced-only"
        )
        st.caption(
            f"Each anonymous bidder can have up to three candidate plants; 'rank 1' is its best match. "
            f"Showing the **{_label}** results."
        )
        _tag = "magnitude" if is_mag else "forced_planned" if use_planned else "forced"
        st.download_button(
            "⬇ Download these matches (CSV)",
            m.to_csv(index=False),
            f"caiso_reidentification_2025_{_tag}.csv",
            "text/csv",
        )

# =====================================================================
# PAGE 3b — BIDDER LOOKUP (profile any resource id + its likeliest plants)
#   The other screens are population-level: they rank everyone and show the top of the
#   list. This one answers the opposite question — "tell me everything about THIS id" —
#   so it must resolve any id in the bid files, including the interties and loads the
#   withholding screens deliberately exclude.
# =====================================================================
elif PAGE == "Screen 3 · Look up one bidder":
    st.markdown("## Screen 3 · Look up one bidder")
    st.caption(
        "Everything the public data says about a single anonymous bidder — who it bids as, "
        "how much it offers, how it priced that power, and which real named plants it could "
        "plausibly be. Type any resource ID from the bid files."
    )

    if not have("bidder_directory.parquet"):
        st.warning(
            "Lookup tables not available. Re-run `python pipeline.py` to build "
            "`bidder_directory.parquet`, `bidder_candidates.parquet` and `plant_catalog.parquet`."
        )
        st.stop()

    bdir = load("bidder_directory.parquet")
    all_ids = sorted(bdir["res"].unique().tolist())

    pick_col, type_col = st.columns([3, 2])
    with pick_col:
        sel = st.selectbox(
            f"Resource ID  ({len(all_ids):,} in the bid data)",
            all_ids,
            format_func=lambda r: f"#{int(r)}",
            help="Every bidder ID present in either bid file — generators, interties and "
            "loads alike. Start typing to search.",
        )
    with type_col:
        typed = st.text_input(
            "…or paste an ID",
            placeholder="e.g. 111621",
            help="Overrides the picker when it matches a known ID.",
        )
    res_id = sel
    if typed.strip():
        try:
            cand_id = int(float(typed.strip()))
        except ValueError:
            st.error(f"'{typed}' is not a numeric resource ID.")
            cand_id = None
        else:
            if cand_id in set(all_ids):
                res_id = cand_id
            else:
                st.error(
                    f"Resource ID **{cand_id}** does not appear in either 2025 bid file. "
                    "It may belong to a different year, or be a plant that never submitted a bid."
                )

    rows = bdir[bdir["res"] == res_id]
    rtm_row = rows[rows["market"] == "RTM"]
    dam_row = rows[rows["market"] == "DAM"]
    markets = rows["market"].tolist()
    rtype = sorted({str(v) for v in rows["resource_type"].dropna()})
    is_gen = rtype == ["GENERATOR"]

    prof = pd.DataFrame()
    if have("bidder_profiles.parquet"):
        _p = load("bidder_profiles.parquet")
        prof = _p[_p["res"] == res_id]
    screened = len(prof) > 0

    st.markdown(f"### Bidder #{int(res_id)}")
    if not is_gen:
        st.info(
            f"**This ID bids as {', '.join(t.lower() for t in rtype)}, not a generator.** The "
            "withholding and unmasking screens cover generators only — an intertie is an "
            "import/export schedule and a load is demand, so neither can withhold its own "
            "capacity. The activity profile below still applies; the plant-matching panel does not."
        )
    elif not screened:
        st.info(
            "**This generator is outside the screened population.** It appears in the bid data "
            "but never submitted a priced energy offer with positive megawatts, so it has no "
            "capacity fingerprint to score."
        )

    # ---------- identity ----------
    c = st.columns(4)
    kpi(
        c[0],
        "Scheduling coordinator",
        f"#{int(rows['sc'].dropna().iloc[0]):,}" if rows["sc"].notna().any() else "—",
        "the market participant that submits on this resource's behalf",
    )
    kpi(
        c[1],
        "Resource type",
        " + ".join(t.title() for t in rtype) or "—",
        "as labelled in the bid file"
        + (
            " (bids under more than one type)"
            if int(rows["n_resource_types"].max() or 1) > 1
            else ""
        ),
    )
    kpi(
        c[2],
        "Bid markets",
        " + ".join(markets) if markets else "—",
        "real-time and/or day-ahead",
    )
    kpi(
        c[3],
        "Peak offered capacity",
        f"{rows['en_cap_max'].max():,.1f} MW" if rows["en_cap_max"].notna().any() else "—",
        "largest single-hour energy offer, either market",
    )

    st.markdown("")
    c = st.columns(4)
    _pk = float(prof["cap_ref"].iloc[0]) if screened else float("nan")
    kpi(
        c[0],
        "Working ceiling (P99)",
        f"{_pk:,.1f} MW" if _pk == _pk else "—",
        "99th percentile of hourly offered capacity — the size clue used for matching",
    )
    kpi(
        c[1],
        "Typical offer size",
        f"{float(prof['cap_avg'].iloc[0]):,.1f} MW" if screened else "—",
        "mean hourly offered capacity",
    )
    kpi(
        c[2],
        "Median offer price",
        f"\\${rows['med_en_price'].dropna().median():,.0f}/MWh"
        if rows["med_en_price"].notna().any()
        else "—",
        "middle of its priced energy steps",
    )
    _ss = int(rows["n_selfsched_rows"].sum())
    kpi(
        c[3],
        "Self-schedules",
        f"{_ss:,} rows" if _ss else "none",
        f"fixed must-run quantities, peak {rows['selfsched_mw_max'].max():,.0f} MW"
        if _ss
        else "never submitted a fixed self-schedule",
    )

    _days = int(rows["n_days"].max() or 0)
    _first, _last = rows["first_day"].min(), rows["last_day"].max()
    st.caption(
        f"Active on **{_days}** of the {META.get('n_bid_days', 364)} days in the data "
        f"({_first} → {_last}). Products bid: "
        f"**{', '.join(sorted({p for v in rows['products'].dropna() for p in str(v).split(',')}))}**."
        + (
            f" Hours with a priced energy offer: **{int(prof['active_hours'].iloc[0]):,}**."
            if screened
            else ""
        )
        + (
            "  Tagged as **storage** (battery) by its matched plant name."
            if screened and bool(prof["is_storage"].iloc[0])
            else ""
        )
    )

    # ---------- Screen 1 behaviour, every market x basis ----------
    st.markdown("---")
    section(
        "How it priced its power (Screen 1 scores)",
        "The withholding score for this bidder under every definition of 'when the grid is "
        "short'. Blank rows mean it did not clear the minimum short-hour activity to be scored "
        f"on that basis (at least {META['thresholds']['min_tight_hours']} short hours).",
    )
    if have("withholding_resource.parquet"):
        wr_all = load("withholding_resource.parquet")
        mine = wr_all[wr_all["res"] == res_id]
        BASIS_LABEL = {
            "outage": "Outages (forced)",
            "price": "Day-ahead price spikes",
            "price_rtm": "Real-time price spikes",
        }
        recs = []
        for mkt in ("RTM", "DAM"):
            for b in ("outage", "price", "price_rtm"):
                r = mine[(mine["market"] == mkt) & (mine["basis"] == b)]
                if not len(r):
                    recs.append(
                        {
                            "Market": mkt,
                            "'Short' defined by": BASIS_LABEL[b],
                            "Scored": "no",
                            "Rank": None,
                            "Withholding score": None,
                            "High-priced share, short": None,
                            "High-priced share, normal": None,
                            "Short hours": None,
                        }
                    )
                    continue
                r = r.iloc[0]
                recs.append(
                    {
                        "Market": mkt,
                        "'Short' defined by": BASIS_LABEL[b],
                        "Scored": "yes",
                        "Rank": int(r["rank"]) if r["withholding_index"] > 0 else None,
                        "Withholding score": round(float(r["withholding_index"]), 3),
                        "High-priced share, short": round(float(r["hi_share_tight"] or 0) * 100, 1),
                        "High-priced share, normal": round(
                            float(r["hi_share_normal"] or 0) * 100, 1
                        ),
                        "Short hours": int(r["tight_hours"]),
                    }
                )
        st.dataframe(pd.DataFrame(recs), width="stretch", hide_index=True)
        _best = mine[mine["withholding_index"] > 0]
        if len(_best):
            _b = _best.sort_values("withholding_index", ascending=False).iloc[0]
            st.caption(
                f"Its strongest signal is on the **{BASIS_LABEL[_b['basis']]}** basis in the "
                f"**{_b['market']}** market: it priced "
                f"**{float(_b['hi_share_tight']) * 100:.0f}%** of offered capacity at or above "
                f"\\${META['thresholds']['elevated_price']:.0f}/MWh in short hours versus "
                f"**{float(_b['hi_share_normal']) * 100:.0f}%** in normal hours. A positive gap is "
                "a lead, not proof — and a small plant's offers may not move the market at all."
            )
        else:
            st.caption(
                "No positive withholding score on any basis: this bidder did not shift capacity "
                "toward high prices when the grid was short."
            )
    else:
        st.caption("Withholding results not available.")

    # ---------- how identifiable is it, really? ----------
    if is_gen and have("bidder_anonymity.parquet"):
        _an = load("bidder_anonymity.parquet")
        _an = _an[_an["res"] == res_id]
        if len(_an):
            _a = _an.iloc[0]
            _tech_label = {
                "storage": "battery storage",
                "solar": "solar",
                "other": "not solar or storage",
            }[_a["tech"]]
            _tech_why = {
                "storage": "its bids carry state-of-charge limits, which only storage submits",
                "solar": "it essentially never offers overnight and clusters in daylight hours",
                "other": "it offers around the clock and carries no state-of-charge limits",
            }[_a["tech"]]
            st.markdown("---")
            section(
                "How identifiable is this bidder, really?",
                "Before trusting any single candidate, it is worth knowing how many real "
                "California plants this bidder could be judging only by what the bid data "
                "reveals — its size, and the technology implied by how it bids.",
            )
            c = st.columns(3)
            kpi(
                c[0],
                "Technology (from bidding alone)",
                _tech_label.title(),
                _tech_why,
            )
            kpi(
                c[1],
                "Real plants of this size",
                f"{int(_a['n_plants_size']):,}",
                f"in-service California plants within ±{META['thresholds']['cap_tolerance_pct']:.0f}%"
                f" of {float(_a['cap_ref']):,.1f} MW",
            )
            kpi(
                c[2],
                "…of this size AND technology",
                f"{int(_a['n_plants_size_tech']):,}",
                f"out of {int(_a['n_plants_tech_total']):,} such plants statewide",
                tone="good" if int(_a["n_plants_size_tech"]) > 5 else "critical",
            )
            _k = int(_a["n_plants_size_tech"])
            _floor = float(META.get("anonymity", {}).get("cec_floor_mw", 1.0))
            _n_cands = 0
            if have("bidder_candidates.parquet"):
                _bcx = load("bidder_candidates.parquet")
                _n_cands = int((_bcx["res"] == res_id).sum())
            if bool(_a.get("below_cec_floor", False)):
                st.info(
                    f"**This comparison can't say anything for a bidder this small.** At "
                    f"{float(_a['cap_ref']):,.2f} MW it sits below the CEC list's coverage "
                    f"(only a handful of listed plants are under {_floor:g} MW), so a low count "
                    "here reflects what the list contains, not how distinctive this bidder is. "
                    "Resources this small are usually aggregations of units rather than a single "
                    "listed plant."
                )
            elif _k == 0:
                st.info(
                    f"**No in-service listed plant is {_tech_label} at roughly "
                    f"{float(_a['cap_ref']):,.1f} MW.** That most likely means this bidder is not a "
                    "single CEC-listed plant at all — an aggregation of units, an out-of-state "
                    "resource delivering into CAISO, or a plant the list has under a different "
                    "capacity. Size gives no purchase here either way."
                )
            elif _k == 1:
                st.error(
                    f"**Size alone nearly identifies this bidder.** Exactly one in-service "
                    f"California plant is {_tech_label} at roughly {float(_a['cap_ref']):,.1f} MW. "
                    "A bidder this distinctive is exposed by its capacity before any timing "
                    "analysis is applied — which is itself a finding about the anonymization, and "
                    "a rare one: it does not happen to any other bidder the list covers."
                )
            elif _k <= 5:
                st.warning(
                    f"**Only {_k} real plants share this size and technology**, so capacity alone "
                    "already narrows this bidder to a handful. The anonymization is thin here."
                )
            else:
                st.info(
                    f"**Capacity alone cannot identify this bidder** — {_k} real California plants "
                    f"are {_tech_label} at roughly this size. Any narrowing below that comes from "
                    "the outage-timing evidence, not from size"
                    + (
                        f", which is what takes it to the {_n_cands} candidate(s) below."
                        if _n_cands
                        else "."
                    )
                )
            _ac = META.get("anonymity", {})
            st.caption(
                "Counts come from the California Energy Commission's public plant list "
                f"({int(_ac.get('cec_plants', 0)):,} in-service plants). That list carries no "
                "CAISO resource ID, so it is **never** joined to a bidder by name — only counted, "
                "which is why no plant name, operator or location from it appears anywhere here. "
                f"Across the {int(_ac.get('bidders_covered', 0)):,} bidders it covers (at or above "
                f"{float(_ac.get('cec_floor_mw', 1.0)):g} MW), a typical one shares its size and "
                f"technology with **{int(_ac.get('median_size_tech_matches', 0))}** real plants and "
                f"only **{int(_ac.get('unique_on_size_tech', 0))}** are unique on that basis. "
                "**Capacity and technology essentially never identify a bidder on their own** — "
                "the outage-timing evidence does that work. A further "
                f"{int(_ac.get('bidders_below_floor', 0)):,} bidders are too small for the list to "
                "cover at all."
            )

    # ---------- activity over time ----------
    st.markdown("---")
    section(
        "What it offered, day by day",
        "Offered capacity in each market. Gaps are days with no priced offer — which may mean "
        "an outage, or simply that it ran on a fixed self-schedule instead.",
    )

    def _daily(fname):
        if not have(fname):
            return pd.DataFrame(columns=["day", "cap"])
        d = load(fname)
        d = d[d["res"] == res_id][["day", "cap"]].copy()
        d["day"] = pd.to_datetime(d["day"])
        return d.sort_values("day")

    d_rtm, d_dam = _daily("bidder_daily_cap.parquet"), _daily("bidder_daily_cap_dam.parquet")
    if len(d_rtm) or len(d_dam):
        _all_days = pd.concat([d_rtm["day"], d_dam["day"]])
        cal = pd.date_range(_all_days.min(), _all_days.max(), freq="D")
        fig = go.Figure()
        for d, lbl, col in ((d_rtm, "Real-time (RTM)", BLUE), (d_dam, "Day-ahead (DAM)", AQUA)):
            if not len(d):
                continue
            ser = d.set_index("day").reindex(cal)["cap"]
            fig.add_trace(
                go.Scatter(
                    x=cal,
                    y=ser.values,
                    mode="lines",
                    name=lbl,
                    line=dict(color=col, width=1.6),
                    connectgaps=False,
                    hovertemplate=f"{lbl}<br>%{{x|%b %d}}: %{{y:,.1f}} MW offered<extra></extra>",
                )
            )
        style(fig, height=300, ytitle="MW offered (daily peak)")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("No priced daily offer series for this bidder.")

    # ---------- top 5 candidate plants ----------
    st.markdown("---")
    section(
        "Top 5 plants this bidder could be",
        "Ranked by the same evidence Screen 2 uses — capacity size, went-quiet timing against "
        "the plant's outage calendar, curtailment-size tracking, and battery-vs-not type — but "
        "here we always show the best five, even when the evidence is weak.",
    )

    if not is_gen:
        st.info("Plant matching applies to generators only. Nothing to match for this ID.")
    elif not have("bidder_candidates.parquet"):
        st.caption("Candidate table not available. Re-run `python pipeline.py`.")
    else:
        bc = load("bidder_candidates.parquet")
        mine_c = bc[bc["res"] == res_id].sort_values("rank").head(5)
        if not len(mine_c):
            st.warning(
                "**No candidate plants.** No named plant in the outage data has a capacity "
                f"within ±{META['thresholds']['cap_tolerance_pct']:.0f}% of this bidder's "
                "working ceiling, so there is nothing to rank. A bidder can be un-matchable "
                "simply because no plant of its size ever reported an outage in 2025."
            )
        else:
            _fp = bool(mine_c["fingerprintable"].iloc[0])
            _any_admitted = bool(mine_c["admitted"].any())
            if not _fp:
                st.warning(
                    "**This bidder has no usable went-quiet pattern**, so timing evidence (φ) "
                    "carries no weight here. The ranking below rests mostly on capacity size, "
                    "which many plants can share — treat it as a shortlist to check, not an "
                    "identification."
                )
            elif not _any_admitted:
                st.warning(
                    "**None of these candidates clears Screen 2's admission test** (φ ≥ 0.30 on "
                    "at least 3 overlapping days, or ρ ≥ 0.35). They are the closest available "
                    "matches, not confident ones."
                )
            tbl = pd.DataFrame(
                {
                    "#": mine_c["rank"].astype(int),
                    "Plant": mine_c["cand_name"],
                    "PMAX (MW)": mine_c["cand_pmax"].round(1),
                    "Size gap": mine_c["cap_diff_pct"].map(lambda v: f"{v:.1f}%"),
                    "Timing φ": mine_c["phi"].round(3),
                    "Quiet+outage days": mine_c["overlap_days"].astype(int),
                    "Curtailment ρ": mine_c["rho"].map(
                        lambda v: "—" if pd.isna(v) else f"{float(v):.2f}"
                    ),
                    "Type match": mine_c["type_match"].map({True: "yes", False: "no"}),
                    "Confidence": mine_c["confidence"].map(lambda v: f"{float(v) * 100:.0f}%"),
                    "Day-ahead ✓": mine_c["dam_corroborates"].map({True: "yes", False: "no"}),
                    "Clears Screen 2 gate": mine_c["admitted"].map({True: "yes", False: "no"}),
                }
            )
            st.dataframe(tbl, width="stretch", hide_index=True)
            st.caption(
                "**Size gap** = how far the plant's PMAX is from this bidder's P99 offered "
                "capacity. **φ** compares the bidder's quiet days with the plant's outage days "
                "(−1 to 1; 0 = chance). **ρ** correlates the depth of the plant's curtailment "
                "with how far the bidder scaled back. **Confidence** blends "
                "0.35·φ + 0.30·ρ + 0.25·size + 0.10·type — so a candidate can score ~30% on "
                "capacity alone with no timing evidence whatsoever."
            )

            # per-candidate evidence overlay
            top = mine_c.iloc[0]
            csel = st.selectbox(
                "Show the evidence for…",
                mine_c["cand_rid"].tolist(),
                format_func=lambda r: (
                    f"#{int(mine_c[mine_c.cand_rid == r]['rank'].iloc[0])}  "
                    f"{mine_c[mine_c.cand_rid == r]['cand_name'].iloc[0]}"
                ),
            )
            crow = mine_c[mine_c["cand_rid"] == csel].iloc[0]
            if have("resource_outage_daily.parquet") and len(d_rtm):
                rod = load("resource_outage_daily.parquet")
                od = rod[rod["rid"] == csel].copy()
                DAY_MS = 86400000  # one day wide, so each bar covers exactly its own day
                cal = pd.date_range(d_rtm["day"].min(), d_rtm["day"].max(), freq="D")
                yv = d_rtm.set_index("day").reindex(cal)["cap"]
                _ymax = float(yv.max()) if yv.notna().any() else 1.0
                y0, y1 = 0.0, (_ymax * 1.08 if _ymax > 0 else 1.0)
                fig = go.Figure()

                # Outage days are drawn as full-height BARS rather than shapes: a plotly shape
                # (add_vrect) cannot emit hover events, so the band details would be invisible.
                def _bands(kind, color, label):
                    b = od[od["kind"] == kind]
                    if not len(b):
                        return
                    pmax = b["pmax"].where(b["pmax"] > 0)
                    pct = (b["curt_mw"] / pmax * 100).fillna(-1)
                    cust = [
                        [mw if mw == mw else -1, p, pm if pm == pm else -1]
                        for mw, p, pm in zip(b["curt_mw"], pct, b["pmax"])
                    ]
                    fig.add_trace(
                        go.Bar(
                            x=b["day"],
                            y=[y1 - y0] * len(b),
                            base=y0,
                            width=DAY_MS,
                            marker=dict(color=color, line=dict(width=0)),
                            name=f"{crow['cand_name']} — {label} outage",
                            customdata=cust,
                            hovertemplate=(
                                "<b>%{x|%a %d %b %Y}</b><br>"
                                f"{crow['cand_name']}<br>"
                                f"{label.title()} outage<br>"
                                "Curtailed: %{customdata[0]:,.0f} MW"
                                " (%{customdata[1]:.0f}% of PMAX)<br>"
                                "Plant PMAX: %{customdata[2]:,.0f} MW<extra></extra>"
                            ),
                        )
                    )

                if len(od):
                    od["day"] = pd.to_datetime(od["day"])
                    _bands("forced", "rgba(208,59,59,0.22)", "forced")
                    _bands("planned", "rgba(237,161,0,0.26)", "planned")
                fig.add_trace(
                    go.Scatter(
                        x=cal,
                        y=yv.values,
                        mode="lines",
                        name="Bidder's offered MW",
                        line=dict(color=BLUE, width=1.6),
                        connectgaps=False,
                        hovertemplate="%{x|%b %d}: %{y:,.1f} MW offered<extra></extra>",
                    )
                )
                fig.update_yaxes(range=[y0, y1])
                fig.update_layout(bargap=0, hovermode="closest")
                style(fig, height=300, ytitle="MW offered (daily peak)")
                st.plotly_chart(fig, use_container_width=True)
                st.caption(
                    f"🟥 Red = **{crow['cand_name']}** on a forced outage · 🟧 Amber = planned. "
                    "**Hover any band** for that day's curtailed megawatts and what share of the "
                    "plant's capacity that was. "
                    f"Of this bidder's **{int(crow['dip_days'])}** quiet days, "
                    f"**{int(crow['overlap_days'])}** fall on one of that plant's outage days "
                    f"(φ = {float(crow['phi']):.3f})."
                    + (
                        ""
                        if len(od)
                        else "  No daily outage detail is stored for this plant, so no bands are drawn."
                    )
                )
            _pc = load("plant_catalog.parquet") if have("plant_catalog.parquet") else pd.DataFrame()
            if len(_pc):
                pc = _pc[_pc["rid"] == csel]
                if len(pc):
                    pc = pc.iloc[0]
                    st.caption(
                        f"**{pc['rname']}** — PMAX {float(pc['pmax']):,.1f} MW, net qualifying "
                        f"capacity {float(pc['nqc']):,.1f} MW, "
                        f"{int(pc['forced_days'])} forced and {int(pc['planned_days'])} planned "
                        f"outage days in 2025"
                        + (", tagged as storage." if bool(pc["is_storage"]) else ".")
                    )

            # does this bidder appear in the published match sets?
            pub = []
            for _n, _lbl in (
                ("forced", "forced outages"),
                ("combined", "forced + planned"),
                ("magnitude", "magnitude-aware"),
            ):
                _f = f"reident_matches_{_n}.parquet"
                if not have(_f):
                    continue
                _m = load(_f)
                _mm = _m[(_m["res"] == res_id) & (_m["rank"] == 1)]
                if len(_mm) and float(_mm["confidence"].iloc[0]) >= 0.60:
                    pub.append(
                        f"**{_lbl}** → {_mm['cand_name'].iloc[0]} "
                        f"({float(_mm['confidence'].iloc[0]) * 100:.0f}%)"
                    )
            if pub:
                st.success(
                    "This bidder is **confidently unmasked** on Screen 2 by: " + "; ".join(pub)
                )
            else:
                st.caption(
                    "This bidder is **not** among Screen 2's confident unmaskings under any "
                    "method, so nothing above should be read as an identification."
                )

        st.download_button(
            "⬇ Download this bidder's candidates (CSV)",
            (
                load("bidder_candidates.parquet")
                .pipe(lambda d: d[d["res"] == res_id])
                .to_csv(index=False)
            ),
            f"caiso_bidder_{int(res_id)}_candidates.csv",
            "text/csv",
        )

# =====================================================================
# PAGE 4 — METHOD & ASSUMPTIONS
# =====================================================================
else:
    st.markdown("## How this works & what it can't tell you")
    st.caption(
        "Worth reading before you share these results. Both screens produce **leads to investigate**, "
        "not proof of wrongdoing."
    )

    # ---------- the data behind the tool ----------
    section(
        "The data behind this tool",
        f"Everything here is built from {len(META.get('datasets', []))} public datasets for the "
        "2025 delivery year — no private or confidential data is used. All the market data comes "
        "from CAISO; the one outside source is the California Energy Commission's public plant "
        "list, used only to count how many real plants share a bidder's size and technology (see "
        "its card below — no names from it are attached to any bidder). Each plant is anonymized "
        "to an ID number in the bid data.",
    )
    ds = META.get("datasets", [])
    for i in range(0, len(ds), 2):
        cols = st.columns(2)
        for col, d in zip(cols, ds[i : i + 2]):
            with col:
                col.markdown(
                    f"**{d['name']}**  \n<span style='font-size:12px;color:{MUTED}'>"
                    f"<code>{d['file']}</code></span>",
                    unsafe_allow_html=True,
                )
                col.caption(d["what"])
                body = "\n".join(f"| {lab} | {val} |" for lab, val in d["stats"])
                col.markdown(f"| measure | value |\n|---|---|\n{body}")
                col.markdown("")
    st.caption(
        "A note on dates: the outages file contains records going back several years, but every "
        "screen analyzes only the **2025** delivery year (Jan 1 → Dec 31). One day, 2025-03-09 "
        "(the spring daylight-saving switch), is absent from both bid files and the day-ahead price "
        f"file, so no hour of it is analyzed anywhere — coverage is {META.get('n_bid_days', 364)} "
        f"days / {META.get('n_bid_hours', 8736):,} hours. (The 5-minute real-time price file does "
        "cover that day.) On 2025-11-02 the clocks go back and the two physical 01:00 hours share "
        "one row; prices keep the higher of the two, matching how the bid files stamp that hour."
    )
    st.markdown("---")

    t = META["thresholds"]
    c = st.columns(2)
    with c[0]:
        section("The settings behind the screens")
        st.markdown(f"""
| Setting | Value | In plain terms |
|---|---|---|
| "High price" cutoff | **\\${t["elevated_price"]:.0f}/MWh** | Offers at or above this count as steeply priced (the median energy offer is roughly \\$35/MWh in real-time, \\$40 day-ahead). |
| "Near-maximum" cutoff | **\\${t["nearcap_price"]:.0f}/MWh** | Close to the market's ~\\$1,000 price ceiling. |
| "Grid is short" — outages | **top {int((1 - t["tight_percentile"]) * 100)}%** of hours | Hours with ≥ {t["tight_mw"]:,.0f} MW on a forced (unplanned) outage; planned maintenance excluded. |
| "Grid is short" — day-ahead prices | **top {int((1 - t.get("price_tight_percentile", 0.9)) * 100)}%** of hours | Hours with system day-ahead price ≥ \\${t.get("price_tight_lmp", 0):.0f}/MWh. |
| "Grid is short" — real-time prices | **top {int((1 - t.get("price_tight_percentile", 0.9)) * 100)}%** of hours | Hours whose *average* 5-minute real-time system price was ≥ \\${t.get("price_tight_lmp_rtm", 0):.0f}/MWh. |
| Min. short hours to score | **{t["min_tight_hours"]}** | A plant needs enough short-hour activity to be judged fairly. |
| Size-match tolerance | **±{t["cap_tolerance_pct"]:.0f}%** | How close a bidder's ceiling must be to a plant's capacity to be a candidate. |
""")
    with c[1]:
        section("What's in the data")
        st.markdown(f"""
| | |
|---|---|
| Source | {META["generated_scope"]} |
| Price steps in the source file | {meta_num("n_bid_rows"):,} |
| Price steps used by the screens | {meta_num("n_bid_rows_analyzed", "n_bid_rows"):,} |
| Bidders analyzed (generators with positive-MW energy offers) | {META["n_resources"]:,} |
| Dates | {META["date_min"]} → {META["date_max"]} |
| Bid markets | {" + ".join(META.get("markets", ["RTM"]))} (real-time + day-ahead) |
| Day-ahead prices | 3 trading hubs, median \\${META.get("price", {}).get("median_sys_lmp", 0):.0f}/MWh, {META.get("price", {}).get("neg_price_hours", 0):,} negative-price hours |
| Plants scored — RTM (outage / day-ahead price / real-time price) | {META.get("withholding_scored_by", {}).get("RTM_outage", 0):,} / {META.get("withholding_scored_by", {}).get("RTM_price", 0):,} / {META.get("withholding_scored_by", {}).get("RTM_price_rtm", 0):,} |
| Plants scored — DAM (outage / day-ahead price / real-time price) | {META.get("withholding_scored_by", {}).get("DAM_outage", 0):,} / {META.get("withholding_scored_by", {}).get("DAM_price", 0):,} / {META.get("withholding_scored_by", {}).get("DAM_price_rtm", 0):,} |
| Confident unmaskings — forced / +planned / magnitude-aware | {META["reident_highconf_links"]:,} / {META["reident_highconf_links_combined"]:,} / {META["reident_highconf_links_magnitude"]:,} |
| …of those, corroborated by the day-ahead cross-check | {META.get("reident_dam_corroborated", 0):,} / {META.get("reident_dam_corroborated_combined", 0):,} / {META.get("reident_dam_corroborated_magnitude", 0):,} |
""")

    section(
        "How each number is worked out",
        "Every metric in this tool is a simple comparison — here is each one in plain terms, with a "
        "worked example.",
    )
    st.markdown(f"""
**1 · Withholding score (Screen 1).** We look at each plant on its own and ask: does it price power steeply high *more often when the grid is short*?

> *Worked example.* A plant offers 100 MW. In **normal** hours it prices only 20 MW of that steeply high (≥ \\${t["elevated_price"]:.0f}/MWh) → a 20% "high-priced share." In **short** hours it prices 60 MW steeply high → 60%.
> **Withholding score = 60% − 20% = 0.40.** The bigger the jump when the grid is short, the higher the score. A score near 0 means the plant behaves the same either way.

**2 · Clearing-price impact (Screen 1, day-ahead market only).** The same idea, but instead of a fixed \\${t["elevated_price"]:.0f} cutoff we use the price that *actually cleared* the market that hour.

> *Worked example.* In a short hour the day-ahead price settled at \\$80/MWh. A plant offered 100 MW but priced 30 MW of it above \\$80 — so those 30 MW couldn't be used. That's **30 MWh offered above the clearing price** (effectively withheld). The impact index compares that share in short vs. normal hours, exactly like the withholding score, but measured against the real market price.

**3 · Timing match, φ (Screen 2).** We line up two calendars — the days a bidder *went quiet* and the days a named plant was *on outage* — and score how well they coincide with a standard statistic, the Matthews correlation (**φ**).

> φ runs from **−1** (the two calendars line up *worse* than random) through **0** (no better than random) to **1** (a perfect match). A plant that is almost always on, or almost always off, scores near 0 — so only genuinely *distinctive* patterns produce a high φ. A negative φ is evidence *against* a match; note that the confidence blend treats a negative φ as 0 rather than penalising it.

**4 · Match confidence (Screen 2).** We blend three public clues into one 0–100% score:

> **confidence = 60% × timing match (φ) + 25% × how closely the sizes match + 15% × same type (battery or not).** We call a match **confident** at **≥ 60%**. (The magnitude-aware method re-weights these and adds a fourth clue — see that screen.)

**5 · Day-ahead cross-check (Screen 2).** We run the timing-match test a *second* time using the bidder's **day-ahead** offers, wherever the bidder has a usable day-ahead pattern — roughly half do, since many submit no day-ahead offers at all. If the bidder also goes quiet in day-ahead on the same plant's outage days, the match is corroborated by a second, separate bid stream. Both tests use the same plant outage calendar, so this is corroboration rather than fully independent evidence — but it is still a materially stronger lead.

**6 · "When the grid is short."** Three independent definitions, toggled in Screen 1:

> **Outages** — the {int((1 - t["tight_percentile"]) * 100)}% of hours with the most capacity on a **forced (unplanned)** outage (≥ {t["tight_mw"]:,.0f} MW); scheduled maintenance is excluded. Needs no price data.
> **Day-ahead prices** — the {int((1 - t.get("price_tight_percentile", 0.9)) * 100)}% of hours with the highest day-ahead system price (≥ \\${t.get("price_tight_lmp", 0):.0f}/MWh). The market's own scarcity signal.
> **Real-time prices** — the {int((1 - t.get("price_tight_percentile", 0.9)) * 100)}% of hours whose *average* 5-minute real-time system price was highest (≥ \\${t.get("price_tight_lmp_rtm", 0):.0f}/MWh). Because it is an hourly average, one 5-minute spike inside an otherwise cheap hour does not by itself mark the hour short.
""")

    section("Key assumptions & caveats")
    for a in META["assumptions"]:
        st.markdown(f"- {a}")
    st.markdown("""
- **Leads, not proof.** Both screens spot *suspicious patterns*. Confirming them needs the private, real-time
  data — actual market prices, metered output, and regulator logs — held by the official market monitor.
- **On holding back power:** high prices when the grid is short can be legitimate scarcity pricing *or* gaming.
  This screen can't tell the two apart on its own; it points investigators to where to look.
- **On unmasking:** the fact that anonymous bidders *can* be re-identified from public clues is itself a useful
  finding — it means the anonymization has a weakness worth reporting to whoever sets the data-sharing rules.
""")

    section("Plain-language glossary")
    st.markdown("""
- **CAISO** — the California Independent System Operator, the agency that runs California's power grid and its electricity market.
- **Megawatt-hour (MWh)** — a unit of electricity; roughly enough to power ~750 average homes for one hour.
- **\\$/MWh** — the price of electricity, per megawatt-hour.
- **Offer (or bid)** — a plant's statement of how much power it will sell, and at what price, for a given hour.
- **Price cap** — the highest price the market allows (about \\$1,000/MWh here).
- **Forced outage** — an unplanned plant breakdown; it stops producing.
- **Tight / short grid** — an hour when lots of capacity is offline and supply is scarce.
- **Economic withholding** — pricing power so high it won't be used, to shrink supply and lift prices.
- **Re-identification** — figuring out which real, named plant an anonymous bidder actually is.
- **PMAX** — a plant's maximum output capacity.
- **φ (phi), the Matthews correlation** — a −1-to-1 score for how well two on/off calendars line up (0 = chance, 1 = perfect, negative = worse than chance).
- **Ancillary services** — backup and stability services (like standby reserves) the grid buys on top of plain energy.
""")
    st.caption(
        "Built from public CAISO data • colors checked for color-blind readability • no outside data used."
    )
