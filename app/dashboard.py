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
import os, json
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

HERE = os.path.dirname(os.path.abspath(__file__))
DER  = os.path.join(HERE, "data", "derived")

# ---- validated dataviz palette (fixed categorical order; sequential blue) ----
CAT = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
BLUE, AQUA, YELLOW, GREEN, VIOLET, RED, MAGENTA, ORANGE = CAT
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
STATUS = dict(good="#0ca30c", warning="#fab219", serious="#ec835a", critical="#d03b3b")
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, SURF = "#e1e0d9", "#fcfcfb"

st.set_page_config(page_title="CAISO Market Surveillance", page_icon="⚡",
                   layout="wide", initial_sidebar_state="expanded")

# ---------- data loading ----------
@st.cache_data(show_spinner=False)
def load(name):
    return pd.read_parquet(os.path.join(DER, name))

@st.cache_data(show_spinner=False)
def load_meta():
    with open(os.path.join(DER, "meta.json")) as f:
        return json.load(f)

def have(name):
    return os.path.exists(os.path.join(DER, name))

if not have("meta.json"):
    st.error("Derived data not found. Run `python pipeline.py` first.")
    st.stop()

META = load_meta()

# ---------- styling helpers ----------
def style(fig, height=360, legend=True, ytitle=None, xtitle=None):
    fig.update_layout(
        template="plotly_white", height=height,
        paper_bgcolor=SURF, plot_bgcolor=SURF,
        font=dict(family="system-ui,-apple-system,Segoe UI,sans-serif", color=INK2, size=13),
        margin=dict(l=10, r=16, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title_text="",
                    bgcolor="rgba(0,0,0,0)") if legend else dict(),
        showlegend=legend,
        hoverlabel=dict(bgcolor="white", font_size=12),
    )
    fig.update_xaxes(showgrid=False, linecolor="#c3c2b7", ticks="outside",
                     tickcolor=GRID, color=MUTED, title_text=xtitle or "")
    fig.update_yaxes(showgrid=True, gridcolor=GRID, zeroline=False, color=MUTED,
                     title_text=ytitle or "", title_font=dict(size=12))
    return fig

def kpi(col, label, value, help=None, tone=None):
    color = STATUS.get(tone, INK)
    col.markdown(
        f"""<div style="background:{SURF};border:1px solid rgba(11,11,11,.08);
        border-radius:12px;padding:14px 16px;">
        <div style="font-size:12px;color:{MUTED};text-transform:uppercase;letter-spacing:.04em">{label}</div>
        <div style="font-size:26px;font-weight:700;color:{color};line-height:1.15;margin-top:4px">{value}</div>
        <div style="font-size:12px;color:{MUTED};margin-top:2px">{help or ''}</div></div>""",
        unsafe_allow_html=True)

def section(title, subtitle=None):
    st.markdown(f"### {title}")
    if subtitle:
        st.caption(subtitle)

# ---------- sidebar ----------
st.sidebar.markdown("## ⚡ CAISO Surveillance")
st.sidebar.caption(META["generated_scope"])
PAGE = st.sidebar.radio("View", [
    "Market Overview",
    "Economic Withholding",
    "Re-identification",
    "Method & Assumptions",
], label_visibility="collapsed")
st.sidebar.markdown("---")
st.sidebar.metric("Bid records", f'{META["n_bid_rows"]/1e6:.1f} M')
st.sidebar.metric("Anonymous resources", f'{META["n_resources"]:,}')
st.sidebar.caption(f'Coverage: {META["date_min"]} → {META["date_max"]}')

# =====================================================================
# PAGE 1 — MARKET OVERVIEW
# =====================================================================
if PAGE == "Market Overview":
    st.markdown("## Market Overview")
    st.caption("Where the market got tight and where high-priced ('near-cap') energy offers "
               "clustered — the backdrop for the surveillance screens.")

    md = load("market_daily.parquet")
    md["day"] = pd.to_datetime(md["day"])
    wh = load("withholding_resource.parquet")

    c = st.columns(5)
    kpi(c[0], "Bid records", f'{META["n_bid_rows"]/1e6:.1f} M', "energy + A/S bids, 2025")
    kpi(c[1], "Resources", f'{META["n_resources"]:,}', "anonymous bidders")
    flagged = int((wh["withholding_index"] > 0.05).sum())
    kpi(c[2], "Withholding flags", f"{flagged:,}",
        "index > 0.05", tone="serious" if flagged else None)
    hc = META.get("reident_highconf_links", 0)
    kpi(c[3], "Re-identified", f"{hc:,}",
        "≥0.60 confidence links", tone="critical" if hc else None)
    kpi(c[4], "Peak tightness", f'{md["peak_tight_mw"].max()/1000:.1f} GW',
        "max forced-outage MW")

    st.markdown("")
    left, right = st.columns([3, 2])

    with left:
        section("Near-cap energy offers over the year",
                "Share of offered energy priced ≥ $%d/MWh (near the $%d bid cap). Spikes = capacity "
                "parked at prices unlikely to clear." % (META["thresholds"]["nearcap_price"], 1000))
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=md["day"], y=md["near_share"]*100, mode="lines", name="Near-cap share",
            line=dict(color=BLUE, width=2), fill="tozeroy",
            fillcolor="rgba(42,120,214,0.10)",
            hovertemplate="%{x|%b %d}<br>Near-cap share: %{y:.1f}%<extra></extra>"))
        style(fig, height=340, legend=False, ytitle="% of offered MW")
        st.plotly_chart(fig, width='stretch')

    with right:
        section("System tightness", "Daily peak forced-outage MW (our scarcity proxy — no LMP in source).")
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=md["day"], y=md["peak_tight_mw"]/1000, mode="lines", name="Peak outage GW",
            line=dict(color=ORANGE, width=2), fill="tozeroy",
            fillcolor="rgba(235,104,52,0.10)",
            hovertemplate="%{x|%b %d}<br>Peak forced-outage: %{y:.2f} GW<extra></extra>"))
        thr = META["thresholds"]["tight_mw"]/1000
        fig.add_hline(y=thr, line=dict(color=STATUS["serious"], width=1, dash="dot"),
                      annotation_text=f"'tight' P90 = {thr:.1f} GW",
                      annotation_font_color=MUTED, annotation_font_size=11)
        style(fig, height=340, legend=False, ytitle="GW offline")
        st.plotly_chart(fig, width='stretch')

    section("Bid volume by product type",
            "Monthly counts. Energy (EN) dominates; ancillary services (regulation, spinning/non-spinning reserve) follow.")
    pm = load("product_monthly.parquet")
    pm["month"] = pd.to_datetime(pm["month"])
    order = pm.groupby("product")["n_bids"].sum().sort_values(ascending=False).index.tolist()
    fig = go.Figure()
    for i, prod in enumerate(order[:8]):
        d = pm[pm["product"] == prod]
        fig.add_trace(go.Bar(x=d["month"], y=d["n_bids"], name=prod,
                             marker_color=CAT[i % 8],
                             hovertemplate=f"{prod}<br>%{{x|%b %Y}}: %{{y:,}} bids<extra></extra>"))
    fig.update_layout(barmode="stack", bargap=0.25)
    style(fig, height=340, ytitle="bids / month")
    st.plotly_chart(fig, width='stretch')

# =====================================================================
# PAGE 2 — ECONOMIC WITHHOLDING
# =====================================================================
elif PAGE == "Economic Withholding":
    st.markdown("## Economic-Withholding Screen")
    st.caption("Flags resources that shift capacity to high prices **specifically when the system is tight** — "
               "the behavioral signature of economic withholding, measured without any cost data.")

    with st.expander("How this screen works", expanded=False):
        t = META["thresholds"]
        st.markdown(f"""
- **Tight hours** = hours in the top {int((1-t['tight_percentile'])*100)}% of system forced-outage MW (≥ **{t['tight_mw']:,.0f} MW** offline). No LMP exists in the data, so outages proxy scarcity.
- For each resource we compute the share of its offered energy priced ≥ **\\${t['elevated_price']:.0f}/MWh** ("elevated"), separately in **tight** vs **normal** hours.
- **Withholding index = elevated-share(tight) − elevated-share(normal).** A large positive value means the resource withholds more precisely when the grid can least afford it.
- Only resources active ≥ {t['min_tight_hours']} tight hours are scored ({META['withholding_scored']:,} resources).
- This is a *conduct screen* (a lead, not proof) — the standard next step is an impact test against actual clearing prices.
""")

    wh = load("withholding_resource.parquet")
    wd = load("withholding_daily.parquet")
    wd["day"] = pd.to_datetime(wd["day"])

    flagged = int((wh["withholding_index"] > 0.05).sum())
    c = st.columns(4)
    kpi(c[0], "Resources scored", f'{len(wh):,}')
    kpi(c[1], "Flagged (index>0.05)", f"{flagged:,}", tone="serious")
    kpi(c[2], "Strong (index>0.20)", f'{int((wh["withholding_index"]>0.20).sum()):,}', tone="critical")
    kpi(c[3], "Top index", f'{wh["withholding_index"].max():.2f}',
        f'resource #{int(wh.iloc[0]["res"])}')

    left, right = st.columns(2)
    with left:
        section("Top 20 flagged resources", "Ranked by withholding index (higher = more tight-hour withholding).")
        top = wh.head(20).iloc[::-1]
        fig = go.Figure(go.Bar(
            x=top["withholding_index"], y=top["res"].astype(str), orientation="h",
            marker_color=BLUE, marker_line_width=0,
            customdata=top[["nearcap_mwh_tight", "cap_max"]].values,
            hovertemplate="Resource %{y}<br>Index: %{x:.3f}"
                          "<br>Near-cap MWh (tight): %{customdata[0]:,.0f}"
                          "<br>Max cap: %{customdata[1]:.0f} MW<extra></extra>"))
        fig.update_yaxes(type="category", title_text="resource seq")
        style(fig, height=460, legend=False, xtitle="withholding index")
        st.plotly_chart(fig, width='stretch')

    with right:
        section("Tight vs. normal behavior", "Above the diagonal ⇒ withholds more when system is tight. "
                "Point size = near-cap MWh offered during tight hours.")
        d = wh.copy()
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=d["hi_share_normal"]*100, y=d["hi_share_tight"]*100, mode="markers",
            marker=dict(size=(d["nearcap_mwh_tight"].clip(lower=1)**0.5)/30+6,
                        color=d["withholding_index"], colorscale=[[0, SEQ_BLUE[0]], [1, SEQ_BLUE[5]]],
                        showscale=True, colorbar=dict(title="index", thickness=10, len=0.6),
                        line=dict(width=0.5, color="white")),
            customdata=d[["res", "withholding_index"]].values,
            hovertemplate="Resource %{customdata[0]}<br>elevated tight: %{y:.0f}%"
                          "<br>elevated normal: %{x:.0f}%<br>index: %{customdata[1]:.3f}<extra></extra>",
            name=""))
        fig.add_shape(type="line", x0=0, y0=0, x1=100, y1=100,
                      line=dict(color=MUTED, width=1, dash="dash"))
        style(fig, height=460, legend=False,
              xtitle="elevated share — normal hours (%)", ytitle="elevated share — tight hours (%)")
        st.plotly_chart(fig, width='stretch')

    st.markdown("---")
    section("Resource drill-down", "Daily offer behavior for a flagged resource. Red markers = days containing a system-tight hour.")
    ids = wd["res"].unique().tolist()
    sel = st.selectbox("Resource (top offenders with daily detail)", ids,
                       format_func=lambda r: f"#{int(r)}  —  rank {int(wh[wh.res==r]['rank'].iloc[0])}, index {wh[wh.res==r]['withholding_index'].iloc[0]:.3f}")
    d = wd[wd["res"] == sel].sort_values("day")
    row = wh[wh.res == sel].iloc[0]
    k = st.columns(4)
    kpi(k[0], "Rank", f'{int(row["rank"])}')
    kpi(k[1], "Withholding index", f'{row["withholding_index"]:.3f}', tone="critical" if row["withholding_index"]>0.2 else "serious")
    kpi(k[2], "Elevated share (tight)", f'{row["hi_share_tight"]*100:.0f}%')
    kpi(k[3], "Elevated share (normal)", f'{row["hi_share_normal"]*100:.0f}%')

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=d["day"], y=d["hi_share"]*100, mode="lines",
                             line=dict(color=BLUE, width=1.5), name="Elevated share",
                             hovertemplate="%{x|%b %d}: %{y:.0f}%<extra></extra>"))
    td = d[d["had_tight_hour"] == 1]
    fig.add_trace(go.Scatter(x=td["day"], y=td["hi_share"]*100, mode="markers",
                             marker=dict(color=STATUS["critical"], size=6),
                             name="Tight day",
                             hovertemplate="%{x|%b %d} (tight): %{y:.0f}%<extra></extra>"))
    style(fig, height=340, ytitle="% offered ≥ elevated price")
    st.plotly_chart(fig, width='stretch')

    with st.expander("Full ranked table + CSV export"):
        show = wh[["rank","res","sc","cap_max","tight_hours","hi_share_tight",
                   "hi_share_normal","withholding_index","nearcap_mwh_tight","is_storage"]].copy()
        st.dataframe(show, width='stretch', height=320)
        st.download_button("⬇ Download withholding results (CSV)",
                           wh.to_csv(index=False), "caiso_withholding_2025.csv", "text/csv")

# =====================================================================
# PAGE 3 — RE-IDENTIFICATION
# =====================================================================
elif PAGE == "Re-identification":
    st.markdown("## Re-identification / Fingerprinting")
    st.caption("How much the anonymization leaks: linking an anonymous **RESOURCEBID_SEQ** to a named plant "
               "using two public side-channels — **offered capacity** and **outage timing**.")

    with st.expander("How this screen works", expanded=False):
        t = META["thresholds"]
        st.markdown(f"""
Anonymous bidders have no names, but two public signals leak identity:
1. **Capacity fingerprint** — a bidder's offered MW ceiling ≈ a named resource's PMAX (matched within ±{t['cap_tolerance_pct']:.0f}%).
2. **Outage-timing fingerprint** — when a plant is on a *forced outage* it stops bidding. We build the bidder's daily "drop-out" pattern and the named resource's daily outage pattern, then score their coincidence with the **Matthews correlation (φ)** over the active span.

**Confidence = 0.60·φ + 0.25·capacity-closeness + 0.15·storage-class-match.** Trivial always-on patterns collapse to φ≈0, so only *distinctive* timing coincidences score. A high-confidence link is an investigative lead, not proof.
""")

    if not have("reident_matches.parquet"):
        st.warning("No re-identification results available.")
        st.stop()

    m = load("reident_matches.parquet")
    r1 = m[m["rank"] == 1].copy()
    prof = load("bidder_profiles.parquet")

    c = st.columns(4)
    kpi(c[0], "Fingerprint-able bidders", f'{r1["res"].nunique():,}', "have a usable drop-out pattern")
    kpi(c[1], "High-confidence links", f'{int((r1["confidence"]>=0.6).sum()):,}', "≥0.60", tone="critical")
    kpi(c[2], "Very strong links", f'{int((r1["confidence"]>=0.75).sum()):,}', "≥0.75", tone="critical")
    kpi(c[3], "Named plants implicated", f'{r1[r1.confidence>=0.6]["cand_name"].nunique():,}')

    left, right = st.columns([2, 3])
    with left:
        section("Confidence distribution", "Best (rank-1) candidate per bidder.")
        fig = go.Figure(go.Histogram(
            x=r1["confidence"], nbinsx=24, marker_color=BLUE, marker_line_width=0,
            hovertemplate="conf %{x:.2f}<br>%{y} bidders<extra></extra>"))
        fig.add_vline(x=0.6, line=dict(color=STATUS["critical"], width=1.5, dash="dot"),
                      annotation_text="high-conf", annotation_font_color=MUTED, annotation_font_size=11)
        style(fig, height=360, legend=False, xtitle="confidence", ytitle="bidders")
        st.plotly_chart(fig, width='stretch')

    with right:
        section("Strongest identity links", "Anonymous bidder → most likely named plant.")
        show = r1.sort_values("confidence", ascending=False).head(25)[
            ["res","bidder_cap","cand_name","cand_pmax","cap_diff_pct","phi","overlap_days","confidence"]]
        show = show.rename(columns={"res":"bidder","bidder_cap":"cap(MW)","cand_name":"likely plant",
                                    "cand_pmax":"PMAX","cap_diff_pct":"cap Δ%","phi":"φ",
                                    "overlap_days":"outage∩drop"})
        st.dataframe(show, width='stretch', height=360, hide_index=True)

    st.markdown("---")
    section("Fingerprint drill-down",
            "The 'aha': a bidder's offered capacity collapses on exactly the days its matched plant is on forced outage.")
    r1s = r1.sort_values("confidence", ascending=False)
    sel = st.selectbox(
        "Identity link", r1s["res"].tolist(),
        format_func=lambda r: f'#{int(r)} → {r1s[r1s.res==r]["cand_name"].iloc[0]}  '
                              f'(conf {r1s[r1s.res==r]["confidence"].iloc[0]:.2f})')
    link = r1s[r1s.res == sel].iloc[0]

    k = st.columns(5)
    kpi(k[0], "Anon bidder", f'#{int(link["res"])}')
    kpi(k[1], "Likely plant", link["cand_name"])
    kpi(k[2], "Capacity", f'{link["bidder_cap"]:.1f} / {link["cand_pmax"]:.1f} MW', "bidder / PMAX")
    kpi(k[3], "Timing φ", f'{link["phi"]:.2f}', tone="critical" if link["phi"]>0.6 else "serious")
    kpi(k[4], "Confidence", f'{link["confidence"]:.2f}', tone="critical")

    bday = load("bidder_daily_cap.parquet")
    bd = bday[bday["res"] == sel].copy()
    bd["day"] = pd.to_datetime(bd["day"])
    bd = bd.sort_values("day")
    ref = bd["cap"].median()

    fig = go.Figure()
    # shade the matched plant's forced-outage days
    if have("resource_outage_daily.parquet"):
        rod = load("resource_outage_daily.parquet")
        od = rod[rod["rid"] == link["cand_rid"]].copy()
        od["day"] = pd.to_datetime(od["day"])
        ymax = max(bd["cap"].max(), ref) * 1.1
        first = True
        for dday in od["day"]:
            fig.add_vrect(x0=dday - pd.Timedelta(hours=12), x1=dday + pd.Timedelta(hours=12),
                          fillcolor="rgba(208,59,59,0.14)", line_width=0, layer="below",
                          annotation_text="outage" if first else None,
                          annotation_font_size=10, annotation_font_color=STATUS["critical"])
            first = False
    fig.add_trace(go.Scatter(
        x=bd["day"], y=bd["cap"], mode="lines", name="Offered MW (bidder)",
        line=dict(color=BLUE, width=1.6),
        hovertemplate="%{x|%b %d}: %{y:.1f} MW offered<extra></extra>"))
    fig.add_hline(y=ref, line=dict(color=MUTED, width=1, dash="dot"),
                  annotation_text="typical", annotation_font_size=10, annotation_font_color=MUTED)
    style(fig, height=380, ytitle="offered capacity (MW)")
    st.caption("🟥 Red bands = days the matched named plant was on a **forced outage**. "
               "The bidder's offered capacity drops out on those same days — the identity fingerprint.")
    st.plotly_chart(fig, width='stretch')

    with st.expander("Full candidate table + CSV export"):
        st.dataframe(m, width='stretch', height=320)
        st.download_button("⬇ Download re-identification links (CSV)",
                           m.to_csv(index=False), "caiso_reidentification_2025.csv", "text/csv")

# =====================================================================
# PAGE 4 — METHOD & ASSUMPTIONS
# =====================================================================
else:
    st.markdown("## Method & Assumptions")
    st.caption("Read before presenting. These screens produce **investigative leads**, not findings of manipulation.")

    t = META["thresholds"]
    c = st.columns(2)
    with c[0]:
        section("Thresholds")
        st.markdown(f"""
| Parameter | Value |
|---|---|
| Elevated-price cutoff | **${t['elevated_price']:.0f}/MWh** |
| Near-cap cutoff | **${t['nearcap_price']:.0f}/MWh** (cap ≈ $1000) |
| System-tight percentile | **P{int(t['tight_percentile']*100)}** ({t['tight_mw']:,.0f} MW offline) |
| Min tight-hours to score | **{t['min_tight_hours']}** |
| Capacity match tolerance | **±{t['cap_tolerance_pct']:.0f}%** |
""")
    with c[1]:
        section("Coverage")
        st.markdown(f"""
| | |
|---|---|
| Scope | {META['generated_scope']} |
| Bid records | {META['n_bid_rows']:,} |
| Anonymous resources | {META['n_resources']:,} |
| Dates | {META['date_min']} → {META['date_max']} |
| Withholding scored | {META['withholding_scored']:,} |
| High-confidence re-ID links | {META['reident_highconf_links']:,} |
""")

    section("Key assumptions & caveats")
    for a in META["assumptions"]:
        st.markdown(f"- {a}")
    st.markdown(f"""
- **Leads, not proof.** Both screens are *conduct/structural* screens. Confirmation requires the identified,
  real-time data (LMPs, metered output, market-power mitigation logs) held by the market monitor.
- **Withholding** here is self-referential (tight vs. normal behavior). It cannot distinguish legitimate
  scarcity pricing from abuse without a marginal-cost benchmark.
- **Re-identification** confirms the anonymization is *reversible* for resources with distinctive outage
  timing — itself a finding worth reporting to whoever owns the data-release policy.
""")
    st.caption("Generated by pipeline.py • palette validated for CVD safety • no external data used.")
