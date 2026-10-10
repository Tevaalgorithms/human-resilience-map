"""Pure rendering helpers (no I/O) so they are easy to test."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import plotly.graph_objects as go
from dash import dash_table, dcc, html
from plotly.colors import sample_colorscale

from resilience_map.frontend.charts import ChartSpec
from resilience_map.risk.scoring import band_for

SCORE_NOTE = (
    "Resilience index: 0-100, higher = more resilient. A tract-level planning measure, "
    "not an individual health prediction."
)


# --------------------------------------------------------------------------------------------
# Small building blocks
# --------------------------------------------------------------------------------------------
def alert(kind: str, *children: Any) -> html.Div:
    return html.Div(
        list(children), className=f"alert {kind}", role="alert" if kind == "error" else "status"
    )


def error_box(message: str) -> html.Div:
    return alert("error", html.Span(["⚠ ", message]))


def empty_state(icon: str, title: str, body: str) -> html.Div:
    return html.Div(
        [
            html.Div(icon, className="ico", **{"aria-hidden": "true"}),
            html.B(title),
            html.Span(body),
        ],
        className="empty",
    )


def bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    west, south, east, north = bbox
    mid = math.radians((south + north) / 2)
    return abs(east - west) * 111.32 * math.cos(mid) * abs(north - south) * 110.57


def selection_view(
    sel: dict[str, Any] | None,
    scope: str,
    bbox: tuple[float, float, float, float] | None,
    compact: bool = False,
) -> Any:
    """Plain-language summary of what the current county/box selection contains."""
    if sel is None:
        return html.Span("Choose a study area to begin.", className="muted")
    n = sel["tracts"]
    status = sel["status"]
    label, cls = {
        "ok": ("Ready to analyse", "ok"),
        "too_many": (f"Too many tracts (max {sel['limit']})", "bad"),
        "empty": ("No tracts selected", "warn"),
    }[status]
    parts: list[Any] = [
        html.Span(scope, className="sel-scope"),
        html.Span(f"{n:,} tracts", className="sel-num"),
        html.Span(f"{sel['population']:,} people", className="sel-pop"),
        html.Span(label, className=f"badge-status {cls}"),
    ]
    if bbox:
        km2 = bbox_area_km2(bbox)
        parts.append(
            html.Span(
                f"Box ≈ {km2:,.0f} km²",
                className="sel-box" + (" bad-text" if km2 > 2500 else ""),
                title="Analyses are limited to 2,500 km²",
            )
        )
    if status == "too_many" and not compact:
        hint = (
            "Zoom in further and click “Use map view” again, or draw a smaller box."
            if bbox
            else "Zoom in on the map, then click “Use map view”, or draw a box."
        )
        parts.append(html.Span(hint, className="sel-hint"))
    return html.Div(parts, className="sel-parts")


def page_head(area: str, county: str | None, dataset: dict[str, Any], synthetic: bool) -> list[Any]:
    chip = html.Span(
        [html.Span(className="dot"), "Synthetic data" if synthetic else "Real data"],
        className="chip synthetic" if synthetic else "chip real",
    )
    crumbs = [html.Span("Study areas"), html.Span("/", className="sep"), html.B(area)]
    if county:
        crumbs += [html.Span("/", className="sep"), html.Span(county)]
    return [
        html.Div(html.Div(crumbs, className="crumbs")),
        html.Div(
            [
                chip,
                html.Span(f"Dataset {dataset['version']}"),
                html.Span(f"Published {dataset['published_at'][:10]}"),
                html.Span(dataset["scoring_version"]),
            ],
            className="page-meta",
        ),
    ]


def band_badge(score: float | None) -> html.Span:
    label = band_for(score)
    cls = {"Higher vulnerability": "high", "Moderate": "mid", "Lower vulnerability": "low"}.get(
        label, "none"
    )
    return html.Span(label, className=f"badge {cls}")


def fmt_num(v: float | None, digits: int = 1, unit: str = "") -> str:
    return "n/a" if v is None else f"{v:,.{digits}f}{unit}"


# --------------------------------------------------------------------------------------------
# Map layers
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Layer:
    key: str
    label: str
    scale: str
    reverse: bool
    unit: str
    digits: int
    lo_label: str
    hi_label: str
    fixed: tuple[float, float] | None = None

    def value(self, props: dict[str, Any]) -> float | None:
        if self.key == "score":
            return props.get("score")
        if self.key == "population":
            return props.get("population")
        return (props.get("indicators") or {}).get(self.key)


LAYERS: dict[str, Layer] = {
    lyr.key: lyr
    for lyr in (
        Layer(
            "score",
            "Resilience score",
            "YlOrRd",
            True,
            "",
            1,
            "More vulnerable",
            "More resilient",
            (0, 100),
        ),
        Layer(
            "heat", "Heat anomaly", "YlOrRd", False, " °C", 1, "Cooler than area median", "Hotter"
        ),
        Layer("canopy", "Tree canopy", "Greens", False, " %", 1, "Less canopy", "More canopy"),
        Layer("income", "Median income", "Blues", False, "", 0, "Lower", "Higher"),
        Layer("age65", "Age 65+", "Purples", False, " %", 1, "Lower share", "Higher share"),
        Layer(
            "population", "Population", "Greys", False, "", 0, "Fewer residents", "More residents"
        ),
    )
}


def _range(layer: Layer, values: list[float]) -> tuple[float, float]:
    if layer.fixed:
        return layer.fixed
    if not values:
        return (0.0, 1.0)
    s = sorted(values)
    lo, hi = s[int(0.02 * (len(s) - 1))], s[int(0.98 * (len(s) - 1))]  # clip outliers for contrast
    return (lo, hi if hi > lo else lo + 1)


def _hex_stops(layer: Layer, n: int = 8) -> list[str]:
    cols = sample_colorscale(layer.scale, [i / (n - 1) for i in range(n)])
    return cols[::-1] if layer.reverse else cols


def legend(data: dict[str, Any] | None, layer_key: str, has_sites: bool) -> html.Div:
    layer = LAYERS[layer_key]
    vals = [
        v
        for f in (data or {}).get("tracts", {}).get("features", [])
        if (v := layer.value(f["properties"])) is not None
    ]
    lo, hi = _range(layer, vals)
    stops = ", ".join(_hex_stops(layer))
    fmt = (
        (lambda v: f"{v:,.{layer.digits}f}{layer.unit}")
        if layer.key != "income"
        else (lambda v: f"${v:,.0f}")
    )
    keys = [
        html.Span(
            [html.I(style={"background": "#1d4ed8"}), "Existing cooling center"], className="key"
        ),
        html.Span([html.I(style={"background": "#6b7280"}), "Candidate site"], className="key"),
    ]
    if has_sites:
        keys.append(
            html.Span(
                [html.I(style={"background": "#dc2626"}), "Conditional candidate site"],
                className="key",
            )
        )
    return html.Div(
        [
            html.Div(
                [
                    html.Div(layer.label, className="legend-title"),
                    html.Div(
                        className="legend-bar",
                        style={"background": f"linear-gradient(90deg, {stops})"},
                    ),
                    html.Div(
                        [
                            html.Span(f"{layer.lo_label} ({fmt(lo)})"),
                            html.Span(f"{layer.hi_label} ({fmt(hi)})"),
                        ],
                        className="legend-ends",
                    ),
                ],
                className="legend-scale",
            ),
            html.Div(keys, className="legend-keys"),
        ],
        className="legend",
    )


def _rings(geom: dict[str, Any]) -> list[list[list[float]]]:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return [ring for poly in polys for ring in poly]


def _zoom(lons: list[float], lats: list[float]) -> float:
    """Zoom level that frames the data extent (web-mercator, ~800px wide map)."""
    span = max(max(lons) - min(lons), (max(lats) - min(lats)) * 1.3, 1e-4)
    return float(min(14.0, max(5.0, math.log2(420 / span) - 0.2)))


def build_map(
    data: dict[str, Any] | None,
    layer_key: str,
    theme: str,
    selected_sites: list[dict[str, Any]],
    highlight: str | None,
    bbox: tuple[float, float, float, float] | None = None,
) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)",
        uirevision="map",
        font={"family": "system-ui, sans-serif"},
    )
    style = "carto-darkmatter" if theme == "dark" else "carto-positron"
    if not data or not data["tracts"]["features"]:
        fig.update_layout(map={"style": style, "zoom": 3, "center": {"lon": -98, "lat": 39}})
        return fig
    layer = LAYERS[layer_key]
    gj = data["tracts"]
    feats = gj["features"]
    ids = [f["properties"]["tract_id"] for f in feats]
    vals = [layer.value(f["properties"]) for f in feats]
    lo, hi = _range(layer, [v for v in vals if v is not None])
    fmt = (
        "$%{customdata[1]:,.0f}"
        if layer.key == "income"
        else f"%{{customdata[1]:,.{layer.digits}f}}{layer.unit}"
    )
    names = [f["properties"]["name"].split(";")[0] for f in feats]
    centroids = []
    for f in feats:
        rs = _rings(f["geometry"])
        xs = [c[0] for ring in rs for c in ring]
        ys = [c[1] for ring in rs for c in ring]
        centroids.append(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
    if bbox:
        west, south, east, north = bbox
        inside = [west <= x <= east and south <= y <= north for x, y in centroids]
        opacity: Any = [0.78 if i else 0.2 for i in inside]
    else:
        opacity = 0.72
    fig.add_trace(
        go.Choroplethmap(
            geojson=gj,
            locations=ids,
            featureidkey="properties.tract_id",
            z=vals,
            zmin=lo,
            zmax=hi,
            colorscale=layer.scale,
            reversescale=layer.reverse,
            showscale=False,
            marker_opacity=opacity,
            marker_line_width=[3 if i == highlight else 0.4 for i in ids],
            marker_line_color=["#111827" if i == highlight else "#6b7280" for i in ids],
            customdata=[
                [n, v if v is not None else float("nan"), f["properties"]["population"]]
                for n, v, f in zip(names, vals, feats, strict=True)
            ],
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>Population %{customdata[2]:,}<br>"
                f"{layer.label}: {fmt}<extra></extra>"
            ),
        )
    )
    for key, color, size in (("centers", "#1d4ed8", 11), ("candidates", "#6b7280", 8)):
        pts = data[key]
        if pts:
            fig.add_trace(
                go.Scattermap(
                    lon=[p["lon"] for p in pts],
                    lat=[p["lat"] for p in pts],
                    mode="markers",
                    marker={"size": size, "color": color},
                    text=[f"{p['name']} ({p['id']}) · capacity {p['capacity']:,}" for p in pts],
                    hoverinfo="text",
                )
            )
    if selected_sites:
        fig.add_trace(
            go.Scattermap(
                lon=[s["lon"] for s in selected_sites],
                lat=[s["lat"] for s in selected_sites],
                mode="markers+text",
                marker={"size": 18, "color": "#dc2626"},
                text=[s["site_id"] for s in selected_sites],
                textposition="top right",
                textfont={"color": "#dc2626", "size": 12},
                hovertext=[f"{s['name']} ({s['site_id']})" for s in selected_sites],
                hoverinfo="text",
            )
        )
    if bbox:
        west, south, east, north = bbox
        fig.add_trace(
            go.Scattermap(
                lon=[west, east, east, west, west],
                lat=[south, south, north, north, south],
                mode="lines",
                line={"width": 3, "color": "#0f62fe"},
                hoverinfo="skip",
                name="Planning box",
            )
        )
    # Invisible per-tract points so the box-select tool can report the drawn range.
    fig.add_trace(
        go.Scattermap(
            lon=[c[0] for c in centroids],
            lat=[c[1] for c in centroids],
            mode="markers",
            marker={"size": 10, "opacity": 0.01},
            customdata=ids,
            hoverinfo="skip",
            name="Tract centroids",
        )
    )
    lons = [c[0] for f in feats for ring in _rings(f["geometry"]) for c in ring]
    lats = [c[1] for f in feats for ring in _rings(f["geometry"]) for c in ring]
    fig.update_layout(
        map={
            "style": style,
            "zoom": _zoom(lons, lats),
            "center": {"lon": (min(lons) + max(lons)) / 2, "lat": (min(lats) + max(lats)) / 2},
        }
    )
    return fig


# --------------------------------------------------------------------------------------------
# KPIs
# --------------------------------------------------------------------------------------------
def kpi(label: str, value: str, sub: str = "", accent: bool = False) -> html.Div:
    return html.Div(
        [
            html.Div(label, className="kpi-label"),
            html.Div(value, className="kpi-value"),
            html.Div(sub, className="kpi-sub"),
        ],
        className="kpi accent" if accent else "kpi",
    )


def kpis(data: dict[str, Any] | None, scope: str) -> list[html.Div]:
    if not data:
        return [html.Div(className="kpi-skeleton") for _ in range(5)]
    feats = [f["properties"] for f in data["tracts"]["features"]]
    total_pop = sum(p["population"] for p in feats)
    scored = [p for p in feats if p["score"] is not None]
    spop = sum(p["population"] for p in scored)
    mean = sum(p["score"] * p["population"] for p in scored) / spop if spop else None
    high = [p for p in scored if p["score"] < 40]
    high_pop = sum(p["population"] for p in high)
    d = data["dataset"]
    return [
        kpi("Census tracts", f"{len(feats):,}", scope),
        kpi("Population", f"{total_pop:,}", "ACS 5-year estimate"),
        kpi(
            "Avg. resilience",
            fmt_num(mean, 1) + (" / 100" if mean is not None else ""),
            f"Population-weighted · {band_for(mean)}",
        ),
        kpi(
            "Higher-vulnerability tracts",
            f"{len(high):,}",
            f"{high_pop:,} residents ({100 * high_pop / total_pop:.0f}%)" if total_pop else "",
            accent=bool(high),
        ),
        kpi(
            "Data version",
            d["version"][:12],
            f"Published {d['published_at'][:10]} · {d['scoring_version']}",
        ),
    ]


# --------------------------------------------------------------------------------------------
# Tracts table
# --------------------------------------------------------------------------------------------
def table_rows(
    data: dict[str, Any] | None, search: str | None, county_names: dict[str, str]
) -> list[dict[str, Any]]:
    if not data:
        return []
    q = (search or "").strip().lower()
    rows = []
    for f in data["tracts"]["features"]:
        p = f["properties"]
        ind = p.get("indicators") or {}
        county = county_names.get(p.get("county_id") or "", "")
        name = p["name"].split(";")[0]
        if q and q not in f"{name} {p['tract_id']} {county}".lower():
            continue
        rows.append({
            "id": p["tract_id"], "name": name, "county": county, "population": p["population"],
            "score": None if p["score"] is None else round(p["score"], 1),
            "heat": None if ind.get("heat") is None else round(ind["heat"], 1),
            "canopy": None if ind.get("canopy") is None else round(ind["canopy"], 1),
            "income": None if ind.get("income") is None else round(ind["income"]),
        })  # fmt: skip
    return sorted(rows, key=lambda r: (r["score"] is None, r["score"] or 0))


def tracts_table(table_id: str = "table") -> Any:
    cols = [
        {"name": "Tract", "id": "name"},
        {"name": "County", "id": "county"},
        {"name": "Population", "id": "population", "type": "numeric", "format": {"specifier": ","}},
        {"name": "Score", "id": "score", "type": "numeric"},
        {"name": "Heat Δ°C", "id": "heat", "type": "numeric"},
        {"name": "Canopy %", "id": "canopy", "type": "numeric"},
        {"name": "Income $", "id": "income", "type": "numeric", "format": {"specifier": ",.0f"}},
    ]
    return dash_table.DataTable(  # type: ignore[attr-defined]
        id=table_id,
        columns=cols,
        data=[],
        page_size=10,
        sort_action="native",
        export_format="csv",
        cell_selectable=True,
        row_selectable=False,
        style_as_list_view=True,
        style_table={"overflowX": "auto"},
        style_header={
            "backgroundColor": "var(--surface-2)",
            "color": "var(--mute)",
            "fontWeight": 600,
            "fontSize": "11px",
            "textTransform": "uppercase",
            "letterSpacing": ".04em",
            "border": "0",
            "borderBottom": "1px solid var(--line)",
        },
        style_cell={
            "backgroundColor": "transparent",
            "color": "var(--ink)",
            "fontFamily": "var(--font)",
            "fontSize": "13px",
            "padding": "8px 10px",
            "border": "0",
            "borderBottom": "1px solid var(--line)",
            "textAlign": "left",
        },
        style_cell_conditional=[
            {"if": {"column_id": c}, "textAlign": "right"}
            for c in ("population", "score", "heat", "canopy", "income")
        ],
        style_data_conditional=[
            {
                "if": {"filter_query": "{score} < 40", "column_id": "score"},
                "color": "var(--bad)",
                "fontWeight": 700,
            },
            {
                "if": {"filter_query": "{score} >= 40 && {score} < 60", "column_id": "score"},
                "color": "var(--warn)",
                "fontWeight": 700,
            },
            {
                "if": {"filter_query": "{score} >= 60", "column_id": "score"},
                "color": "var(--ok)",
                "fontWeight": 700,
            },
            {
                "if": {"state": "active"},
                "backgroundColor": "var(--brand-soft)",
                "border": "1px solid var(--brand)",
            },
        ],
        css=[
            {
                "selector": ".export, .previous-next, .page-number",
                "rule": "font-family: var(--font); font-size: 12px;",
            }
        ],
    )


# --------------------------------------------------------------------------------------------
# Tract card
# --------------------------------------------------------------------------------------------
def components_table(components: list[dict[str, Any]]) -> html.Table:
    rows = []
    for c in components:
        contrib, raw = c.get("contribution"), c.get("raw_value")
        w = c.get("weight") or 0
        pct = 0 if contrib is None or not w else min(100, contrib / (100 * w) * 100)
        unit = c["unit"]
        value = (
            "unavailable"
            if raw is None
            else (f"${raw:,.0f}" if unit == "USD" else f"{raw:,.2f} {unit}".strip())
        )
        rows.append(
            html.Tr(
                [
                    html.Td(c["label"]),
                    html.Td(value, className="num"),
                    html.Td(
                        [
                            html.Div(html.Span(style={"width": f"{pct:.0f}%"}), className="bar"),
                            html.Span(
                                "—" if contrib is None else f"{contrib:.1f} pts",
                                className="muted small",
                            ),
                        ]
                    ),
                ]
            )
        )
    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Indicator"),
                        html.Th("Value", className="num"),
                        html.Th("Contribution"),
                    ]
                )
            ),
            html.Tbody(rows),
        ],
        className="tbl",
    )


def tract_card(d: dict[str, Any], rank: tuple[int, int] | None, county: str | None) -> html.Div:
    score = d["score"]
    dates = d.get("source_dates") or {}
    marker = (
        None if score is None else html.I(style={"left": f"{max(0.0, min(100.0, score)):.1f}%"})
    )
    notices = [alert("warn", html.Span(n)) for n in d["notices"]]
    observed = ", ".join(sorted({str(v) for v in dates.values()})) or "n/a"
    return html.Div(
        [
            html.H3(d["name"].split(";")[0], style={"fontSize": "16px"}),
            html.Div(
                f"Tract {d['tract_id']}" + (f" · {county}" if county else ""),
                className="muted small",
            ),
            html.Div(
                [
                    html.Span("n/a" if score is None else f"{score:.1f}", className="score-num"),
                    html.Span("/ 100", className="score-of"),
                    band_badge(score),
                ],
                className="score-row",
            ),
            html.Div(marker, className="gauge", **{"aria-hidden": "true"}),
            html.Div(
                [html.Span("More vulnerable"), html.Span("More resilient")], className="gauge-ends"
            ),
            html.Div(
                f"More vulnerable than {100 - round(100 * rank[0] / rank[1])}% of {rank[1]:,} scored tracts in view"
                if rank and score is not None
                else "",
                className="muted small",
                style={"marginTop": "6px"},
            ),
            *notices,
            html.Div("What drives this score", className="section-t"),
            components_table(d["components"]),
            html.Div("Context", className="section-t"),
            html.Dl(
                [
                    html.Dt("Population"),
                    html.Dd(f"{d['population']:,}"),
                    html.Dt("Nearest cooling center"),
                    html.Dd(
                        f"{d['nearest_center_km']:.2f} km (straight line)"
                        if d.get("nearest_center_km") is not None
                        else "no centers loaded"
                    ),
                    html.Dt("Income margin of error"),
                    html.Dd(
                        f"±${d['median_income_moe_usd']:,.0f}"
                        if d.get("median_income_moe_usd")
                        else "n/a"
                    ),
                    html.Dt("Data observed"),
                    html.Dd(observed),
                ],
                className="kv",
            ),
            html.Div(
                f"Dataset {d['dataset_version']} · {d['scoring_version']}. {SCORE_NOTE}",
                className="muted small",
                style={"marginTop": "10px"},
            ),
            html.Div(
                html.Button(
                    "Ask about this tract →",
                    id="explain-tract",
                    className="btn btn-secondary btn-sm",
                    n_clicks=0,
                )
            ),
        ]
    )


# --------------------------------------------------------------------------------------------
# Job progress + results
# --------------------------------------------------------------------------------------------
_STEPS = ["Queued", "Understanding request", "Readiness", "Analysis & checks", "Explanation"]
_STEP_OF = {"queued": 0, "resuming": 0, "interpret": 1, "validate_plan": 1, "clarify": 1,
            "retrieve": 1, "assess_data_readiness": 2, "analyze": 3, "verify": 3,
            "compare_scenarios": 3, "assess_uncertainty": 3, "explain": 4,
            "check_narrative": 4, "finalize": 4}  # fmt: skip


def stepper(status: str, progress: str) -> html.Ol:
    now = _STEP_OF.get(progress, 0 if status == "queued" else 1)
    finished = status in {"completed", "incomplete"}
    items = []
    for i, label in enumerate(_STEPS):
        cls = (
            "done"
            if finished or i < now
            else ("now" if i == now and status in {"queued", "running"} else "")
        )
        if status == "failed" and i == now:
            cls = "fail"
        if status == "needs_input" and i == now:
            cls = "now"
        items.append(
            html.Li(label, className=cls, **({"aria-current": "step"} if cls == "now" else {}))
        )
    return html.Ol(items, className="stepper")


def job_header(job: dict[str, Any]) -> html.Div:
    st = job["status"]
    label = {"needs_input": "Needs your input"}.get(st, st.capitalize())
    return html.Div(
        [
            html.Div(
                [
                    html.Span(label, className=f"badge {st}"),
                    html.Span(f"  {job['question'][:90]}", className="muted small"),
                ]
            ),
            stepper(st, job["progress"]),
            html.Div(
                f"Pinned to dataset {job['dataset_version_id'][:8]}… · {job['scoring_version']}",
                className="muted small",
            ),
        ]
    )


def _tile(label: str, value: str, positive: bool = False) -> html.Div:
    return html.Div(
        [html.Div(value, className="v"), html.Div(label, className="l")],
        className="tile pos" if positive else "tile",
    )


def _explanation_note(source: str | None, provider: str | None, model: str | None = None) -> str:
    if provider == "stub":
        return "Offline template (no language model configured), checked against the numbers above."
    if source != "llm":
        return "Deterministic summary (model narrative unavailable or failed the grounding check)."
    return f"Written by {model or 'the language model'} ({provider or 'model provider'}) and checked against the numbers above."


def _accordion(title: str, items: list[str]) -> html.Details:
    return html.Details(
        [html.Summary(title), html.Div(html.Ul([html.Li(x) for x in items]))], className="acc"
    )


def _readiness_panel(readiness: dict[str, Any] | None) -> Any:
    if not readiness:
        return None
    level = {"ready": "ok", "exploratory": "warn", "blocked": "warn"}.get(
        readiness.get("status"), "info"
    )
    detail = (
        readiness.get("warnings")
        or readiness.get("missing_inputs")
        or ["Required evidence checks passed for this operation."]
    )
    return html.Div(
        [
            html.Div("Data readiness", className="section-t"),
            alert(
                level,
                html.Span(
                    [
                        html.B(readiness.get("status", "unknown").capitalize() + ". "),
                        " ".join(detail[:3]),
                    ]
                ),
            ),
            html.Details(
                [
                    html.Summary("Evidence, missing inputs and next actions"),
                    html.Pre(
                        json.dumps(
                            {
                                "reasons": readiness.get("reasons", []),
                                "evidence_references": readiness.get("evidence_references", []),
                                "suggested_next_actions": readiness.get(
                                    "suggested_next_actions", []
                                ),
                            },
                            indent=2,
                        ),
                        style={"whiteSpace": "pre-wrap", "maxHeight": "300px", "overflow": "auto"},
                    ),
                ]
            ),
        ]
    )


def _comparison_panel(comparison: dict[str, Any] | None) -> Any:
    if not comparison:
        return None
    rows = comparison.get("scenarios", [])
    return html.Div(
        [
            html.Div("Scenario comparison", className="section-t"),
            html.Table(
                [
                    html.Thead(
                        html.Tr(
                            [
                                html.Th("Scenario"),
                                html.Th("Status"),
                                html.Th("Baseline access", className="num"),
                                html.Th("Scenario access", className="num"),
                                html.Th("Newly served", className="num"),
                                html.Th("Cost", className="num"),
                                html.Th("Verification"),
                            ]
                        )
                    ),
                    html.Tbody(
                        [
                            html.Tr(
                                [
                                    html.Td(row["scenario_id"]),
                                    html.Td(row["status"]),
                                    html.Td(
                                        f"{row['coverage']['baseline_pct']}%"
                                        if row.get("coverage")
                                        else "—",
                                        className="num",
                                    ),
                                    html.Td(
                                        f"{row['coverage']['scenario_pct']}%"
                                        if row.get("coverage")
                                        else "—",
                                        className="num",
                                    ),
                                    html.Td(
                                        f"{row['coverage']['newly_served']:,}"
                                        if row.get("coverage")
                                        else "—",
                                        className="num",
                                    ),
                                    html.Td(
                                        f"${row['cost_usd']:,.0f}"
                                        if row.get("cost_usd") is not None
                                        else "—",
                                        className="num",
                                    ),
                                    html.Td(
                                        "passed"
                                        if (row.get("verification") or {}).get("passed")
                                        else (row.get("reason") or "not completed")
                                    ),
                                ]
                            )
                            for row in rows
                        ]
                    ),
                ],
                className="tbl",
            ),
            html.P(comparison.get("notice", ""), className="muted small"),
        ]
    )


def _uncertainty_panel(uncertainty: dict[str, Any] | None) -> Any:
    if not uncertainty:
        return None
    return html.Div(
        [
            html.Div("Sensitivity assessment", className="section-t"),
            html.P(
                f"Newly served range: {uncertainty.get('coverage_range') or 'unavailable'} · "
                f"Cost range: {uncertainty.get('cost_range_usd') or 'unavailable'} · "
                f"Stable across tested variants: {'yes' if uncertainty.get('stable_recommendation') else 'no'}"
            ),
            html.Table(
                [
                    html.Thead(
                        html.Tr(
                            [
                                html.Th("Variant"),
                                html.Th("Status"),
                                html.Th("Selected sites"),
                                html.Th("Verification"),
                            ]
                        )
                    ),
                    html.Tbody(
                        [
                            html.Tr(
                                [
                                    html.Td(run["variant_id"]),
                                    html.Td(run["status"]),
                                    html.Td(", ".join(run.get("selected_site_ids", [])) or "—"),
                                    html.Td(
                                        "passed"
                                        if (run.get("verification") or {}).get("passed")
                                        else (run.get("reason") or "not completed")
                                    ),
                                ]
                            )
                            for run in uncertainty.get("runs", [])
                        ]
                    ),
                ],
                className="tbl",
            ),
            _accordion("Sensitivity limitations", uncertainty.get("limitations", [])),
        ]
    )


def result_panel(result: dict[str, Any]) -> html.Div:
    if result["outcome"] != "completed":
        errs = "; ".join((result.get("verification") or {}).get("errors", []))
        return html.Div(
            [
                _readiness_panel(result.get("readiness")),
                alert(
                    "warn",
                    html.Span(
                        [
                            html.B("No recommendation. "),
                            result.get("incomplete_reason") or "Incomplete",
                        ]
                    ),
                ),
                html.Div(f"Verification: {errs}" if errs else "", className="muted small"),
                html.Div(
                    "Try rephrasing, or use the example questions in the Run analysis tab.",
                    className="muted small",
                ),
            ]
        )
    a = result["analysis"]
    parts: list[Any] = [_readiness_panel(result.get("readiness"))]
    if a["intent"] == "site_selection":
        cov = a["coverage"]
        parts += [
            html.Div("Coverage impact", className="section-t"),
            html.Div(
                [
                    _tile("Access to listed centers", f"{cov['baseline_pct']}%"),
                    _tile("With new sites", f"{cov['scenario_pct']}%"),
                    _tile(
                        "Newly served",
                        f"+{cov['newly_served']:,}",
                        positive=cov["newly_served"] > 0,
                    ),
                ],
                className="tiles",
            ),
            html.Div(
                f"{cov['baseline_served']:,} → {cov['scenario_served']:,} of {cov['total_population']:,} residents · "
                f"{a['access_model'].replace('_', ' ')} · {a['constraints']['threshold_minutes']} minute threshold",
                className="muted small",
            ),
            html.P(
                "Inventory status: "
                + a.get("inventory_status", "unknown")
                + ". Empty/unknown inventory does not establish absence. Listed centers are not confirmed currently open. Recommendations remain conditional on local review."
            ),
            html.Details(
                [
                    html.Summary("Routing, inventory and indicator readiness"),
                    html.Pre(
                        json.dumps(
                            {
                                k: a.get(k)
                                for k in (
                                    "routing",
                                    "access_issues",
                                    "inventory",
                                    "indicator_readiness",
                                )
                            },
                            indent=2,
                        ),
                        style={"whiteSpace": "pre-wrap", "maxHeight": "300px", "overflow": "auto"},
                    ),
                ]
            ),
            coverage_bar(cov),
            html.Div("Conditional candidate sites", className="section-t"),
            html.Table(
                [
                    html.Thead(
                        html.Tr(
                            [
                                html.Th("#"),
                                html.Th("Site"),
                                html.Th("Capacity", className="num"),
                                html.Th("Cost", className="num"),
                                html.Th("Assigned", className="num"),
                            ]
                        )
                    ),
                    html.Tbody(
                        [
                            html.Tr(
                                [
                                    html.Td(i),
                                    html.Td(f"{s['name']} ({s['site_id']})"),
                                    html.Td(f"{s['capacity']:,}", className="num"),
                                    html.Td(f"${s['cost_usd']:,.0f}", className="num"),
                                    html.Td(f"{s['population_assigned']:,}", className="num"),
                                ]
                            )
                            for i, s in enumerate(a["selected_sites"], 1)
                        ]
                        or [
                            html.Tr(
                                html.Td(
                                    "None under these constraints", colSpan=5, className="muted"
                                )
                            )
                        ]
                    ),
                ],
                className="tbl",
            ),
        ]
        parts.append(site_meters(a["selected_sites"]))
        sv = a["solver"]
        parts.append(
            html.Div(f"Solver {sv['status']} · {sv['runtime_seconds']}s", className="muted small")
        )
        parts += [alert("warn", html.Span(n)) for n in sv["notes"]]
        parts += [
            _comparison_panel(result.get("comparison")),
            _uncertainty_panel(result.get("uncertainty")),
        ]
    elif a["intent"] == "vulnerability_ranking":
        parts += [
            html.Div("Most vulnerable tracts", className="section-t"),
            html.Table(
                [
                    html.Thead(
                        html.Tr(
                            [
                                html.Th("#"),
                                html.Th("Tract"),
                                html.Th("Population", className="num"),
                                html.Th("Score", className="num"),
                            ]
                        )
                    ),
                    html.Tbody(
                        [
                            html.Tr(
                                [
                                    html.Td(i),
                                    html.Td(f"{r['name'].split(';')[0]} ({r['tract_id']})"),
                                    html.Td(f"{r['population']:,}", className="num"),
                                    html.Td(f"{r['score']:.1f}", className="num"),
                                ]
                            )
                            for i, r in enumerate(a["ranking"], 1)
                        ]
                    ),
                ],
                className="tbl",
            ),
        ]
    else:
        t = a["tract"]
        parts += [
            html.Div(
                f"{t['name'].split(';')[0]} · score {fmt_num(t['score'])}", className="section-t"
            ),
            components_table(t["components"]),
        ]
    checks = (result.get("verification") or {}).get("checks_run", 0)
    parts += [
        html.Div("Explanation", className="section-t"),
        html.Div(result["explanation"], className="prose"),
        html.Div(
            _explanation_note(
                result.get("explanation_source"),
                result["workflow"].get("llm_provider"),
                result["workflow"].get("llm_model"),
            ),
            className="muted small",
            style={"marginTop": "4px"},
        ),
        alert(
            "ok",
            html.Span(
                [
                    html.B(f"{checks} internal consistency checks passed. "),
                    html.Span("These do not establish real-world accuracy. "),
                    "Operating suitability of any site still needs local review.",
                ]
            ),
        ),
        _accordion("Assumptions", a["assumptions"]),
        _accordion("Limitations", a["limitations"]),
        html.Div(
            f"Dataset {a['dataset_version']} · {a['scoring_version']} · prompts {', '.join(result['workflow']['prompts'])}",
            className="muted small",
        ),
    ]
    return html.Div([part for part in parts if part is not None])


def history_list(jobs: list[dict[str, Any]]) -> html.Div:
    if not jobs:
        return empty_state("i", "No analyses yet", "Your recent analyses will appear here.")
    return html.Div(
        [
            html.Button(
                [
                    html.Span(j["question"], className="q"),
                    html.Span(j["status"].replace("_", " "), className=f"badge {j['status']}"),
                ],
                id={"type": "hist", "job": j["id"]},
                className="hist-item",
                n_clicks=0,
                title=j["question"],
            )
            for j in jobs
        ],
        className="history",
    )


# --------------------------------------------------------------------------------------------
# Methodology
# --------------------------------------------------------------------------------------------
def methodology_panel(m: dict[str, Any] | None, dataset: dict[str, Any] | None) -> html.Div:
    if not m:
        return html.Div("Methodology unavailable.", className="muted")
    weights = html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Indicator"),
                        html.Th("Weight", className="num"),
                        html.Th("Scored 0 at"),
                        html.Th("Scored 100 at"),
                    ]
                )
            ),
            html.Tbody(
                [
                    html.Tr(
                        [
                            html.Td(i["label"]),
                            html.Td(f"{100 * i['weight']:.0f}%", className="num"),
                            html.Td(f"{i['worst']:,g} {i['unit']}"),
                            html.Td(f"{i['best']:,g} {i['unit']}"),
                        ]
                    )
                    for i in m["indicators"]
                ]
            ),
        ],
        className="tbl",
    )
    sources = []
    for s in (dataset or {}).get("sources", []):
        if isinstance(s, dict) and s.get("name"):
            sources.append(
                html.Li(
                    [
                        html.B(s["name"]),
                        f" · {s.get('license', '')} · {s.get('observation_period', '')} · ",
                        html.Span(s.get("url", ""), className="muted small"),
                    ]
                )
            )
    return html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.H3("How the resilience score works", style={"fontSize": "14px"}),
                            weights,
                            html.P(
                                f"Scoring model {m['scoring_version']}. Missing indicators are excluded and weights renormalized; "
                                f"a score needs at least {m['min_indicators']} indicators. Bands (display only): <40 higher vulnerability, "
                                "40-60 moderate, ≥60 lower vulnerability. These bands are illustrative, not validated thresholds.",
                                className="muted small",
                            ),
                        ]
                    ),
                    html.Div(
                        [
                            html.H3("Data sources", style={"fontSize": "14px"}),
                            html.Ul(sources or [html.Li("No source manifest available.")]),
                            html.H3(
                                "Standing assumptions",
                                style={"fontSize": "14px", "marginTop": "12px"},
                            ),
                            html.Ul([html.Li(x) for x in m["assumptions"]]),
                            html.H3("Limitations", style={"fontSize": "14px", "marginTop": "12px"}),
                            html.Ul([html.Li(x) for x in m["limitations"]]),
                        ]
                    ),
                ],
                className="methods-grid",
            ),
        ]
    )


# --------------------------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------------------------
def chart_card(spec: ChartSpec) -> html.Section:
    """Insight-led title, subtitle, chart, table twin and caveat. Never gated on hover."""
    head = html.Div(
        [html.H3(spec.title), html.P(spec.subtitle, className="muted small")], className="chart-h"
    )
    if spec.fig is None:
        body: Any = empty_state("i", "Not available here", spec.empty)
        table = None
    else:
        body = html.Figure(
            dcc.Graph(
                figure=spec.fig,
                config={"displayModeBar": False, "responsive": True},
                style={"height": f"{spec.height}px", "width": "100%"},
            ),
            role="img",
            **{"aria-label": f"{spec.title}. Use the table view below for exact values."},
            className="chart-fig",
        )
        table = html.Details(
            [
                html.Summary("View as table"),
                html.Div(
                    html.Table(
                        [
                            html.Thead(
                                html.Tr(
                                    [
                                        html.Th(c, className="" if i == 0 else "num")
                                        for i, c in enumerate(spec.columns)
                                    ]
                                )
                            ),
                            html.Tbody(
                                [
                                    html.Tr(
                                        [
                                            html.Td(v, className="" if i == 0 else "num")
                                            for i, v in enumerate(r)
                                        ]
                                    )
                                    for r in spec.rows
                                ]
                            ),
                        ],
                        className="tbl",
                    )
                ),
            ],
            className="acc",
        )
    return html.Section(
        [head, body, table, html.Div(spec.note, className="muted small chart-note")],
        className="card chart-card",
    )


def insights_panel(specs: list[ChartSpec]) -> html.Div:
    if not specs:
        return empty_state("i", "No data to chart", "Choose a study area and county first.")
    return html.Div([chart_card(s) for s in specs], className="charts-grid")


def coverage_bar(cov: dict[str, Any]) -> html.Div:
    """Part-to-whole: already served | newly served | not served (3 series -> legend + values)."""
    total = cov["total_population"] or 1
    served, new = cov["baseline_served"], cov["newly_served"]
    rest = max(0, total - served - new)
    parts = [
        ("seg-1", "Already served", served),
        ("seg-2", "Newly served", new),
        ("seg-n", "Not served", rest),
    ]
    bar = html.Div(
        [
            html.Div(
                className=f"seg {cls}",
                style={"width": f"{100 * v / total:.2f}%"},
                title=f"{label}: {v:,} ({100 * v / total:.1f}%)",
            )
            for cls, label, v in parts
            if v > 0
        ],
        className="stack",
        role="img",
        **{"aria-label": "; ".join(f"{label} {100 * v / total:.1f}%" for _, label, v in parts)},
    )
    legend = html.Div(
        [
            html.Span(
                [
                    html.I(className=f"sw {cls}"),
                    html.Span(f"{label} "),
                    html.B(f"{v:,}"),
                    html.Span(f" ({100 * v / total:.1f}%)", className="muted"),
                ],
                className="key",
            )
            for cls, label, v in parts
        ],
        className="legend-keys stack-legend",
    )
    return html.Div([html.Div("Who gets access", className="section-t"), bar, legend])


def site_meters(sites: list[dict[str, Any]]) -> html.Div:
    """Capacity used per recommended site (track = lighter step of the fill's own ramp)."""
    if not sites:
        return html.Div()
    rows = []
    for s in sites:
        used = s["population_assigned"] / s["capacity"] if s["capacity"] else 0
        rows.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(f"{s['name']} ({s['site_id']})"),
                            html.Span(
                                f"{s['population_assigned']:,} of {s['capacity']:,} · {100 * used:.0f}%",
                                className="muted small",
                            ),
                        ],
                        className="meter-h",
                    ),
                    html.Div(
                        html.Div(
                            style={"width": f"{min(100, 100 * used):.1f}%"}, className="meter-fill"
                        ),
                        className="meter-track",
                        title=f"{s['population_assigned']:,} of {s['capacity']:,} capacity assigned",
                    ),
                ],
                className="meter",
            )
        )
    return html.Div([html.Div("Capacity used at each site", className="section-t"), *rows])
