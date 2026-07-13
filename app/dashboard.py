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

HERE = os.path.dirname(os.path.abspath(__file__))
DER = os.path.join(HERE, "data", "derived")

# ---- validated dataviz palette (fixed categorical order; sequential blue) ----
CAT = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
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


if not have("meta.json"):
    st.error("Derived data not found. Run `python pipeline.py` first.")
    st.stop()

META = load_meta()


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
            orientation="h", yanchor="bottom", y=1.02, x=0, title_text="", bgcolor="rgba(0,0,0,0)"
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
        "Screen 1 · Holding back power",
        "Screen 2 · Unmasking bidders",
        "How this works & caveats",
    ],
    label_visibility="collapsed",
)
st.sidebar.markdown("---")
st.sidebar.markdown("**What am I looking at?**")
st.sidebar.caption(
    "Every power plant in California offers to sell electricity, hour by hour. "
    "Those offers are public but stripped of names. This tool reads a full year "
    "of them and checks for two things: plants that may be **gaming prices**, and "
    "how easily the **anonymity can be undone**."
)
st.sidebar.markdown("---")
st.sidebar.metric("Price offers analyzed", f"{META['n_bid_rows'] / 1e6:.1f} million")
st.sidebar.metric("Anonymous bidders", f"{META['n_resources']:,}")
st.sidebar.caption(
    f"Period: full-year {META['date_min'][:4]}  ({META['date_min']} → {META['date_max']})"
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
        "reverse-engineered (Screen 2)."
    )

    md = load("market_daily.parquet")
    md["day"] = pd.to_datetime(md["day"])
    wh = load("withholding_resource.parquet")

    c = st.columns(5)
    kpi(
        c[0],
        "Price offers analyzed",
        f"{META['n_bid_rows'] / 1e6:.1f} M",
        "hourly offers to sell power, full year",
    )
    kpi(c[1], "Power plants", f"{META['n_resources']:,}", "anonymous bidders in the data")
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
        "bidders we could confidently name",
        tone="critical" if hc else None,
    )
    kpi(
        c[4],
        "Tightest moment",
        f"{md['peak_tight_mw'].max() / 1000:.1f} GW",
        "most plant capacity offline at once (1 GW ≈ 750k homes)",
    )

    st.markdown("")
    left, right = st.columns([3, 2])

    with left:
        section(
            "How often power was offered at near-maximum prices",
            "The market caps prices at about \\$1,000 per megawatt-hour. This is the share of all "
            f"offered power priced within reach of that cap (≥ \\${META['thresholds']['nearcap_price']}). "
            "Spikes mean lots of capacity "
            "was parked at prices so high it was unlikely to actually be used — a pattern worth "
            "watching.",
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
        "'Energy' is plain electricity and dominates. The rest are backup and grid-stability "
        "services (called ancillary services) — reserves kept on standby in case a plant suddenly fails.",
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
                hovertemplate=f"{product_label(prod)}<br>%{{x|%b %Y}}: %{{y:,}} offers<extra></extra>",
            )
        )
    fig.update_layout(barmode="stack", bargap=0.25)
    style(fig, height=340, ytitle="offers per month")
    st.plotly_chart(fig, use_container_width=True)

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

    with st.expander("How this screen works — in plain terms", expanded=False):
        t = META["thresholds"]
        st.markdown(f"""
The idea: a plant gaming the market will price its power very high **especially when the grid is short**, because that's when the tactic pays off. So we compare each plant against *itself* — its behavior in short hours vs. normal hours.

- **"Grid is short" hours** = the {int((1 - t["tight_percentile"]) * 100)}% of hours with the most plant capacity offline (at least **{t["tight_mw"]:,.0f} MW** unavailable). The data has no actual prices, so we use outages as a stand-in for how short the grid was.
- For each plant we measure the share of its offered power priced steeply high — **≥ \\${t["elevated_price"]:.0f} per megawatt-hour** — separately during short hours and normal hours. (For context, a typical price is around $32.)
- **Withholding score = (high-priced share when short) − (high-priced share when normal).** A big positive number means the plant pushes prices up precisely when the grid can least afford it.
- We only score plants active in at least {t["min_tight_hours"]} short hours, so the number is reliable ({META["withholding_scored"]:,} plants qualify).
- **Important:** this flags *suspicious behavior*, not proven wrongdoing — a lead, not a verdict. Confirming it means checking against the actual market prices, which aren't in this public data.
""")

    wh = load("withholding_resource.parquet")
    wd = load("withholding_daily.parquet")
    wd["day"] = pd.to_datetime(wd["day"])

    flagged = int((wh["withholding_index"] > 0.05).sum())
    c = st.columns(4)
    kpi(c[0], "Plants analyzed", f"{len(wh):,}", "had enough short-hour activity to score")
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
            type="line", x0=0, y0=0, x1=100, y1=100, line=dict(color=MUTED, width=1, dash="dash")
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
        "Day by day, how much of this plant's offered power was priced high. Red dots mark days when the grid hit a short ('tight') hour.",
    )
    ids = wd["res"].unique().tolist()
    sel = st.selectbox(
        "Pick a flagged plant",
        ids,
        format_func=lambda r: (
            f"Plant #{int(r)}  —  rank {int(wh[wh.res == r]['rank'].iloc[0])}, withholding score {wh[wh.res == r]['withholding_index'].iloc[0]:.3f}"
        ),
    )
    d = wd[wd["res"] == sel].sort_values("day")
    row = wh[wh.res == sel].iloc[0]
    k = st.columns(4)
    kpi(k[0], "Suspicion rank", f"#{int(row['rank'])}", "1 = most suspicious")
    kpi(
        k[1],
        "Withholding score",
        f"{row['withholding_index']:.3f}",
        tone="critical" if row["withholding_index"] > 0.2 else "serious",
    )
    kpi(k[2], "High-priced share, short hours", f"{row['hi_share_tight'] * 100:.0f}%")
    kpi(k[3], "High-priced share, normal hours", f"{row['hi_share_normal'] * 100:.0f}%")

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=d["day"],
            y=d["hi_share"] * 100,
            mode="lines",
            line=dict(color=BLUE, width=1.5),
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
            "caiso_withholding_2025.csv",
            "text/csv",
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

1. **Size clue.** Every bidder has a maximum amount of power it offers. Real named plants publish their maximum capacity (called **PMAX**). If a bidder's ceiling matches a named plant's capacity within **±{t["cap_tolerance_pct"]:.0f}%**, that plant becomes a candidate.
2. **Timing clue.** When a plant is on an outage it stops bidding. We build each anonymous bidder's calendar of "went-quiet" days and compare it to each named plant's outage calendar. When the two calendars line up unusually well, that's a fingerprint.

We score the timing overlap with a standard statistic — the **Matthews correlation (φ)** — which runs from 0 (no better than chance) to 1 (perfect match). Plants that are always on, or always off, score near 0, so only genuinely *distinctive* patterns count.

**Overall confidence = 60% timing match + 25% size match + 15% same type (battery vs. not).** A high-confidence match is a strong lead worth verifying — not courtroom proof.

**Forced vs. planned outages.** A plant goes quiet during *any* outage — unexpected (**forced**) or scheduled (**planned**). Use the toggle below to match timing against forced outages only, or forced + planned. Adding planned outages stops penalizing a bidder for going quiet during scheduled maintenance, and can surface plants whose 2025 outages were mostly planned.

**Magnitude-aware (partial curtailments).** Most outages aren't full shut-downs — the plant loses only *part* of its capacity, and a bidder on a partial outage offers *proportionally* less. The third method correlates the **size** of a plant's daily curtailment against **how much the bidder scaled back its offers** (a rank correlation, ρ), then blends it in: *confidence = 0.35·timing + 0.30·size-tracking + 0.25·capacity + 0.10·type*. This reaches plants that only ever partially derate — invisible to the on/off methods. Because timing is weighted less here, the three methods are best read as **complementary**, not strictly ranked.
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
        f"{int((r1['confidence'] >= 0.6).sum()):,}",
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
        style(fig, height=360, legend=False, xtitle="match confidence", ytitle="number of bidders")
        st.plotly_chart(fig, use_container_width=True)

    with right:
        section(
            "The strongest unmaskings", "Each anonymous bidder → the real plant it most likely is."
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
            "bidder_cap": "Offered cap (MW)",
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
        show = r1.sort_values("confidence", ascending=False).head(25)[cols].rename(columns=rename)
        st.dataframe(show, width="stretch", height=360, hide_index=True)

    st.markdown("---")
    section(
        "See the fingerprint for one match",
        "The giveaway: an anonymous bidder's offered power drops to nothing on exactly the days its matched real plant was broken down.",
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
        "bidder offers / plant capacity",
    )
    has_rho = is_mag and "rho" in link.index and pd.notna(link.get("rho"))
    if has_rho:
        kpi(
            k[3],
            "Timing φ · Size-track ρ",
            f"{link['phi']:.2f} · {link['rho']:.2f}",
            "0–1 each; ρ uses partial curtailments",
            tone="critical" if max(link["phi"], link["rho"]) > 0.6 else "serious",
        )
    else:
        kpi(
            k[3],
            "Timing match (φ)",
            f"{link['phi']:.2f}",
            "0 = chance, 1 = perfect",
            tone="critical" if link["phi"] > 0.6 else "serious",
        )
    kpi(k[4], "Overall confidence", f"{link['confidence']:.0%}", tone="critical")

    bhr = load("bidder_hourly_cap.parquet")
    bd = bhr[bhr["res"] == sel].copy()
    bd["h"] = pd.to_datetime(bd["h"])
    bd = bd.sort_values("h")

    gran = st.radio(
        "Break points",
        ["Hourly", "Daily"],
        index=0,
        horizontal=True,
        key="reid_granularity",
        help="Hourly shows every individual offer submission; Daily shows each day's peak offer.",
    )
    if gran == "Daily":
        agg = bd.assign(day=bd["h"].dt.floor("D")).groupby("day", as_index=False)["cap"].max()
        xvals, yvals = agg["day"], agg["cap"]
        line_hover = "%{x|%b %d}: %{y:.1f} MW (day's peak offer)<extra></extra>"
        mk_size = 4
    else:
        xvals, yvals = bd["h"], bd["cap"]
        line_hover = "%{x|%b %d, %H:%M}: %{y:.1f} MW offered<extra></extra>"
        mk_size = 3

    if len(yvals):
        ref = float(yvals.median())
        y0, y1 = float(yvals.min()), float(yvals.max())
    else:
        ref, y0, y1 = 0.0, 0.0, 1.0
    pad = max(0.5, (y1 - y0) * 0.08)
    y0, y1 = y0 - pad, y1 + pad
    DAY_MS = 86400000  # 1 day width for the outage bars

    fig = go.Figure()

    # matched plant's outage days as HOVERABLE bars (full height): forced=red,
    # planned=amber. Hover shows the day's curtailment in MW and % of PMAX.
    n_forced = n_planned = 0

    def outage_bars(df, color, name, kindlabel):
        if not len(df):
            return
        pct = (df["curt_mw"] / df["pmax"] * 100).where(df["pmax"] > 0)
        cust = [[mw, p] for mw, p in zip(df["curt_mw"].fillna(0), pct.fillna(-1))]
        fig.add_trace(
            go.Bar(
                x=df["day"],
                y=[y1 - y0] * len(df),
                base=y0,
                width=DAY_MS,
                marker=dict(color=color, line=dict(width=0)),
                name=name,
                customdata=cust,
                hovertemplate=(
                    "%{x|%b %d, %Y}<br>"
                    + kindlabel
                    + " outage<br>Curtailed: %{customdata[0]:.0f} MW"
                    " (%{customdata[1]:.0f}% of PMAX)<extra></extra>"
                ),
            )
        )

    if have("resource_outage_daily.parquet"):
        rod = load("resource_outage_daily.parquet")
        od = rod[rod["rid"] == link["cand_rid"]].copy()
        od["day"] = pd.to_datetime(od["day"])
        forced = od[od["kind"] == "forced"]
        planned = od[od["kind"] == "planned"] if use_planned else od.iloc[0:0]
        outage_bars(forced, "rgba(208,59,59,0.22)", "Matched plant — forced outage", "Forced")
        outage_bars(planned, "rgba(237,161,0,0.26)", "Matched plant — planned outage", "Planned")
        n_forced, n_planned = len(forced), len(planned)

    # bidder's offered capacity — hourly break points, or daily peak (toggle above)
    fig.add_trace(
        go.Scatter(
            x=xvals,
            y=yvals,
            mode="lines+markers",
            name="Anonymous bidder — offered capacity",
            line=dict(color=BLUE, width=1.3),
            marker=dict(size=mk_size, color=BLUE, line=dict(width=0)),
            hovertemplate=line_hover,
        )
    )
    fig.add_hline(
        y=ref,
        line=dict(color=MUTED, width=1, dash="dot"),
        annotation_text="typical",
        annotation_font_size=10,
        annotation_font_color=MUTED,
    )
    fig.update_layout(bargap=0)
    style(fig, height=380, ytitle="offered capacity (MW)")
    fig.update_yaxes(range=[y0, y1])
    _dots = (
        " Each blue dot is one hour the bidder submitted an offer; gaps are hours with no entry."
        if gran == "Hourly"
        else " Each blue dot is a day the bidder submitted offers (its peak that day); gaps are days with no entry."
    ) + " Hover an outage band to see how many MW the plant was curtailed."
    if use_planned:
        st.caption(
            "🟥 Red = forced (unexpected) outages · 🟧 Amber = planned (scheduled) outages. "
            "The bidder's offered capacity drops on those same days — that lined-up pattern is the fingerprint."
            + _dots
        )
    else:
        st.caption(
            "🟥 Red bands mark days the matched real plant was on a forced (unexpected) outage. "
            "The bidder's offered capacity drops on those same days — that lined-up pattern is the fingerprint."
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
# PAGE 4 — METHOD & ASSUMPTIONS
# =====================================================================
else:
    st.markdown("## How this works & what it can't tell you")
    st.caption(
        "Worth reading before you share these results. Both screens produce **leads to investigate**, "
        "not proof of wrongdoing."
    )

    t = META["thresholds"]
    c = st.columns(2)
    with c[0]:
        section("The settings behind the screens")
        st.markdown(f"""
| Setting | Value | In plain terms |
|---|---|---|
| "High price" cutoff | **\\${t["elevated_price"]:.0f}/MWh** | Offers at or above this count as steeply priced (a typical price is ~\\$32). |
| "Near-maximum" cutoff | **\\${t["nearcap_price"]:.0f}/MWh** | Close to the market's ~\\$1,000 price ceiling. |
| "Grid is short" line | **top {int((1 - t["tight_percentile"]) * 100)}%** of hours | Hours with ≥ {t["tight_mw"]:,.0f} MW of capacity offline. |
| Min. short hours to score | **{t["min_tight_hours"]}** | A plant needs enough short-hour activity to be judged fairly. |
| Size-match tolerance | **±{t["cap_tolerance_pct"]:.0f}%** | How close a bidder's ceiling must be to a plant's capacity to be a candidate. |
""")
    with c[1]:
        section("What's in the data")
        st.markdown(f"""
| | |
|---|---|
| Source | {META["generated_scope"]} |
| Price offers | {META["n_bid_rows"]:,} |
| Anonymous bidders | {META["n_resources"]:,} |
| Dates | {META["date_min"]} → {META["date_max"]} |
| Plants scored for withholding | {META["withholding_scored"]:,} |
| Confident unmaskings | {META["reident_highconf_links"]:,} |
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
- **φ (phi), the Matthews correlation** — a 0-to-1 score for how well two on/off calendars line up (0 = chance, 1 = perfect).
- **Ancillary services** — backup and stability services (like standby reserves) the grid buys on top of plain energy.
""")
    st.caption(
        "Built from public CAISO data • colors checked for color-blind readability • no outside data used."
    )
