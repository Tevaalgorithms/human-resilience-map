"""Insight charts (pure functions: data in, figure + table twin out).

Design rules (see docs: data-viz method): colour does one job per chart and is validated against
the actual card surfaces; thin marks; hairline recessive grid; selective direct labels; legend only
for 2+ series; every chart ships a table-view twin; tooltips enhance but never gate; light and dark
palettes are separate, validated selections (not an automatic flip).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import plotly.graph_objects as go

from resilience_map.risk.scoring import BANDS, band_for

# ---- palettes: each slot validated (contrast, CVD, ordinal monotonicity) on the card surface ----
PAL: dict[str, dict[str, Any]] = {
    "light": {
        "surface": "#ffffff",
        "ink": "#14202b",
        "ink2": "#566676",
        "muted": "#7d8a97",
        "grid": "#e6eaee",
        "axis": "#c3cbd3",
        "series1": "#2a78d6",
        "series2": "#eb6834",
        "hot": "#e34948",
        "cool": "#2a78d6",  # diverging poles (neutral midpoint = zero baseline)
        # ordinal ramp, lower -> higher vulnerability (light -> dark)
        "bands": {
            "Lower vulnerability": "#86b6ef",
            "Moderate": "#3987e5",
            "Higher vulnerability": "#184f95",
        },
        # sequential ramp for density cells: low count recedes into the surface, high count is dark
        "seq": ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"],
    },
    "dark": {
        "surface": "#262626",
        "ink": "#f4f4f4",
        "ink2": "#c6c6c6",
        "muted": "#a8a8a8",
        "grid": "#363636",
        "axis": "#525252",
        "series1": "#3987e5",
        "series2": "#d95926",
        "hot": "#e66767",
        "cool": "#3987e5",
        # anchor flips in dark: more vulnerable = lighter. Steps 550/400/250 clear the 2:1 floor on #262626.
        "bands": {
            "Lower vulnerability": "#1c5cab",
            "Moderate": "#3987e5",
            "Higher vulnerability": "#86b6ef",
        },
        # density ramp: the lightest-to-surface cell (1 tract) must stay visible, so it starts at step 550
        "seq": ["#1c5cab", "#256abf", "#3987e5", "#5598e7", "#6da7ec", "#9ec5f4", "#cde2fb"],
    },
}
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"


@dataclass
class ChartSpec:
    key: str
    title: str
    subtitle: str
    fig: go.Figure | None
    height: int
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    note: str = ""
    empty: str = ""  # shown instead of the chart when there is not enough data


# --------------------------------------------------------------------------------------------
def _layout(
    theme: str, height: int, *, left: int = 8, right: int = 8, top: int = 8, bottom: int = 8
) -> dict[str, Any]:
    p = PAL[theme]
    return {
        "height": height, "margin": {"l": left, "r": right, "t": top, "b": bottom}, "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)", "font": {"family": FONT, "size": 12, "color": p["ink2"]},
        "showlegend": False, "barcornerradius": 4,
        "hoverlabel": {"bgcolor": p["surface"], "bordercolor": p["axis"],
                       "font": {"family": FONT, "size": 12, "color": p["ink"]}},
    }  # fmt: skip


def _axes(fig: go.Figure, theme: str, *, x_grid: bool = False, y_grid: bool = True) -> None:
    p = PAL[theme]
    common = {"linecolor": p["axis"], "tickfont": {"color": p["muted"], "size": 11}, "ticks": "",
              "zeroline": False}  # fmt: skip
    fig.update_xaxes(showgrid=x_grid, gridcolor=p["grid"], gridwidth=1, **common)
    fig.update_yaxes(showgrid=y_grid, gridcolor=p["grid"], gridwidth=1, **common)


def _compact(n: float) -> str:
    n = float(n)
    if abs(n) >= 1e6:
        return f"{n / 1e6:.1f}M"
    if abs(n) >= 1e4:
        return f"{n / 1e3:.0f}K"
    return f"{n:,.0f}"


def _props(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f["properties"] for f in features]


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    tot = sum(weights)
    return sum(v * w for v, w in zip(values, weights, strict=True)) / tot if tot else float("nan")


# --------------------------------------------------------------------------------------------
# 1. Who is exposed: residents by resilience band (ordinal ramp, one hue)
# --------------------------------------------------------------------------------------------
def band_chart(features: list[dict[str, Any]], theme: str) -> ChartSpec:
    p, order = PAL[theme], ["Higher vulnerability", "Moderate", "Lower vulnerability"]
    scored = [x for x in _props(features) if x["score"] is not None]
    pops = dict.fromkeys(order, 0)
    tracts = dict.fromkeys(order, 0)
    for x in scored:
        b = band_for(x["score"])
        pops[b] += x["population"]
        tracts[b] += 1
    total = sum(pops.values())
    if total == 0:
        return ChartSpec(
            "bands",
            "Residents by resilience band",
            "",
            None,
            240,
            empty="No scored tracts in this view.",
        )
    share = {b: 100 * pops[b] / total for b in order}
    fig = go.Figure(
        go.Bar(
            y=order,
            x=[pops[b] for b in order],
            orientation="h",
            marker={"color": [p["bands"][b] for b in order], "line": {"width": 0}},
            text=[f"{_compact(pops[b])} · {share[b]:.0f}%" for b in order],
            textposition="outside",
            cliponaxis=False,
            textfont={"color": p["ink"], "size": 12},
            customdata=[[tracts[b], share[b]] for b in order],
            hovertemplate="<b>%{x:,.0f} residents</b> (%{customdata[1]:.1f}%)<br>%{y}<br>%{customdata[0]:,} tracts<extra></extra>",
        )
    )
    fig.update_layout(**_layout(theme, 230, left=8, right=70, top=6, bottom=30), bargap=0.55)
    _axes(fig, theme, x_grid=True, y_grid=False)
    fig.update_yaxes(autorange="reversed", tickfont={"color": p["ink2"], "size": 12})
    fig.update_xaxes(range=[0, max(pops.values()) * 1.25], tickformat="~s", rangemode="tozero")
    lo, hi = BANDS[0][0], BANDS[1][0]
    return ChartSpec(
        "bands", f"{share['Higher vulnerability']:.0f}% of residents live in higher-vulnerability tracts",
        f"Residents by resilience band, scored tracts only. Higher vulnerability < {lo:g}, moderate {lo:g}-{hi:g}, lower ≥ {hi:g}.",
        fig, 230, ["Band", "Residents", "Share", "Tracts"],
        [[b, f"{pops[b]:,}", f"{share[b]:.1f}%", f"{tracts[b]:,}"] for b in order],
        "Bands are illustrative planning cut-offs, not validated thresholds.",
    )  # fmt: skip


# --------------------------------------------------------------------------------------------
# 2. Equity: heat anomaly by income fifth (diverging: above / below the county median)
# --------------------------------------------------------------------------------------------
def equity_chart(features: list[dict[str, Any]], theme: str) -> ChartSpec:
    p = PAL[theme]
    rows = [(x["indicators"]["income"], x["indicators"]["heat"], x["population"]) for x in _props(features)
            if x["indicators"].get("income") is not None and x["indicators"].get("heat") is not None and x["population"] > 0]  # fmt: skip
    if len(rows) < 25:
        return ChartSpec(
            "equity",
            "Heat by neighbourhood income",
            "",
            None,
            280,
            empty="Not enough tracts with both income and heat data in this view (need 25).",
        )
    rows.sort(key=lambda r: r[0])
    n = len(rows)
    labels = ["Lowest 20%", "2nd", "Middle", "4th", "Highest 20%"]
    means, ranges, counts = [], [], []
    for q in range(5):
        chunk = rows[q * n // 5 : (q + 1) * n // 5]
        means.append(_weighted_mean([c[1] for c in chunk], [c[2] for c in chunk]))
        ranges.append(f"${chunk[0][0] / 1000:.0f}K-${chunk[-1][0] / 1000:.0f}K")
        counts.append(len(chunk))
    gap = means[0] - means[-1]
    fig = go.Figure(
        go.Bar(
            x=labels,
            y=means,
            marker={
                "color": [p["hot"] if m >= 0 else p["cool"] for m in means],
                "line": {"width": 0},
            },
            text=[f"{m:+.1f}°" for m in means],
            textposition="outside",
            cliponaxis=False,
            textfont={"color": p["ink"], "size": 12},
            customdata=[[ranges[i], counts[i]] for i in range(5)],
            hovertemplate="<b>%{y:+.2f} °C</b> vs county median<br>%{x} income tracts (%{customdata[0]})<br>%{customdata[1]} tracts<extra></extra>",
        )
    )
    fig.update_layout(**_layout(theme, 280, left=44, right=8, top=24, bottom=30), bargap=0.7)
    _axes(fig, theme)
    span = max(abs(m) for m in means) or 1
    fig.update_yaxes(
        range=[min(0, min(means)) - span * 0.25, max(0, max(means)) + span * 0.3],
        ticksuffix="°",
        zeroline=True,
        zerolinecolor=p["axis"],
        zerolinewidth=1,
    )
    if abs(gap) >= 0.3:
        title = f"Lowest-income tracts are {abs(gap):.1f} °C {'hotter' if gap > 0 else 'cooler'} than the highest-income tracts"
    else:
        title = f"Little income gradient in surface heat here ({gap:+.1f} °C between the extremes)"
    return ChartSpec(
        "equity", title,
        "Average heat anomaly by income fifth of tracts (population-weighted). Red = hotter than the county median, blue = cooler.",
        fig, 280, ["Income fifth", "Income range", "Tracts", "Heat anomaly (°C)"],
        [[labels[i], ranges[i], f"{counts[i]:,}", f"{means[i]:+.2f}"] for i in range(5)],
        "Land-surface temperature, not air temperature. In counties with large elevation ranges the anomaly partly reflects elevation.",
    )  # fmt: skip


# --------------------------------------------------------------------------------------------
# 3. Relationship: canopy vs heat (2-D density grid + linear fit)
# --------------------------------------------------------------------------------------------
def relationship_chart(features: list[dict[str, Any]], theme: str) -> ChartSpec:
    p = PAL[theme]
    pts = [(x["indicators"]["canopy"], x["indicators"]["heat"]) for x in _props(features)
           if x["indicators"].get("canopy") is not None and x["indicators"].get("heat") is not None]  # fmt: skip
    if len(pts) < 25:
        return ChartSpec(
            "rel",
            "Tree canopy and surface heat",
            "",
            None,
            340,
            empty="Not enough tracts with both canopy and heat data in this view (need 25).",
        )
    xs, ys = [a for a, _ in pts], [b for _, b in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    r = sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0
    slope = sxy / sxx if sxx else 0.0
    ic = my - slope * mx

    # Focus on the bulk of the data: a few extreme tracts must not squash everything into a corner.
    def pct(vals: list[float], q: float) -> float:
        v = sorted(vals)
        return v[min(len(v) - 1, max(0, round(q * (len(v) - 1))))]

    x0, x1 = pct(xs, 0.01), pct(xs, 0.99)
    y0, y1 = pct(ys, 0.01), pct(ys, 0.99)
    x1, y1 = (x1 if x1 > x0 else x0 + 1), (y1 if y1 > y0 else y0 + 1)
    n_bins = 22
    # Empty cells stay transparent (count 0); from 1 tract upward the ramp starts at its light end.
    scale = [[0.0, "rgba(0,0,0,0)"], [1e-6, p["seq"][0]]] + [
        [(i + 1) / 6, c] for i, c in enumerate(p["seq"][1:])
    ]
    fig = go.Figure()
    fig.add_trace(
        go.Histogram2d(
            x=xs, y=ys, xbins={"start": x0, "end": x1, "size": (x1 - x0) / n_bins},
            ybins={"start": y0, "end": y1, "size": (y1 - y0) / n_bins}, xgap=2, ygap=2, colorscale=scale,
            hovertemplate="<b>%{z} tracts</b><br>Canopy %{x:.0f}% · heat %{y:+.1f} °C<extra></extra>",
            colorbar={"title": {"text": "Tracts", "font": {"size": 11, "color": p["ink2"]}}, "thickness": 8, "len": 0.55,
                      "outlinewidth": 0, "tickfont": {"size": 11, "color": p["muted"]}},
        )
    )  # fmt: skip
    fig.add_trace(go.Scatter(x=[x0, x1], y=[ic + slope * x0, ic + slope * x1], mode="lines", hoverinfo="skip",
                             line={"color": p["ink"], "width": 2}))  # fmt: skip
    fig.update_layout(**_layout(theme, 340, left=44, right=8, top=8, bottom=44))
    _axes(fig, theme)
    fig.update_xaxes(
        title={"text": "Tree canopy cover (%)", "font": {"size": 11, "color": p["ink2"]}},
        ticksuffix="%",
    )
    fig.update_yaxes(
        title={"text": "Heat anomaly (°C)", "font": {"size": 11, "color": p["ink2"]}},
        ticksuffix="°",
    )
    strength = "strongly" if abs(r) >= 0.6 else "moderately" if abs(r) >= 0.3 else "weakly"
    title = (f"More tree canopy goes with cooler surfaces (r = {r:.2f})" if r <= -0.3
             else f"Canopy and surface heat are {strength} related here (r = {r:.2f})")  # fmt: skip
    return ChartSpec(
        "rel", title,
        f"Each cell counts tracts (scale at right). Line = linear fit: {10 * slope:+.1f} °C for every +10 points of canopy.",
        fig, 340, ["Measure", "Value"],
        [["Tracts compared", f"{len(pts):,}"], ["Correlation (r)", f"{r:.2f}"], ["Fit slope (°C per canopy point)", f"{slope:+.3f}"]],
        "Axes show the 1st-99th percentile; the correlation uses all tracts. Correlation is not causation: canopy tracks income, building density and land use too.",
    )  # fmt: skip


# --------------------------------------------------------------------------------------------
# 4. Comparison: counties ranked by residents in higher-vulnerability tracts (emphasis)
# --------------------------------------------------------------------------------------------
def county_chart(counties: list[dict[str, Any]], current: str | None, theme: str) -> ChartSpec:
    p = PAL[theme]
    ranked = sorted(
        (c for c in counties if c.get("high_population")), key=lambda c: -c["high_population"]
    )[:8]
    if len(counties) < 2:
        return ChartSpec(
            "county",
            "County comparison",
            "",
            None,
            340,
            empty="This study area has a single county: nothing to compare.",
        )
    if not ranked:
        return ChartSpec(
            "county",
            "County comparison",
            "",
            None,
            340,
            empty="No county has tracts in the higher-vulnerability band.",
        )
    names = [c["name"].replace(" County", "") for c in ranked]
    fig = go.Figure(
        go.Bar(
            y=names, x=[c["high_population"] for c in ranked], orientation="h",
            marker={"color": [p["series1"] if c["county_id"] == current else p["axis"] for c in ranked], "line": {"width": 0}},  # emphasis: one hue + grey
            text=[_compact(c["high_population"]) for c in ranked], textposition="outside", cliponaxis=False,
            textfont={"color": p["ink"], "size": 12},
            customdata=[[c["high_tracts"], c["tracts"], c["population"]] for c in ranked],
            hovertemplate="<b>%{x:,.0f} residents</b> in higher-vulnerability tracts<br>%{y} County<br>"
                          "%{customdata[0]} of %{customdata[1]} tracts · %{customdata[2]:,} residents total<extra></extra>",
        )
    )  # fmt: skip
    fig.update_layout(**_layout(theme, 340, left=8, right=56, top=6, bottom=30), bargap=0.4)
    _axes(fig, theme, x_grid=True, y_grid=False)
    fig.update_yaxes(autorange="reversed", tickfont={"color": p["ink2"], "size": 12})
    fig.update_xaxes(
        rangemode="tozero", tickformat="~s", range=[0, ranked[0]["high_population"] * 1.18]
    )
    top = ranked[0]
    return ChartSpec(
        "county", f"{top['name']} has the most residents in higher-vulnerability tracts ({_compact(top['high_population'])})",
        "Top counties by residents living in tracts scoring below the higher-vulnerability cut-off. Selected county highlighted.",
        fig, 340, ["County", "Residents in higher-vulnerability tracts", "Such tracts", "All tracts", "Population"],
        [[c["name"], f"{c['high_population']:,}", f"{c['high_tracts']:,}", f"{c['tracts']:,}", f"{c['population']:,}"] for c in ranked],
        "Counts are absolute, so large counties rank high; compare with the share in the first chart.",
    )  # fmt: skip


def build_all(data: dict[str, Any] | None, theme: str) -> list[ChartSpec]:
    if not data or not data["tracts"]["features"]:
        return []
    feats = data["tracts"]["features"]
    return [band_chart(feats, theme), equity_chart(feats, theme), relationship_chart(feats, theme),
            county_chart(data["counties"], data.get("county"), theme)]  # fmt: skip
