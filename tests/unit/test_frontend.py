"""Frontend contract tests: the layout must construct and every callback must run against the API
shapes the backend returns. (Catches things like unsupported component props that type checkers miss.)"""

from __future__ import annotations

import json
from typing import Any

import pytest

from resilience_map.frontend import views

POLY = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}


def feature(tid, name, pop, score, county="48453"):
    return {"type": "Feature", "id": tid, "geometry": POLY, "properties": {
        "tract_id": tid, "name": name, "county_id": county, "population": pop, "score": score,
        "nearest_center_km": None,
        "indicators": {"heat": 2.0, "age65": 12.0, "income": 60000.0, "canopy": 20.0, "transit": None}}}  # fmt: skip


FEATURES = [
    feature("48453000101", "Census Tract 1.01; Travis County; Texas", 4000, 35.0),
    feature("48453000102", "Census Tract 1.02", 3000, 55.0),
    feature("48453000103", "Census Tract 1.03", 2000, None),
]


class FakeApi:
    def __init__(self, areas=None):
        self.areas = (
            areas
            if areas is not None
            else [{"id": "a", "name": "Area A", "description": "", "is_synthetic": False}]
        )

    def get(self, path: str, auth: dict[str, str], **params: Any) -> Any:
        if path == "/v1/study-areas":
            return self.areas
        if path == "/v1/counties":
            return [
                {
                    "county_id": "48453",
                    "name": "Travis County",
                    "tracts": 3,
                    "population": 9000,
                    "mean_score": 44.0,
                }
            ]
        if path == "/v1/tracts":
            return {
                "features": FEATURES,
                "truncated": False,
                "counties": [
                    {"county_id": "48453", "name": "Travis County", "tracts": 3, "population": 9000}
                ],
                "dataset": {
                    "id": "dv",
                    "version": "v-abcdef123456",
                    "scoring_version": "scoring-v1",
                    "published_at": "2026-10-08T00:00:00+00:00",
                    "sources": [{"name": "ACS", "license": "PD"}],
                },
            }
        if path.startswith("/v1/tracts/"):
            return {
                "tract_id": "48453000101",
                "name": "Census Tract 1.01",
                "population": 4000,
                "score": 35.0,
                "components": [
                    {
                        "key": "canopy_pct",
                        "label": "Tree canopy",
                        "raw_value": 20.0,
                        "unit": "%",
                        "contribution": 5.0,
                        "weight": 0.2,
                    }
                ],
                "missing_indicators": ["transit_access_index"],
                "nearest_center_km": None,
                "source_dates": {"x": "2023"},
                "median_income_moe_usd": 5000.0,
                "notices": ["Missing indicators: transit_access_index"],
                "dataset_version": "v1",
                "scoring_version": "scoring-v1",
            }
        if path in ("/v1/cooling-centers", "/v1/candidate-sites"):
            return []
        if path == "/v1/methodology":
            return {
                "scoring_version": "scoring-v1",
                "min_indicators": 4,
                "assumptions": ["a"],
                "limitations": ["l"],
                "indicators": [
                    {
                        "key": "k",
                        "label": "Canopy",
                        "weight": 0.2,
                        "unit": "%",
                        "worst": 0.0,
                        "best": 60.0,
                    }
                ],
                "bands": [],
            }
        if path == "/v1/analyses":
            return [
                {
                    "id": "j1",
                    "status": "completed",
                    "question": "q",
                    "study_area_id": "a",
                    "created_at": "2026-10-08",
                }
            ]
        raise AssertionError(f"unexpected API call {path}")


@pytest.fixture
def app(monkeypatch):
    from resilience_map.frontend import app as app_mod

    monkeypatch.setattr(app_mod, "ApiClient", lambda *a, **k: FakeApi())
    return app_mod.create_dash_app()


def update(client, outputs, inputs, state=(), callback_key=None):
    ids = [f"{o[0]}.{o[1]}" for o in outputs]
    body = {
        "output": callback_key or (ids[0] if len(ids) == 1 else ".." + "...".join(ids) + ".."),
        "outputs": [{"id": o[0], "property": o[1]} for o in outputs] if len(ids) > 1 else {"id": outputs[0][0], "property": outputs[0][1]},
        "inputs": [{"id": i[0], "property": i[1], "value": i[2]} for i in inputs],
        "state": [{"id": i[0], "property": i[1], "value": i[2]} for i in state],
        "changedPropIds": [f"{inputs[0][0]}.{inputs[0][1]}"],
    }  # fmt: skip
    r = client.post("/_dash-update-component", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    return r.get_json()["response"]


def test_layout_builds_with_and_without_study_areas(app, monkeypatch):
    from resilience_map.frontend import app as app_mod

    html = json.dumps(app.layout().to_plotly_json(), default=str)
    assert "Human Resilience Map" in html and "Area A" in html
    monkeypatch.setattr(app_mod, "ApiClient", lambda *a, **k: FakeApi(areas=[]))
    assert app_mod.create_dash_app().layout() is not None  # empty deployment must not crash


def test_main_data_callbacks_end_to_end(app):
    c = app.server.test_client()
    r = update(
        c,
        [
            ("county", "options"),
            ("county", "value"),
            ("county-field", "className"),
            ("view", "data"),
        ],
        [("area", "value", "a")],
    )
    assert (
        r["view"]["data"] == {"area": "a", "county": "ALL"}
        and r["county-field"]["className"] == "field hidden"
    )
    r = update(
        c,
        [
            ("map-data", "data"),
            ("page-head", "children"),
            ("synthetic-banner", "children"),
            ("map-notice", "children"),
        ],
        [("view", "data", {"area": "a", "county": "ALL"})],
    )
    data = r["map-data"]["data"]
    assert len(data["tracts"]["features"]) == 3 and "Real data" in json.dumps(r["page-head"])
    fig = update(
        c,
        [("map", "figure"), ("legend", "children")],
        [
            ("map-data", "data", data),
            ("layer", "data", "canopy"),
            ("theme", "data", "dark"),
            ("overlay", "data", []),
            ("selected-tract", "data", None),
        ],
    )
    assert fig["map"]["figure"]["layout"]["map"][
        "style"
    ] == "carto-darkmatter" and "Tree canopy" in json.dumps(fig["legend"])
    assert (
        len(
            update(
                c, [("table", "data")], [("map-data", "data", data), ("search", "value", "1.01")]
            )["table"]["data"]
        )
        == 1
    )
    assert "Census tracts" in json.dumps(
        update(c, [("kpis", "children")], [("map-data", "data", data)])
    )
    card = update(
        c,
        [("tract-card", "children")],
        [("selected-tract", "data", "48453000101")],
        [("map-data", "data", data)],
    )
    assert "Higher vulnerability" in json.dumps(card)
    assert "Canopy" in json.dumps(
        update(c, [("methods", "children")], [("map-data", "data", data)])
    )
    ins = update(
        c, [("insights", "children")], [("map-data", "data", data), ("theme", "data", "dark")]
    )
    assert "Not available here" in json.dumps(ins) or "View as table" in json.dumps(
        ins
    )  # tiny fixture is sparse
    left = update(
        c,
        [
            ("lv-map", "className"),
            ("lv-insights", "className"),
            ("c-map", "className"),
            ("c-insights", "className"),
        ],
        [("left-view", "data", "insights")],
    )
    assert left["c-map"]["className"] == "card hidden" and left["c-insights"]["className"] == ""


def test_views_handle_missing_values_without_crashing():
    data = {"tracts": {"features": FEATURES}, "centers": [], "candidates": [], "counties": [],
            "dataset": {"version": "v", "published_at": "2026-10-08", "scoring_version": "s"}}  # fmt: skip
    for key in views.LAYERS:
        assert views.build_map(data, key, "light", [], None) is not None
        assert views.legend(data, key, False) is not None
    assert views.build_map(None, "score", "light", [], None) is not None
    assert views.kpis(None, "") and views.kpis({**data, "tracts": {"features": []}}, "x")
    assert views.table_rows(data, "zzz", {}) == []


def test_new_area_monitoring_and_routing_callbacks(monkeypatch):
    from resilience_map.frontend import app as app_mod

    calls = []

    class PlanningApi(FakeApi):
        def get(self, path, auth, **params):
            calls.append((path, params))
            if path == "/v1/readiness":
                return {
                    "inventory_status": "unknown",
                    "missing_inputs": ["buffered roads"],
                    "sources": [{"license": "ODbL", "coverage_state": "discoverable_only"}],
                }
            if path == "/v1/monitoring":
                return {
                    "observations": {"state": "stale"},
                    "alerts": {"state": "unavailable"},
                    "historical_score_affected": False,
                }
            return super().get(path, auth, **params)

        def post(self, path, auth, payload, **kwargs):
            calls.append((path, payload))
            return {"job_id": "fixture-job"}

    monkeypatch.setattr(app_mod, "ApiClient", lambda: PlanningApi())
    app = app_mod.create_dash_app()
    client = app.server.test_client()
    layout = json.dumps(app.layout().to_plotly_json(), default=str)
    assert "planning-bbox" in layout and "c-access" in layout
    bbox = "-97.8,30.2,-97.7,30.3"
    ready = update(
        client,
        [("readiness-output", "children")],
        [("check-readiness", "n_clicks", 1)],
        [("area", "value", "a"), ("planning-bbox", "value", bbox)],
    )
    assert "unknown" in json.dumps(ready) and "discoverable_only" in json.dumps(ready)
    weather = update(
        client,
        [("monitoring-output", "children")],
        [("refresh-monitoring", "n_clicks", 1)],
        [("planning-bbox", "value", bbox)],
    )
    assert "stale" in json.dumps(weather) and "unavailable" in json.dumps(weather)
    update(
        client,
        [
            ("job-store", "data"),
            ("poll", "disabled"),
            ("submit-msg", "children"),
            ("panel", "data"),
        ],
        [("submit", "n_clicks", 1)],
        [
            ("question", "value", "Open one cooling center"),
            ("area", "value", "a"),
            ("c-num", "value", 1),
            ("c-mode", "value", "walk"),
            ("c-min", "value", 20),
            ("c-budget", "value", None),
            ("c-access", "value", "road_network"),
            ("planning-bbox", "value", bbox),
            ("compare-enabled", "value", ["enabled"]),
            ("cmp-id", "value", "bike-option"),
            ("cmp-num", "value", 1),
            ("cmp-mode", "value", "bike"),
            ("cmp-min", "value", 15),
            ("cmp-budget", "value", 100000),
            ("cmp-candidates", "value", "SITE-1, SITE-2"),
            ("sensitivity-enabled", "value", ["enabled"]),
            ("sensitivity-parameter", "value", "routing_speed_multiplier"),
            ("sensitivity-values", "value", "0.8,1.2"),
        ],
        callback_key=next(
            key
            for key, item in app.callback_map.items()
            if item["inputs"] == [{"id": "submit", "property": "n_clicks"}]
        ),
    )
    payload = calls[-1][1]
    assert payload["constraints"]["access_model"] == "road_network"
    assert payload["constraints"]["bbox"] == (-97.8, 30.2, -97.7, 30.3)
    assert payload["comparison"]["alternatives"][0]["scenario_id"] == "bike-option"
    assert payload["comparison"]["alternatives"][0]["constraints"]["candidate_site_ids"] == [
        "SITE-1",
        "SITE-2",
    ]
    assert len(payload["uncertainty"]["variants"]) == 2


def test_region_preparation_callback_passes_bbox_and_downloads_binary(monkeypatch):
    import base64

    from resilience_map.frontend import app as app_mod

    calls = []
    ident = "a" * 32

    class RegionApi(FakeApi):
        def post(self, path, auth, payload, **kwargs):
            calls.append((path, payload))
            return {"id": ident}

        def request(self, method, path, auth, **kwargs):
            assert kwargs["binary"] is True
            assert path == f"/v1/preparations/{ident}/download"
            return b"fixture zip bytes"

    monkeypatch.setattr(app_mod, "ApiClient", lambda: RegionApi(areas=[]))
    app = app_mod.create_dash_app()
    client = app.server.test_client()
    assert "Download actual data" in json.dumps(app.layout().to_plotly_json(), default=str)
    output = update(
        client,
        [("prep-job", "data"), ("prep-poll", "disabled"), ("prep-output", "children")],
        [("start-prep", "n_clicks", 1)],
        [
            ("prep-state", "value", "48"),
            ("prep-county", "value", "453"),
            ("planning-bbox", "value", "-97.8,30.2,-97.7,30.3"),
        ],
    )
    assert output["prep-job"]["data"] == ident
    assert calls[-1][1]["bbox"] == [-97.8, 30.2, -97.7, 30.3]
    download = update(
        client,
        [("prep-download", "data")],
        [("save-prep", "n_clicks", 1)],
        [("prep-job", "data", ident)],
    )
    assert base64.b64decode(download["prep-download"]["data"]["content"]) == b"fixture zip bytes"


def test_upload_preview_publish_and_error_states(monkeypatch):
    import base64

    from resilience_map.frontend import app as app_mod
    from resilience_map.frontend.api_client import ApiError

    calls = []

    class ImportApi(FakeApi):
        def request(self, method, path, auth, **kwargs):
            calls.append((path, kwargs))
            if kwargs["content"] == b"bad":
                raise ApiError(422, "Invalid coordinates")
            return {"id": "a" * 32, "report": {"passed": True}, "centers": 1}

        def post(self, path, auth, payload, **kwargs):
            calls.append((path, payload))
            return {"version": "v-new", "study_area_id": "a"}

    monkeypatch.setattr(app_mod, "ApiClient", lambda: ImportApi())
    app = app_mod.create_dash_app()
    client = app.server.test_client()
    states = [
        (
            "import-file",
            "contents",
            "data:text/csv;base64," + base64.b64encode(b"csv bytes").decode(),
        ),
        ("import-kind", "value", "cooling"),
        ("area", "value", "a"),
        ("map-data", "data", {"dataset": {"id": "baseline"}}),
        ("import-area", "value", None),
        ("import-name", "value", None),
        ("import-source", "value", "County inventory"),
    ]
    preview = update(
        client,
        [("import-preview", "data"), ("publish-upload", "disabled"), ("import-output", "children")],
        [("validate-upload", "n_clicks", 1)],
        states,
    )
    assert preview["publish-upload"]["disabled"] is False
    assert calls[-1][1]["content"] == b"csv bytes"
    assert calls[-1][1]["params"]["dataset_version_id"] == "baseline"
    result = update(
        client,
        [
            ("import-output", "children"),
            ("publish-upload", "disabled"),
            ("area", "options"),
            ("area", "value"),
            ("view", "data"),
        ],
        [("publish-upload", "n_clicks", 1)],
        [("import-preview", "data", preview["import-preview"]["data"])],
        callback_key=next(
            key
            for key, item in app.callback_map.items()
            if item["inputs"] == [{"id": "publish-upload", "property": "n_clicks"}]
        ),
    )
    assert result["view"]["data"] == {"area": "a", "county": "ALL"}
    assert "v-new" in result["import-output"]["children"]
    states[0] = (
        "import-file",
        "contents",
        "data:text/csv;base64," + base64.b64encode(b"bad").decode(),
    )
    failed = update(
        client,
        [("import-preview", "data"), ("publish-upload", "disabled"), ("import-output", "children")],
        [("validate-upload", "n_clicks", 2)],
        states,
    )
    assert failed["publish-upload"]["disabled"] is True
    assert failed["import-preview"]["data"] is None
    assert "Invalid coordinates" in json.dumps(failed)


def test_live_model_status_and_connection_callback(monkeypatch):
    from resilience_map.frontend import app as app_mod

    class ModelApi(FakeApi):
        def get(self, path, auth, **params):
            if path == "/v1/model":
                return {
                    "provider": "openai",
                    "model": "gpt-5.4-mini",
                    "mode": "needs_configuration",
                    "configured": False,
                }
            return super().get(path, auth, **params)

        def post(self, path, auth, payload, **kwargs):
            assert path == "/v1/model/check"
            return {"provider": "openai", "model": "gpt-5.4-mini", "status": "connected"}

    monkeypatch.setattr(app_mod, "ApiClient", lambda: ModelApi())
    app = app_mod.create_dash_app()
    client = app.server.test_client()
    result = update(client, [("model-status", "children")], [("model-poll", "n_intervals", 0)])
    assert "gpt-5.4-mini" in json.dumps(result) and "Setup required" in json.dumps(result)
    result = update(client, [("model-check-output", "children")], [("check-model", "n_clicks", 1)])
    assert "Live model request succeeded" in json.dumps(result)


def test_area_chat_scope_followup_and_reset(monkeypatch):
    from resilience_map.frontend import app as app_mod

    calls = []

    class AreaApi(FakeApi):
        def post(self, path, auth, payload, **kwargs):
            assert path == "/v1/assistant/questions"
            calls.append(payload)
            return {
                "answer": "Inventory needs verification.",
                "intent": "data_quality",
                "answer_source": "llm",
                "scope": payload,
                "evidence": {"status": "exploratory"},
                "model_calls": [],
            }

    monkeypatch.setattr(app_mod, "ApiClient", lambda: AreaApi())
    app = app_mod.create_dash_app()
    client = app.server.test_client()
    data = {"study_area_id": "a", "county": "48453", "dataset": {"id": "version"}}
    outputs = [("area-conversation", "children"), ("area-chat", "data")]
    inputs = [
        ("ask-area", "n_clicks", 1),
        ("map-data", "data", data),
        ("planning-bbox", "value", None),
    ]
    states = [("area-question", "value", "What is missing?"), ("area-chat", "data", None)]
    result = update(client, outputs, inputs, states)
    assert calls[-1]["county"] == "48453" and calls[-1]["dataset_version_id"] == "version"
    assert "Inventory needs verification" in json.dumps(result)
    states[1] = ("area-chat", "data", result["area-chat"]["data"])
    result = update(client, outputs, inputs, states)
    assert calls[-1]["previous_questions"] == ["What is missing?"]
    # Dash update helper uses the first input as the triggering property.
    result = update(client, outputs, [inputs[1], inputs[0], inputs[2]], states)
    assert result["area-chat"]["data"] is None
