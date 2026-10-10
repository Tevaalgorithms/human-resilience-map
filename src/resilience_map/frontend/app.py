"""Dash application: explore the map, inspect tracts, run analyses, review results.

All data access goes through the FastAPI service (``ApiClient``); this module only builds the UI
and wires callbacks. Rendering lives in ``views.py``.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import dash
from dash import ALL, ClientsideFunction, Input, Output, State, ctx, dcc, html, no_update
from flask import abort

from resilience_map import __version__
from resilience_map.core.config import get_settings
from resilience_map.core.geography import parse_bbox
from resilience_map.core.logging import configure_logging
from resilience_map.frontend import charts, views
from resilience_map.frontend.api_client import ApiClient, ApiError
from resilience_map.frontend.auth import api_auth_headers, install_auth

log = logging.getLogger(__name__)
TERMINAL = {"completed", "incomplete", "failed"}
PANELS = ("tract", "analysis", "results")
PRESET_SITING = "Where should we open three cooling centers? Walking, 20 minutes, budget $600k"
PRESET_RANK = "Which are the most vulnerable tracts? top 10"
MODES = [{"label": "Walk (4.8 km/h)", "value": "walk"}, {"label": "Bike (14 km/h)", "value": "bike"},
         {"label": "Drive (30 km/h)", "value": "drive"}]  # fmt: skip


def create_dash_app() -> dash.Dash:
    settings = get_settings()
    configure_logging(settings.log_level)
    api = ApiClient()
    app = dash.Dash(
        __name__,
        title="Human Resilience Map",
        assets_folder=str(Path(__file__).parent / "assets"),
        suppress_callback_exceptions=True,
        update_title=None,
        meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
    )
    install_auth(app.server, settings)

    def auth() -> dict[str, str]:
        return api_auth_headers(settings)

    def guard(n: int, slot: int | None = None) -> Any:
        """Turn API failures into a visible message (in output ``slot``) instead of a broken callback."""

        def deco(fn: Any) -> Any:
            def wrapper(*a: Any, **k: Any) -> Any:
                try:
                    return fn(*a, **k)
                except ApiError as e:
                    if e.status == 401:
                        abort(401)
                    out: list[Any] = [no_update] * n
                    if slot is not None:
                        out[slot] = views.error_box(e.detail)
                    return tuple(out) if n > 1 else out[0]

            wrapper.__name__ = fn.__name__
            return wrapper

        return deco

    # ------------------------------------------------------------------ layout
    def field(label: str, control: Any, hint: str = "") -> html.Div:
        return html.Div(
            [
                html.Label(label, className="lbl"),
                control,
                html.Div(hint, className="hint") if hint else None,
            ]
        )

    def serve_layout() -> html.Div:
        try:
            areas = api.get("/v1/study-areas", auth())
        except ApiError as e:
            return html.Div(
                [html.Div(views.error_box(e.detail), className="card-b")], style={"padding": "24px"}
            )
        first = areas[0]["id"] if areas else None
        return html.Div(
            [
                html.A("Skip to content", href="#main", className="skip-link"),
                html.Header(
                    [
                        html.Div(
                            [
                                html.Div(className="logo", **{"aria-hidden": "true"}),
                                html.Div(
                                    [
                                        html.H1("Human Resilience Map"),
                                        html.Div(
                                            "Climate intelligence · Planning workspace",
                                            className="tag",
                                        ),
                                    ]
                                ),
                            ],
                            className="brand",
                        ),
                        html.Div(
                            [
                                html.Label("Study area", className="field-label", htmlFor="area"),
                                dcc.Dropdown(
                                    id="area",
                                    options=[{"label": a["name"], "value": a["id"]} for a in areas],
                                    value=first,
                                    clearable=False,
                                    searchable=True,
                                ),
                            ],
                            className="field",
                        ),
                        html.Div(
                            [
                                html.Label("County", className="field-label", htmlFor="county"),
                                dcc.Dropdown(
                                    id="county", options=[], clearable=False, searchable=True
                                ),
                            ],
                            className="field hidden",
                            id="county-field",
                        ),
                        html.Div(className="spacer"),
                        html.Span(
                            settings.environment.upper(),
                            className=f"env-tag {settings.environment}",
                            title="Deployment environment",
                        ),
                        html.Span(
                            settings.local_dev_user
                            if settings.auth_mode == "local"
                            else "Signed in",
                            className="user-tag",
                        ),
                        html.Button(
                            "◐",
                            id="theme-toggle",
                            className="icon-btn",
                            n_clicks=0,
                            title="Switch light / dark theme",
                            **{"aria-label": "Switch light or dark theme"},
                        ),
                        html.A("Sign out", href="/logout", className="btn btn-ghost")
                        if settings.auth_mode == "cognito"
                        else None,
                    ],
                    className="topbar",
                ),
                html.Div(id="synthetic-banner"),
                html.Div(
                    [
                        html.Div(id="model-status", role="status"),
                        html.Button(
                            "Check model connection",
                            id="check-model",
                            n_clicks=0,
                            className="btn btn-ghost",
                        ),
                        html.Div(id="model-check-output", role="status"),
                        dcc.Interval(id="model-poll", interval=30000),
                    ],
                    className="card-b",
                ),
                html.Details(
                    [
                        html.Summary("Ask about this area"),
                        html.P(
                            "Choose a study area and county. The planning box, when set, narrows the selection by tract centroid. Ask about vulnerability, a tract score, missing data, or scenario ideas."
                        ),
                        html.P(
                            "Examples: Which are the top 5 most vulnerable tracts? What data is missing? How should I compare cooling-center scenarios?"
                        ),
                        dcc.Textarea(
                            id="area-question",
                            placeholder="Ask a question about the selected area…",
                            maxLength=2000,
                            style={"width": "100%", "minHeight": "90px"},
                        ),
                        html.Button(
                            "Ask area assistant",
                            id="ask-area",
                            n_clicks=0,
                            className="btn btn-primary",
                        ),
                        dcc.Loading(html.Div(id="area-conversation", role="status")),
                        dcc.Store(id="area-chat", storage_type="memory"),
                    ],
                    className="card-b",
                ),
                html.Div(
                    [html.Div(html.Div("Study areas", className="crumbs"))],
                    id="page-head",
                    className="page-head",
                ),
                html.Main(
                    [
                        html.Div(
                            [
                                html.Div(
                                    [
                                        html.Div("RESILIENCE INTELLIGENCE", className="eyebrow"),
                                        html.H2("Community resilience overview"),
                                        html.P(
                                            "Understand heat vulnerability, prioritize communities, and plan cooling-center investments."
                                        ),
                                    ]
                                ),
                                html.A(
                                    "Data & methodology ↗",
                                    href="#methodology",
                                    className="btn btn-secondary",
                                ),
                            ],
                            className="workspace-heading",
                        ),
                        html.Details(
                            [
                                html.Summary("How to use this tool"),
                                html.Ol(
                                    [
                                        html.Li(
                                            [
                                                html.B("Explore. "),
                                                "Switch map layers (score, heat, canopy, income…) and pick a county.",
                                            ]
                                        ),
                                        html.Li(
                                            [
                                                html.B("Inspect. "),
                                                "Click a tract on the map or in the table to see what drives its score.",
                                            ]
                                        ),
                                        html.Li(
                                            [
                                                html.B("Analyze. "),
                                                "Open Run analysis to find the most vulnerable tracts or compare new cooling-center sites.",
                                            ]
                                        ),
                                    ]
                                ),
                            ],
                            className="guide",
                            open=False,
                        ),
                        html.Section(
                            views.kpis(None, ""),
                            id="kpis",
                            className="kpis",
                            **{"aria-label": "Key figures"},
                        ),
                        html.Div(
                            [
                                html.Div(
                                    [
                                        html.Div(
                                            [
                                                html.Button(
                                                    "Map",
                                                    id="lv-map",
                                                    className="active",
                                                    n_clicks=0,
                                                ),
                                                html.Button(
                                                    "Insights", id="lv-insights", n_clicks=0
                                                ),
                                            ],
                                            className="seg",
                                            role="tablist",
                                        ),
                                        html.Div(
                                            dcc.Loading(
                                                html.Div(id="insights"),
                                                overlay_style={
                                                    "visibility": "visible",
                                                    "opacity": 0.5,
                                                },
                                            ),
                                            id="c-insights",
                                            className="hidden",
                                        ),
                                        html.Section(
                                            [
                                                html.Div(
                                                    [
                                                        html.H2("Geographic intelligence"),
                                                        html.Div(
                                                            [
                                                                html.Button(
                                                                    lyr.label,
                                                                    id={
                                                                        "type": "layer",
                                                                        "key": lyr.key,
                                                                    },
                                                                    className="layer-chip active"
                                                                    if lyr.key == "score"
                                                                    else "layer-chip",
                                                                    n_clicks=0,
                                                                )
                                                                for lyr in views.LAYERS.values()
                                                            ],
                                                            className="layer-chips",
                                                            role="group",
                                                            **{"aria-label": "Map layer"},
                                                        ),
                                                    ],
                                                    className="card-h",
                                                ),
                                                html.Div(
                                                    dcc.Loading(
                                                        dcc.Graph(
                                                            id="map",
                                                            config={
                                                                "displayModeBar": False,
                                                                "responsive": True,
                                                            },
                                                            responsive=True,
                                                            style={
                                                                "width": "100%",
                                                                "height": "560px",
                                                            },
                                                        ),
                                                        parent_style={"height": "560px"},
                                                    ),
                                                    className="map-wrap",
                                                ),
                                                html.Div(id="legend"),
                                                html.Div(
                                                    [
                                                        html.H4(
                                                            "Selected planning area and source readiness"
                                                        ),
                                                        html.P(
                                                            "Zoom/pan the map, then use its bounds or enter west,south,east,north. US planning only; max 2500 km² / 500 tract centroids."
                                                        ),
                                                        dcc.Input(
                                                            id="planning-bbox",
                                                            placeholder="-97.8,30.2,-97.7,30.3",
                                                            type="text",
                                                        ),
                                                        html.Button(
                                                            "Use map bounds",
                                                            id="use-bounds",
                                                            n_clicks=0,
                                                        ),
                                                        html.Button(
                                                            "Check sources",
                                                            id="check-readiness",
                                                            n_clicks=0,
                                                        ),
                                                        html.Div(id="readiness-output"),
                                                        html.H4(
                                                            "Current monitoring (separate from historical index)"
                                                        ),
                                                        html.P(
                                                            "Weather at the selected box midpoint and a nearby reporting station. This does not establish conditions throughout the area."
                                                        ),
                                                        html.Button(
                                                            "Refresh monitoring",
                                                            id="refresh-monitoring",
                                                            n_clicks=0,
                                                        ),
                                                        html.Div(id="monitoring-output"),
                                                        html.Details(
                                                            [
                                                                html.Summary(
                                                                    "Download actual data for the selected region"
                                                                ),
                                                                dcc.Input(
                                                                    id="prep-state",
                                                                    value="48",
                                                                    placeholder="State FIPS",
                                                                ),
                                                                dcc.Input(
                                                                    id="prep-county",
                                                                    value="453",
                                                                    placeholder="County FIPS",
                                                                ),
                                                                html.Button(
                                                                    "Download Census / ACS",
                                                                    id="start-prep",
                                                                    n_clicks=0,
                                                                ),
                                                                html.Button(
                                                                    "Download Census / ACS + LA cooling",
                                                                    id="start-cooling-prep",
                                                                    n_clicks=0,
                                                                ),
                                                                html.Button(
                                                                    "Save source ZIP",
                                                                    id="save-prep",
                                                                    n_clicks=0,
                                                                ),
                                                                dcc.Download(id="prep-download"),
                                                                html.Div(id="prep-output"),
                                                                dcc.Store(id="prep-job"),
                                                                dcc.Interval(
                                                                    id="prep-poll",
                                                                    interval=5000,
                                                                    disabled=True,
                                                                ),
                                                                html.P(
                                                                    "Uses the selected map bounds within the specified US county. Requires operator enablement and a Census API key. Download the source package, enrich heat/canopy and import verified inventories before publication."
                                                                ),
                                                            ]
                                                        ),
                                                        html.Details(
                                                            [
                                                                html.Summary(
                                                                    "Upload region data or cooling centers"
                                                                ),
                                                                html.P(
                                                                    "Choose a published study area above for a cooling upload. CSV replaces that area's entire cooling inventory, including centers outside the map bounds. For a new region, upload a source ZIP and enter its ID and name."
                                                                ),
                                                                field(
                                                                    "Upload type",
                                                                    dcc.Dropdown(
                                                                        id="import-kind",
                                                                        options=[
                                                                            {
                                                                                "label": "Cooling centers CSV (selected study area)",
                                                                                "value": "cooling",
                                                                            },
                                                                            {
                                                                                "label": "Complete region source ZIP",
                                                                                "value": "region",
                                                                            },
                                                                        ],
                                                                        value="cooling",
                                                                        clearable=False,
                                                                    ),
                                                                ),
                                                                field(
                                                                    "Region ID (ZIP only)",
                                                                    dcc.Input(
                                                                        id="import-area",
                                                                        placeholder="los-angeles-county",
                                                                    ),
                                                                ),
                                                                field(
                                                                    "Region name (ZIP only)",
                                                                    dcc.Input(
                                                                        id="import-name",
                                                                        placeholder="Los Angeles County",
                                                                    ),
                                                                ),
                                                                field(
                                                                    "Inventory source (CSV only)",
                                                                    dcc.Input(
                                                                        id="import-source",
                                                                        placeholder="Authority URL or evidence reference",
                                                                    ),
                                                                ),
                                                                dcc.Upload(
                                                                    id="import-file",
                                                                    children=html.Button(
                                                                        "Choose CSV or ZIP"
                                                                    ),
                                                                    multiple=False,
                                                                    max_size=20000000,
                                                                ),
                                                                html.Div(
                                                                    id="import-filename",
                                                                    role="status",
                                                                ),
                                                                html.Button(
                                                                    "Download cooling CSV template",
                                                                    id="cooling-template",
                                                                    n_clicks=0,
                                                                ),
                                                                html.Button(
                                                                    "Validate upload",
                                                                    id="validate-upload",
                                                                    n_clicks=0,
                                                                ),
                                                                html.Button(
                                                                    "Publish validated dataset",
                                                                    id="publish-upload",
                                                                    n_clicks=0,
                                                                    disabled=True,
                                                                ),
                                                                html.Button(
                                                                    "Download selected dataset sources",
                                                                    id="dataset-sources",
                                                                    n_clicks=0,
                                                                ),
                                                                dcc.Download(id="import-download"),
                                                                dcc.Store(id="import-preview"),
                                                                html.Div(
                                                                    id="import-output",
                                                                    role="status",
                                                                ),
                                                                html.P(
                                                                    "ZIP files must contain tracts.geojson, indicators.csv, centers.csv, candidates.csv and manifest.json at the root; roads.json is optional. Limit: 20 MB upload, 80 MB expanded. Heat/canopy gaps must be completed before publication. Online cooling downloads currently support LA County (state 06, county 037); other counties need local inventory uploads."
                                                                ),
                                                            ]
                                                        ),
                                                    ],
                                                    className="card-b",
                                                ),
                                                html.Div(
                                                    id="map-notice", style={"padding": "0 16px"}
                                                ),
                                            ],
                                            className="card",
                                            id="c-map",
                                        ),
                                        html.Section(
                                            [
                                                html.Div(
                                                    [
                                                        html.H2("Census tract directory"),
                                                        html.Div(
                                                            [
                                                                html.Label(
                                                                    "Search tracts",
                                                                    htmlFor="search",
                                                                    className="sr-only",
                                                                ),
                                                                dcc.Input(
                                                                    id="search",
                                                                    type="text",
                                                                    placeholder="Search tract, name or county",
                                                                    debounce=True,
                                                                    className="search",
                                                                ),
                                                            ],
                                                            className="toolbar",
                                                        ),
                                                    ],
                                                    className="card-h",
                                                ),
                                                html.Div(
                                                    views.tracts_table("table"), className="card-b"
                                                ),
                                            ],
                                            className="card",
                                        ),
                                    ],
                                    className="col",
                                ),
                                html.Div(
                                    [
                                        html.Div(
                                            [
                                                html.Button(
                                                    "Tract details",
                                                    id="btn-tract",
                                                    className="active",
                                                    n_clicks=0,
                                                ),
                                                html.Button(
                                                    "Run analysis", id="btn-analysis", n_clicks=0
                                                ),
                                                html.Button(
                                                    "Results", id="btn-results", n_clicks=0
                                                ),
                                            ],
                                            className="seg",
                                            role="tablist",
                                        ),
                                        # ---- tract details
                                        html.Section(
                                            html.Div(
                                                views.empty_state(
                                                    "◎",
                                                    "No tract selected",
                                                    "Click a tract on the map or a row in the table to see its score and what drives it.",
                                                ),
                                                id="tract-card",
                                                className="card-b",
                                            ),
                                            id="p-tract",
                                            className="card",
                                        ),
                                        # ---- run analysis
                                        html.Section(
                                            html.Div(
                                                [
                                                    html.Div(
                                                        "Try an example",
                                                        className="section-t",
                                                        style={"marginTop": 0},
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Button(
                                                                "Where to open 3 cooling centers",
                                                                id="preset-siting",
                                                                className="btn btn-secondary btn-sm",
                                                                n_clicks=0,
                                                            ),
                                                            html.Button(
                                                                "Most vulnerable tracts",
                                                                id="preset-rank",
                                                                className="btn btn-secondary btn-sm",
                                                                n_clicks=0,
                                                            ),
                                                        ],
                                                        className="presets",
                                                    ),
                                                    field(
                                                        "Your question",
                                                        dcc.Textarea(
                                                            id="question",
                                                            value=PRESET_SITING,
                                                            maxLength=1000,
                                                        ),
                                                        "Plain English. Details you don't give (travel mode, minutes) will be asked for.",
                                                    ),
                                                    html.Div(
                                                        "Optional constraints (these override the question text)",
                                                        className="section-t",
                                                    ),
                                                    html.Div(
                                                        [
                                                            field(
                                                                "New centers (1-3)",
                                                                dcc.Input(
                                                                    id="c-num",
                                                                    type="number",
                                                                    min=1,
                                                                    max=3,
                                                                    step=1,
                                                                    placeholder="e.g. 3",
                                                                ),
                                                            ),
                                                            field(
                                                                "Travel mode",
                                                                dcc.Dropdown(
                                                                    id="c-mode",
                                                                    options=MODES,
                                                                    placeholder="Select",
                                                                ),
                                                            ),
                                                            field(
                                                                "Max travel time (min)",
                                                                dcc.Input(
                                                                    id="c-min",
                                                                    type="number",
                                                                    min=1,
                                                                    max=120,
                                                                    placeholder="e.g. 20",
                                                                ),
                                                            ),
                                                            field(
                                                                "Accessibility model",
                                                                dcc.Dropdown(
                                                                    id="c-access",
                                                                    options=[
                                                                        {
                                                                            "label": "Use question (otherwise proxy)",
                                                                            "value": "from_question",
                                                                        },
                                                                        {
                                                                            "label": "Straight-line proxy (illustrative)",
                                                                            "value": "straight_line_proxy",
                                                                        },
                                                                        {
                                                                            "label": "Pinned road network (required snapshot)",
                                                                            "value": "road_network",
                                                                        },
                                                                    ],
                                                                    value="from_question",
                                                                    clearable=False,
                                                                ),
                                                            ),
                                                            field(
                                                                "Budget (USD)",
                                                                dcc.Input(
                                                                    id="c-budget",
                                                                    type="number",
                                                                    min=0,
                                                                    placeholder="optional",
                                                                ),
                                                            ),
                                                        ],
                                                        className="form-grid",
                                                    ),
                                                    html.Details(
                                                        [
                                                            html.Summary("Compare an alternative"),
                                                            dcc.Checklist(
                                                                id="compare-enabled",
                                                                options=[
                                                                    {
                                                                        "label": " Run this alternative",
                                                                        "value": "enabled",
                                                                    }
                                                                ],
                                                                value=[],
                                                            ),
                                                            html.Div(
                                                                [
                                                                    field(
                                                                        "Alternative ID",
                                                                        dcc.Input(
                                                                            id="cmp-id",
                                                                            value="alternative-1",
                                                                        ),
                                                                    ),
                                                                    field(
                                                                        "New centers",
                                                                        dcc.Input(
                                                                            id="cmp-num",
                                                                            type="number",
                                                                            min=1,
                                                                            max=3,
                                                                        ),
                                                                    ),
                                                                    field(
                                                                        "Travel mode",
                                                                        dcc.Dropdown(
                                                                            id="cmp-mode",
                                                                            options=MODES,
                                                                        ),
                                                                    ),
                                                                    field(
                                                                        "Max minutes",
                                                                        dcc.Input(
                                                                            id="cmp-min",
                                                                            type="number",
                                                                            min=1,
                                                                            max=120,
                                                                        ),
                                                                    ),
                                                                    field(
                                                                        "Budget (USD)",
                                                                        dcc.Input(
                                                                            id="cmp-budget",
                                                                            type="number",
                                                                            min=0,
                                                                        ),
                                                                    ),
                                                                    field(
                                                                        "Candidate IDs",
                                                                        dcc.Input(
                                                                            id="cmp-candidates",
                                                                            placeholder="SITE-1, SITE-2",
                                                                        ),
                                                                    ),
                                                                ],
                                                                className="form-grid",
                                                            ),
                                                        ]
                                                    ),
                                                    html.Details(
                                                        [
                                                            html.Summary("Test sensitivity"),
                                                            dcc.Checklist(
                                                                id="sensitivity-enabled",
                                                                options=[
                                                                    {
                                                                        "label": " Run bounded variants",
                                                                        "value": "enabled",
                                                                    }
                                                                ],
                                                                value=[],
                                                            ),
                                                            field(
                                                                "Parameter",
                                                                dcc.Dropdown(
                                                                    id="sensitivity-parameter",
                                                                    options=[
                                                                        {
                                                                            "label": "Routing speed",
                                                                            "value": "routing_speed_multiplier",
                                                                        },
                                                                        {
                                                                            "label": "Candidate capacity",
                                                                            "value": "candidate_capacity_multiplier",
                                                                        },
                                                                        {
                                                                            "label": "Heat-index weight",
                                                                            "value": "surface_temp_anomaly_c",
                                                                        },
                                                                    ],
                                                                    value="routing_speed_multiplier",
                                                                    clearable=False,
                                                                ),
                                                            ),
                                                            field(
                                                                "Multipliers (0.5-1.5, comma separated)",
                                                                dcc.Input(
                                                                    id="sensitivity-values",
                                                                    value="0.8,1.2",
                                                                ),
                                                                "At most four variants; runs are sequential.",
                                                            ),
                                                        ]
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Button(
                                                                "Run analysis",
                                                                id="submit",
                                                                className="btn btn-primary",
                                                                n_clicks=0,
                                                            ),
                                                            html.Button(
                                                                "Reset",
                                                                id="reset",
                                                                className="btn btn-secondary",
                                                                n_clicks=0,
                                                            ),
                                                        ],
                                                        className="btn-row",
                                                    ),
                                                    html.Div(
                                                        id="submit-msg", **{"aria-live": "polite"}
                                                    ),
                                                    html.Div(
                                                        "Analyses run in the background and are checked before they are shown. "
                                                        "Choose proxy or pinned road routing. Index and display bands are illustrative planning measures, not validated health predictions. Internal checks establish consistency only. Recommendations require data readiness and human review.",
                                                        className="hint",
                                                        style={"marginTop": "10px"},
                                                    ),
                                                ],
                                                className="card-b",
                                            ),
                                            id="p-analysis",
                                            className="card hidden",
                                        ),
                                        # ---- results
                                        html.Div(
                                            [
                                                html.Section(
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                id="job-status",
                                                                **{"aria-live": "polite"},
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        "More information is needed:",
                                                                        className="section-t",
                                                                    ),
                                                                    html.Div(id="clarify-needs"),
                                                                    html.Div(
                                                                        [
                                                                            field(
                                                                                "New centers",
                                                                                dcc.Input(
                                                                                    id="q-num",
                                                                                    type="number",
                                                                                    min=1,
                                                                                    max=3,
                                                                                    step=1,
                                                                                ),
                                                                            ),
                                                                            field(
                                                                                "Travel mode",
                                                                                dcc.Dropdown(
                                                                                    id="q-mode",
                                                                                    options=MODES,
                                                                                ),
                                                                            ),
                                                                            field(
                                                                                "Max minutes",
                                                                                dcc.Input(
                                                                                    id="q-min",
                                                                                    type="number",
                                                                                    min=1,
                                                                                    max=120,
                                                                                ),
                                                                            ),
                                                                            field(
                                                                                "Tract id",
                                                                                dcc.Input(
                                                                                    id="q-tract",
                                                                                    type="text",
                                                                                ),
                                                                            ),
                                                                        ],
                                                                        className="form-grid",
                                                                    ),
                                                                    html.Button(
                                                                        "Continue analysis",
                                                                        id="clarify-submit",
                                                                        className="btn btn-primary",
                                                                        n_clicks=0,
                                                                    ),
                                                                ],
                                                                id="clarify-box",
                                                                className="hidden",
                                                            ),
                                                            dcc.Loading(
                                                                html.Div(
                                                                    views.empty_state(
                                                                        "▥",
                                                                        "No analysis yet",
                                                                        "Run an analysis to see results here.",
                                                                    ),
                                                                    id="results",
                                                                )
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Button(
                                                                        "Download CSV",
                                                                        id="dl-csv",
                                                                        className="btn btn-secondary btn-sm",
                                                                        n_clicks=0,
                                                                    ),
                                                                    html.Button(
                                                                        "Download GeoJSON",
                                                                        id="dl-geojson",
                                                                        className="btn btn-secondary btn-sm",
                                                                        n_clicks=0,
                                                                    ),
                                                                    html.Button(
                                                                        "Download summary",
                                                                        id="dl-summary",
                                                                        className="btn btn-secondary btn-sm",
                                                                        n_clicks=0,
                                                                    ),
                                                                    dcc.Download(id="download"),
                                                                ],
                                                                id="export-box",
                                                                className="hidden btn-row",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        "Planner review of site suitability",
                                                                        className="section-t",
                                                                    ),
                                                                    html.Div(
                                                                        "An analysis does not open a cooling center; local review confirms operating suitability.",
                                                                        className="hint",
                                                                    ),
                                                                    field(
                                                                        "Site",
                                                                        dcc.Dropdown(
                                                                            id="review-site",
                                                                            placeholder="Recommended site",
                                                                        ),
                                                                    ),
                                                                    field(
                                                                        "Decision",
                                                                        dcc.Dropdown(
                                                                            id="review-decision",
                                                                            options=[
                                                                                "approved",
                                                                                "rejected",
                                                                                "needs_followup",
                                                                            ],
                                                                            placeholder="Decision",
                                                                        ),
                                                                    ),
                                                                    field(
                                                                        "Notes",
                                                                        dcc.Input(
                                                                            id="review-notes",
                                                                            type="text",
                                                                            maxLength=1000,
                                                                            placeholder="Optional",
                                                                        ),
                                                                    ),
                                                                    html.Button(
                                                                        "Record review",
                                                                        id="review-submit",
                                                                        className="btn btn-secondary",
                                                                        n_clicks=0,
                                                                    ),
                                                                    html.Div(
                                                                        id="review-msg",
                                                                        **{"aria-live": "polite"},
                                                                    ),
                                                                ],
                                                                id="review-box",
                                                                className="hidden",
                                                            ),
                                                        ],
                                                        className="card-b",
                                                    ),
                                                    className="card",
                                                ),
                                                html.Section(
                                                    [
                                                        html.Div(
                                                            html.H2("Recent analyses"),
                                                            className="card-h",
                                                        ),
                                                        html.Div(id="history", className="card-b"),
                                                    ],
                                                    className="card",
                                                    style={"marginTop": "16px"},
                                                ),
                                            ],
                                            id="p-results",
                                            className="hidden",
                                        ),
                                    ],
                                    className="col sticky",
                                ),
                            ],
                            className="workspace",
                        ),
                        html.Details(
                            [
                                html.Summary("Data sources & methodology"),
                                html.Div(id="methods", className="card-b"),
                            ],
                            className="card methods",
                            id="methodology",
                        ),
                    ],
                    id="main",
                ),
                html.Footer(
                    [
                        html.Span(
                            [
                                html.Span(className="status-dot"),
                                f"Human Resilience Map v{__version__} · a planning aid, not a prediction of individual health outcomes.",
                            ]
                        ),
                        html.Span(
                            "Basemap © OpenStreetMap contributors © CARTO · Census, USGS and USFS public data."
                        ),
                    ]
                ),
                dcc.Store(id="map-data"),
                dcc.Store(id="view"),
                dcc.Store(id="job-store"),
                dcc.Store(id="overlay", data=[]),
                dcc.Store(id="selected-tract"),
                dcc.Store(id="panel", data="tract"),
                dcc.Store(id="left-view", data="map"),
                dcc.Store(id="layer", data="score"),
                dcc.Store(id="theme", storage_type="local"),
                dcc.Interval(id="poll", interval=1500, disabled=True),
            ],
            id="app-root",
        )

    app.layout = serve_layout

    # ------------------------------------------------------------------ theme (clientside)
    app.clientside_callback(
        ClientsideFunction("ui", "theme"),
        Output("theme", "data"),
        Input("theme-toggle", "n_clicks"),
        State("theme", "data"),
    )

    # ------------------------------------------------------------------ study area / county / data
    @app.callback(
        Output("county", "options"),
        Output("county", "value"),
        Output("county-field", "className"),
        Output("view", "data"),
        Input("area", "value"),
    )
    @guard(4)
    def on_area(area: str | None) -> Any:
        if not area:
            return [], None, "field hidden", None
        counties = api.get("/v1/counties", auth(), study_area_id=area)
        total = sum(c["tracts"] for c in counties)
        big = total > settings.max_map_features
        options = (
            [] if big else [{"label": f"All counties ({total:,} tracts)", "value": "ALL"}]
        ) + [
            {"label": f"{c['name']} ({c['tracts']:,} tracts)", "value": c["county_id"]}
            for c in counties
        ]
        value = counties[0]["county_id"] if (big and counties) else "ALL"
        return (
            options,
            value,
            "field" if len(counties) > 1 else "field hidden",
            {"area": area, "county": value},
        )

    @app.callback(
        Output("view", "data", allow_duplicate=True),
        Input("county", "value"),
        State("area", "value"),
        State("view", "data"),
        prevent_initial_call=True,
    )
    def on_county(county: str | None, area: str | None, view: Any) -> Any:
        if (
            not county
            or not area
            or (view and view.get("area") == area and view.get("county") == county)
        ):
            return no_update
        return {"area": area, "county": county}

    @app.callback(
        Output("map-data", "data"),
        Output("page-head", "children"),
        Output("synthetic-banner", "children"),
        Output("map-notice", "children"),
        Input("view", "data"),
    )
    @guard(4, 3)
    def load_data(view: Any) -> Any:
        if not view:
            return None, "", None, ""
        a, area = auth(), view["area"]
        county = None if view["county"] == "ALL" else view["county"]
        tr = api.get("/v1/tracts", a, study_area_id=area, county=county)
        areas = {x["id"]: x for x in api.get("/v1/study-areas", a)}
        synthetic = areas[area]["is_synthetic"]
        data = {
            "tracts": {"type": "FeatureCollection", "features": tr["features"]},
            "dataset": tr["dataset"],
            "counties": tr["counties"],
            "study_area_id": area,
            "county": county,
            "centers": api.get("/v1/cooling-centers", a, study_area_id=area),
            "candidates": api.get("/v1/candidate-sites", a, study_area_id=area),
        }
        county_name = next((c["name"] for c in tr["counties"] if c["county_id"] == county), None)
        head = views.page_head(areas[area]["name"], county_name, tr["dataset"], synthetic)
        banner = (
            html.Div(
                "Synthetic demonstration data: not a real place.", className="banner", role="status"
            )
            if synthetic
            else None
        )
        notice = ""
        if tr.get("truncated"):
            notice = views.alert(
                "warn", html.Span("Showing the first tracts only. Choose a county to see them all.")
            )
        elif not tr["features"]:
            notice = views.alert("info", html.Span("This selection contains no tracts."))
        return data, head, banner, notice

    @app.callback(
        Output("map", "figure"),
        Output("legend", "children"),
        Input("map-data", "data"),
        Input("layer", "data"),
        Input("theme", "data"),
        Input("overlay", "data"),
        Input("selected-tract", "data"),
    )
    def render_map(
        data: Any, layer: str | None, theme: str | None, overlay: Any, selected: Any
    ) -> Any:
        layer = layer if layer in views.LAYERS else "score"
        return (
            views.build_map(data, layer, theme or "light", overlay or [], selected),
            views.legend(data, layer, bool(overlay)),
        )

    @app.callback(Output("kpis", "children"), Input("map-data", "data"))
    def render_kpis(data: Any) -> Any:
        if not data:
            return views.kpis(None, "")
        names = {c["county_id"]: c["name"] for c in data["counties"]}
        scope = names.get(data["county"] or "", "") or "Whole study area"
        return views.kpis(data, scope)

    @app.callback(Output("table", "data"), Input("map-data", "data"), Input("search", "value"))
    def render_table(data: Any, search: str | None) -> Any:
        names = {c["county_id"]: c["name"] for c in (data or {}).get("counties", [])}
        return views.table_rows(data, search, names)

    # ------------------------------------------------------------------ left view (map / insights)
    @app.callback(
        Output("left-view", "data"),
        Input("lv-map", "n_clicks"),
        Input("lv-insights", "n_clicks"),
        prevent_initial_call=True,
    )
    def switch_left(*_: Any) -> Any:
        return {"lv-map": "map", "lv-insights": "insights"}.get(ctx.triggered_id, no_update)

    @app.callback(
        Output("lv-map", "className"),
        Output("lv-insights", "className"),
        Output("c-map", "className"),
        Output("c-insights", "className"),
        Input("left-view", "data"),
    )
    def apply_left(view: str | None) -> Any:
        insights = view == "insights"
        return (
            "" if insights else "active",
            "active" if insights else "",
            "card hidden" if insights else "card",
            "" if insights else "hidden",
        )

    @app.callback(Output("insights", "children"), Input("map-data", "data"), Input("theme", "data"))
    def render_insights(data: Any, theme: str | None) -> Any:
        return views.insights_panel(charts.build_all(data, theme or "light"))

    # ------------------------------------------------------------------ layers
    @app.callback(
        Output("layer", "data"),
        Output({"type": "layer", "key": ALL}, "className"),
        Input({"type": "layer", "key": ALL}, "n_clicks"),
        State("layer", "data"),
    )
    def set_layer(_: Any, current: str | None) -> Any:
        trig = ctx.triggered_id
        key = trig["key"] if isinstance(trig, dict) else (current or "score")
        return key, ["layer-chip active" if k == key else "layer-chip" for k in views.LAYERS]

    # ------------------------------------------------------------------ panels
    @app.callback(
        Output("panel", "data"),
        Input("btn-tract", "n_clicks"),
        Input("btn-analysis", "n_clicks"),
        Input("btn-results", "n_clicks"),
        prevent_initial_call=True,
    )
    def switch_panel(*_: Any) -> Any:
        return {"btn-tract": "tract", "btn-analysis": "analysis", "btn-results": "results"}.get(
            ctx.triggered_id, no_update
        )

    @app.callback(
        *[Output(f"btn-{p}", "className") for p in PANELS],
        *[Output(f"p-{p}", "className") for p in PANELS],
        Input("panel", "data"),
    )
    def apply_panel(panel: str | None) -> Any:
        panel = panel if panel in PANELS else "tract"
        btn = ["active" if p == panel else "" for p in PANELS]
        body = [
            ("card" if p != "results" else "") + ("" if p == panel else " hidden") for p in PANELS
        ]
        return (*btn, *[b.strip() for b in body])

    # ------------------------------------------------------------------ tract selection + card
    @app.callback(
        Output("selected-tract", "data"),
        Output("panel", "data", allow_duplicate=True),
        Input("map", "clickData"),
        prevent_initial_call=True,
    )
    def pick_from_map(click: Any) -> Any:
        tid = (click or {}).get("points", [{}])[0].get("location") if click else None
        return (tid, "tract") if tid else (no_update, no_update)

    @app.callback(
        Output("selected-tract", "data", allow_duplicate=True),
        Output("panel", "data", allow_duplicate=True),
        Input("table", "active_cell"),
        prevent_initial_call=True,
    )
    def pick_from_table(cell: Any) -> Any:
        tid = (cell or {}).get("row_id")
        return (tid, "tract") if tid else (no_update, no_update)

    @app.callback(
        Output("tract-card", "children"), Input("selected-tract", "data"), State("map-data", "data")
    )
    @guard(1, 0)
    def show_tract(tid: str | None, data: Any) -> Any:
        if not tid or not data:
            return no_update
        d = api.get(
            f"/v1/tracts/{tid}",
            auth(),
            study_area_id=data["study_area_id"],
            dataset_version_id=data["dataset"]["id"],
        )
        scored = sorted(
            f["properties"]["score"]
            for f in data["tracts"]["features"]
            if f["properties"]["score"] is not None
        )
        rank = (
            (
                sum(1 for s in scored if s <= (d["score"] if d["score"] is not None else -1)),
                len(scored),
            )
            if scored and d["score"] is not None
            else None
        )
        county = next((c["name"] for c in data["counties"] if c["county_id"] == tid[:5]), None)
        return views.tract_card(d, rank, county)

    # ------------------------------------------------------------------ question helpers
    @app.callback(
        Output("question", "value"),
        Output("c-num", "value"),
        Output("c-mode", "value"),
        Output("c-min", "value"),
        Output("c-budget", "value"),
        Output("panel", "data", allow_duplicate=True),
        Input("preset-siting", "n_clicks"),
        Input("preset-rank", "n_clicks"),
        Input("explain-tract", "n_clicks"),
        Input("reset", "n_clicks"),
        State("selected-tract", "data"),
        prevent_initial_call=True,
    )
    def fill_question(_a: Any, _b: Any, _c: Any, _d: Any, tract: str | None) -> Any:
        t = ctx.triggered_id
        if not any(x for x in (_a, _b, _c, _d)):
            return (no_update,) * 6
        text = {
            "preset-siting": PRESET_SITING,
            "preset-rank": PRESET_RANK,
            "reset": "",
            "explain-tract": f"Why does tract {tract} have its score?",
        }.get(t, no_update)
        return text, None, None, None, None, "analysis"

    # ------------------------------------------------------------------ submit / clarify / poll
    @app.callback(
        Output("job-store", "data"),
        Output("poll", "disabled"),
        Output("submit-msg", "children"),
        Output("panel", "data", allow_duplicate=True),
        Input("submit", "n_clicks"),
        State("question", "value"),
        State("area", "value"),
        State("c-num", "value"),
        State("c-mode", "value"),
        State("c-min", "value"),
        State("c-budget", "value"),
        State("c-access", "value"),
        State("planning-bbox", "value"),
        State("compare-enabled", "value"),
        State("cmp-id", "value"),
        State("cmp-num", "value"),
        State("cmp-mode", "value"),
        State("cmp-min", "value"),
        State("cmp-budget", "value"),
        State("cmp-candidates", "value"),
        State("sensitivity-enabled", "value"),
        State("sensitivity-parameter", "value"),
        State("sensitivity-values", "value"),
        prevent_initial_call=True,
    )
    @guard(4, 2)
    def submit(
        _: int,
        question: str,
        area: str,
        num: Any,
        mode: Any,
        mins: Any,
        budget: Any,
        access: str = "from_question",
        bbox: str | None = None,
        compare_enabled: list[str] | None = None,
        comparison_id: str | None = None,
        comparison_num: Any = None,
        comparison_mode: Any = None,
        comparison_minutes: Any = None,
        comparison_budget: Any = None,
        comparison_candidates: str | None = None,
        sensitivity_enabled: list[str] | None = None,
        sensitivity_parameter: str = "routing_speed_multiplier",
        sensitivity_values: str | None = None,
    ) -> Any:
        if not question or len(question.strip()) < 3:
            return (
                no_update,
                no_update,
                views.error_box("Please enter a question (at least 3 characters)."),
                no_update,
            )
        cons = {
            k: v
            for k, v in {
                "num_centers": num,
                "travel_mode": mode,
                "threshold_minutes": mins,
                "budget_usd": budget,
            }.items()
            if v not in (None, "")
        }
        if access in {"straight_line_proxy", "road_network"}:
            cons["access_model"] = access
        if bbox:
            try:
                cons["bbox"] = parse_bbox(bbox)
            except ValueError as exc:
                return no_update, no_update, views.error_box(str(exc)), no_update
        payload: dict[str, Any] = {
            "question": question.strip(),
            "study_area_id": area,
            "constraints": cons,
        }
        if "enabled" in (compare_enabled or []):
            alternative = {
                key: value
                for key, value in {
                    "num_centers": comparison_num,
                    "travel_mode": comparison_mode,
                    "threshold_minutes": comparison_minutes,
                    "budget_usd": comparison_budget,
                }.items()
                if value not in (None, "")
            }
            if comparison_candidates:
                alternative["candidate_site_ids"] = [
                    item.strip() for item in comparison_candidates.split(",") if item.strip()
                ]
            payload["comparison"] = {
                "alternatives": [
                    {
                        "scenario_id": comparison_id or "alternative-1",
                        "constraints": alternative,
                    }
                ]
            }
        if "enabled" in (sensitivity_enabled or []):
            try:
                values = [float(value.strip()) for value in (sensitivity_values or "").split(",")]
            except ValueError:
                return (
                    no_update,
                    no_update,
                    views.error_box("Sensitivity values must be numbers."),
                    no_update,
                )
            if not values or len(values) > 4 or any(value < 0.5 or value > 1.5 for value in values):
                return (
                    no_update,
                    no_update,
                    views.error_box("Provide one to four sensitivity multipliers from 0.5 to 1.5."),
                    no_update,
                )
            variants = []
            for index, value in enumerate(values, 1):
                variation: dict[str, Any] = {"variant_id": f"variant-{index}"}
                if sensitivity_parameter in {
                    "routing_speed_multiplier",
                    "candidate_capacity_multiplier",
                }:
                    variation[sensitivity_parameter] = value
                else:
                    variation["planning_index_weight_multipliers"] = {sensitivity_parameter: value}
                variants.append(variation)
            payload["uncertainty"] = {"variants": variants}
        acc = api.post(
            "/v1/analyses",
            auth(),
            payload,
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        return {"job_id": acc["job_id"]}, False, "", "results"

    @app.callback(
        Output("job-store", "data", allow_duplicate=True),
        Output("poll", "disabled", allow_duplicate=True),
        Output("submit-msg", "children", allow_duplicate=True),
        Input("clarify-submit", "n_clicks"),
        State("job-store", "data"),
        State("q-num", "value"),
        State("q-mode", "value"),
        State("q-min", "value"),
        State("q-tract", "value"),
        prevent_initial_call=True,
    )
    @guard(3, 2)
    def clarify(_: int, job: Any, num: Any, mode: Any, mins: Any, tract: Any) -> Any:
        body = {
            k: v
            for k, v in {
                "num_centers": num,
                "travel_mode": mode,
                "threshold_minutes": mins,
                "tract_id": tract,
            }.items()
            if v not in (None, "")
        }
        api.post(f"/v1/analyses/{job['job_id']}/clarifications", auth(), body)
        return job, False, ""

    @app.callback(
        Output("job-status", "children"),
        Output("results", "children"),
        Output("poll", "disabled", allow_duplicate=True),
        Output("clarify-box", "className"),
        Output("clarify-needs", "children"),
        Output("export-box", "className"),
        Output("review-box", "className"),
        Output("review-site", "options"),
        Output("overlay", "data"),
        Input("poll", "n_intervals"),
        Input("job-store", "data"),
        prevent_initial_call=True,
    )
    def poll(_: Any, job: Any) -> Any:
        hidden = "hidden"
        if not job:
            return "", no_update, True, hidden, "", "hidden btn-row", hidden, [], []
        try:
            res = api.get(f"/v1/analyses/{job['job_id']}", auth())
        except ApiError as e:
            if e.status == 401:
                abort(401)
            return views.error_box(e.detail), "", True, hidden, "", "hidden btn-row", hidden, [], []
        st = res["status"]
        head = views.job_header(res)
        if st == "needs_input":
            needs = html.Ul([html.Li(n) for n in (res.get("clarification") or {}).get("needs", [])])
            return head, "", True, "", needs, "hidden btn-row", hidden, [], []
        if st == "failed":
            return (
                head,
                views.error_box(res.get("error") or "The analysis failed."),
                True,
                hidden,
                "",
                "hidden btn-row",
                hidden,
                [],
                [],
            )
        if st in TERMINAL:
            result = res["result"]
            ok = result["outcome"] == "completed"
            sites = ((result.get("analysis") or {}).get("selected_sites") or []) if ok else []
            return (
                head,
                views.result_panel(result),
                True,
                hidden,
                "",
                "btn-row" if ok else "hidden btn-row",
                "" if sites else hidden,
                [{"label": f"{s['name']} ({s['site_id']})", "value": s["site_id"]} for s in sites],
                [{k: s[k] for k in ("site_id", "name", "lon", "lat")} for s in sites],
            )
        return head, no_update, False, hidden, "", "hidden btn-row", hidden, [], []

    @app.callback(
        Output("history", "children"),
        Input("job-store", "data"),
        Input("panel", "data"),
        Input("poll", "disabled"),
    )
    @guard(1, 0)
    def history(_a: Any, panel: str | None, _c: Any) -> Any:
        if panel != "results":
            return no_update
        return views.history_list(api.get("/v1/analyses", auth()))

    @app.callback(
        Output("job-store", "data", allow_duplicate=True),
        Output("poll", "disabled", allow_duplicate=True),
        Input({"type": "hist", "job": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_history(clicks: Any) -> Any:
        trig = ctx.triggered_id
        if not isinstance(trig, dict) or not any(clicks or []):
            return no_update, no_update
        return {"job_id": trig["job"]}, False

    # ------------------------------------------------------------------ exports / review / methodology
    @app.callback(
        Output("download", "data"),
        Input("dl-csv", "n_clicks"),
        Input("dl-geojson", "n_clicks"),
        Input("dl-summary", "n_clicks"),
        State("job-store", "data"),
        prevent_initial_call=True,
    )
    def download(_a: Any, _b: Any, _c: Any, job: Any) -> Any:
        fmt = {"dl-csv": "csv", "dl-geojson": "geojson", "dl-summary": "summary"}.get(
            ctx.triggered_id
        )
        if not fmt or not job:
            return no_update
        try:
            body = api.get(f"/v1/analyses/{job['job_id']}/exports", auth(), format=fmt)
        except ApiError:
            return no_update
        ext = {"csv": "csv", "geojson": "geojson", "summary": "txt"}[fmt]
        return {
            "content": body if isinstance(body, str) else str(body),
            "filename": f"analysis-{job['job_id']}.{ext}",
        }

    @app.callback(
        Output("review-msg", "children"),
        Input("review-submit", "n_clicks"),
        State("job-store", "data"),
        State("review-site", "value"),
        State("review-decision", "value"),
        State("review-notes", "value"),
        prevent_initial_call=True,
    )
    def review(_: int, job: Any, site: Any, decision: Any, notes: Any) -> Any:
        if not (job and site and decision):
            return views.error_box("Choose a site and a decision.")
        try:
            api.post(
                f"/v1/analyses/{job['job_id']}/reviews",
                auth(),
                {"site_id": site, "decision": decision, "notes": notes or ""},
            )
        except ApiError as e:
            return views.error_box(e.detail)
        return views.alert("ok", html.Span("Review recorded."))

    @app.callback(Output("methods", "children"), Input("map-data", "data"))
    @guard(1, 0)
    def methods(data: Any) -> Any:
        return views.methodology_panel(
            api.get("/v1/methodology", auth()), (data or {}).get("dataset")
        )

    app.clientside_callback(
        """function(n, data) {
          if (!n || !data) return window.dash_clientside.no_update;
          var d = data['map._derived'] || data['mapbox._derived'];
          if (!d || !d.coordinates) return window.dash_clientside.no_update;
          var xs=d.coordinates.map(p=>p[0]), ys=d.coordinates.map(p=>p[1]);
          return [Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)].join(',');
        }""",
        Output("planning-bbox", "value"),
        Input("use-bounds", "n_clicks"),
        State("map", "relayoutData"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("readiness-output", "children"),
        Input("check-readiness", "n_clicks"),
        State("area", "value"),
        State("planning-bbox", "value"),
        prevent_initial_call=True,
    )
    @guard(1, 0)
    def check_readiness(_: int, area: str, bbox: str) -> Any:
        if not bbox:
            return views.error_box("Select a bounded area first")
        import json

        result = api.get("/v1/readiness", auth(), study_area_id=area, bbox=bbox)
        return html.Pre(
            json.dumps(result, indent=2),
            style={"whiteSpace": "pre-wrap", "maxHeight": "350px", "overflow": "auto"},
        )

    @app.callback(
        Output("monitoring-output", "children"),
        Input("refresh-monitoring", "n_clicks"),
        State("planning-bbox", "value"),
        prevent_initial_call=True,
    )
    @guard(1, 0)
    def refresh_monitoring(_: int, bbox: str) -> Any:
        import json

        try:
            w, south, e, north = parse_bbox(bbox or "")
        except ValueError as exc:
            return views.error_box(str(exc))
        result = api.get("/v1/monitoring", auth(), lat=(south + north) / 2, lon=(w + e) / 2)
        return html.Pre(
            json.dumps(result, indent=2),
            style={"whiteSpace": "pre-wrap", "maxHeight": "350px", "overflow": "auto"},
        )

    @app.callback(
        Output("prep-job", "data"),
        Output("prep-poll", "disabled"),
        Output("prep-output", "children"),
        Input("start-prep", "n_clicks"),
        State("prep-state", "value"),
        State("prep-county", "value"),
        State("planning-bbox", "value"),
        prevent_initial_call=True,
    )
    @guard(3, 2)
    def start_prep(_: int, state: str, county: str, bbox: str = "") -> Any:
        try:
            bounds = parse_bbox(bbox)
        except ValueError as exc:
            return no_update, True, views.error_box(str(exc))
        result = api.post(
            "/v1/preparations", auth(), {"state": state, "county": county, "bbox": list(bounds)}
        )
        return result["id"], False, "Preparation queued; downloads are not yet analytical coverage."

    @app.callback(
        Output("prep-output", "children", allow_duplicate=True),
        Output("prep-poll", "disabled", allow_duplicate=True),
        Input("prep-poll", "n_intervals"),
        State("prep-job", "data"),
        prevent_initial_call=True,
    )
    @guard(2, 0)
    def poll_prep(_: int, ident: str) -> Any:
        import json

        result = api.get(f"/v1/preparations/{ident}", auth())
        return html.Pre(json.dumps(result, indent=2)), result["state"] not in {"queued", "running"}

    @app.callback(
        Output("prep-download", "data"),
        Input("save-prep", "n_clicks"),
        State("prep-job", "data"),
        prevent_initial_call=True,
    )
    @guard(1, 0)
    def save_prep(_: int, ident: str | None) -> Any:
        if not ident:
            return no_update
        content = api.request("GET", f"/v1/preparations/{ident}/download", auth(), binary=True)
        return dcc.send_bytes(content, f"region-{ident}.zip")

    @app.callback(
        Output("prep-job", "data", allow_duplicate=True),
        Output("prep-poll", "disabled", allow_duplicate=True),
        Output("prep-output", "children", allow_duplicate=True),
        Input("start-cooling-prep", "n_clicks"),
        State("prep-state", "value"),
        State("prep-county", "value"),
        State("planning-bbox", "value"),
        prevent_initial_call=True,
    )
    @guard(3, 2)
    def start_cooling_prep(_: int, state: str, county: str, bbox: str) -> Any:
        try:
            bounds = parse_bbox(bbox or "")
        except ValueError as exc:
            return no_update, True, views.error_box(str(exc))
        result = api.post(
            "/v1/preparations",
            auth(),
            {
                "state": state,
                "county": county,
                "bbox": list(bounds),
                "include_cooling": True,
            },
        )
        return (
            result["id"],
            False,
            "Downloading regional Census/ACS and LA cooling records; local verification remains required.",
        )

    @app.callback(
        Output("prep-state", "value"),
        Output("prep-county", "value"),
        Input("county", "value"),
        Input("map-data", "data"),
        prevent_initial_call=True,
    )
    def preparation_county(county: str | None, map_data: Any) -> Any:
        if county == "ALL" and map_data:
            counties = map_data.get("counties", [])
            county = counties[0]["county_id"] if len(counties) == 1 else None
        if not county or len(county) != 5 or not county.isdigit():
            return no_update, no_update
        return county[:2], county[2:]

    @app.callback(Output("import-filename", "children"), Input("import-file", "filename"))
    def upload_filename(filename: str | None) -> str:
        return filename or "No file selected"

    @app.callback(
        Output("import-preview", "data"),
        Output("publish-upload", "disabled"),
        Output("import-output", "children"),
        Input("validate-upload", "n_clicks"),
        State("import-file", "contents"),
        State("import-kind", "value"),
        State("area", "value"),
        State("map-data", "data"),
        State("import-area", "value"),
        State("import-name", "value"),
        State("import-source", "value"),
        prevent_initial_call=True,
    )
    def validate_upload(
        _: int,
        contents: str | None,
        kind: str,
        area: str | None,
        map_data: Any,
        new_area: str | None,
        name: str | None,
        source: str | None,
    ) -> Any:
        import base64
        import binascii
        import json

        try:
            if not contents:
                raise ValueError("Choose a source file first")
            encoded = contents.split(",", 1)[1]
            if len(encoded) > 26666668:
                raise ValueError("Upload exceeds 20 MB")
            data = base64.b64decode(encoded, validate=True)
            if kind == "cooling" and not area:
                raise ValueError("Select a published study area for the cooling inventory")
            if kind == "region" and not new_area:
                raise ValueError("Enter a region ID for the ZIP")
            params = {
                "kind": kind,
                "study_area_id": area if kind == "cooling" else new_area,
                "study_area_name": name or "",
                "source": source or "",
            }
            if kind == "cooling" and map_data:
                params["dataset_version_id"] = map_data["dataset"]["id"]
            result = api.request(
                "POST",
                "/v1/imports",
                auth(),
                params=params,
                content=data,
                headers={"Content-Type": "text/csv" if kind == "cooling" else "application/zip"},
                timeout=120,
            )
            return (
                result,
                not result["report"]["passed"],
                html.Pre(json.dumps(result, indent=2), style={"whiteSpace": "pre-wrap"}),
            )
        except (ValueError, IndexError, binascii.Error, ApiError) as exc:
            return None, True, views.error_box(str(exc))

    @app.callback(
        Output("import-preview", "data", allow_duplicate=True),
        Output("publish-upload", "disabled", allow_duplicate=True),
        Input("import-file", "contents"),
        Input("import-kind", "value"),
        Input("area", "value"),
        Input("import-area", "value"),
        Input("import-name", "value"),
        Input("import-source", "value"),
        prevent_initial_call=True,
    )
    def invalidate_upload(*_: Any) -> Any:
        return None, True

    @app.callback(
        Output("import-output", "children", allow_duplicate=True),
        Output("publish-upload", "disabled", allow_duplicate=True),
        Output("area", "options"),
        Output("area", "value"),
        Output("view", "data", allow_duplicate=True),
        Input("publish-upload", "n_clicks"),
        State("import-preview", "data"),
        prevent_initial_call=True,
    )
    @guard(5, 0)
    def publish_upload(_: int, preview: Any) -> Any:
        if not preview or not preview["report"]["passed"]:
            return (
                views.error_box("Validate a complete upload first"),
                True,
                no_update,
                no_update,
                no_update,
            )
        result = api.post(f"/v1/imports/{preview['id']}/publish", auth(), {}, timeout=120)
        areas = api.get("/v1/study-areas", auth())
        options = [{"label": item["name"], "value": item["id"]} for item in areas]
        return (
            f"Published {result['version']}. Map refreshed; check sources for remaining gaps.",
            True,
            options,
            result["study_area_id"],
            {"area": result["study_area_id"], "county": "ALL"},
        )

    @app.callback(
        Output("import-download", "data"),
        Output("import-output", "children", allow_duplicate=True),
        Input("cooling-template", "n_clicks"),
        Input("dataset-sources", "n_clicks"),
        State("map-data", "data"),
        prevent_initial_call=True,
    )
    @guard(2, 1)
    def download_import_data(_: int, _b: int, map_data: Any) -> Any:
        if ctx.triggered_id == "cooling-template":
            path, filename = "/v1/cooling-centers/template", "centers.csv"
        elif map_data:
            path, filename = (
                f"/v1/datasets/{map_data['dataset']['id']}/download",
                "region-sources.zip",
            )
        else:
            return no_update, views.error_box("Select a published study area first")
        data = api.request("GET", path, auth(), binary=True)
        return dcc.send_bytes(data, filename), no_update

    @app.callback(Output("model-status", "children"), Input("model-poll", "n_intervals"))
    @guard(1, 0)
    def model_status(_: int) -> Any:
        status = api.get("/v1/model", auth())
        if status["mode"] == "offline":
            return "Offline test mode — no AI model is running."
        label = f"AI provider: {status['provider']} · Model: {status['model']}"
        if not status["configured"]:
            return (
                label
                + " · Setup required: add the API key on the server, then restart the API and worker."
            )
        return label + " · Credentials configured. Check the connection to confirm access."

    @app.callback(
        Output("model-check-output", "children"),
        Input("check-model", "n_clicks"),
        prevent_initial_call=True,
    )
    @guard(1, 0)
    def check_model(_: int) -> Any:
        result = api.post("/v1/model/check", auth(), {}, timeout=130)
        return (
            f"Connected to {result['provider']} / {result['model']}. Live model request succeeded."
        )

    @app.callback(
        Output("area-conversation", "children"),
        Output("area-chat", "data"),
        Input("ask-area", "n_clicks"),
        Input("map-data", "data"),
        Input("planning-bbox", "value"),
        State("area-question", "value"),
        State("area-chat", "data"),
        prevent_initial_call=True,
        running=[(Output("ask-area", "disabled"), True, False)],
    )
    def ask_area(_: int, data: Any, bbox: str | None, question: str | None, chat: Any) -> Any:
        import json

        from resilience_map.core.geography import parse_bbox

        if ctx.triggered_id != "ask-area":
            return "Selection changed. Ask a question to start a new conversation.", None
        try:
            if not data or not question or not question.strip():
                raise ValueError("Select a published area and enter a question")
            scope = {
                "study_area_id": data["study_area_id"],
                "dataset_version_id": data["dataset"]["id"],
                "county": data.get("county"),
                "bbox": list(parse_bbox(bbox)) if bbox and bbox.strip() else None,
            }
            turns = (chat or {}).get("turns", []) if (chat or {}).get("scope") == scope else []
            result = api.post(
                "/v1/assistant/questions",
                auth(),
                {
                    **scope,
                    "question": question.strip(),
                    "previous_questions": [t["question"] for t in turns[-6:]],
                },
                timeout=140,
            )
            turns = [*turns[-5:], {"question": question.strip(), "result": result}]
            children = []
            for turn in turns:
                result = turn["result"]
                children.append(
                    html.Div(
                        [
                            html.Strong(turn["question"]),
                            html.P(result["answer"], style={"whiteSpace": "pre-wrap"}),
                            html.Small(
                                " · ".join(
                                    [
                                        result["intent"],
                                        result["answer_source"],
                                        "dataset " + result["scope"]["dataset_version_id"],
                                    ]
                                )
                            ),
                            html.Details(
                                [
                                    html.Summary("Evidence and model calls"),
                                    html.Pre(
                                        json.dumps(
                                            {
                                                "evidence": result["evidence"],
                                                "model_calls": result["model_calls"],
                                                "grounding_check": result.get("grounding_check"),
                                                "notice": result.get("notice"),
                                            },
                                            indent=2,
                                        ),
                                        style={
                                            "maxHeight": "300px",
                                            "overflow": "auto",
                                            "whiteSpace": "pre-wrap",
                                        },
                                    ),
                                ]
                            ),
                        ],
                        className="card-b",
                    )
                )
            return children, {"scope": scope, "turns": turns}
        except (ApiError, ValueError) as exc:
            return views.error_box(str(exc)), no_update

    return app


def wsgi() -> Any:
    """Gunicorn entry: ``gunicorn 'resilience_map.frontend.app:wsgi()'``."""
    return create_dash_app().server
