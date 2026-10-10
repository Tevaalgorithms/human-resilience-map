from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from resilience_map.backend.exports import to_csv, to_geojson, to_summary
from resilience_map.core.errors import ValidationFailed
from resilience_map.core.object_store import LocalObjectStore
from resilience_map.ingestion.pipeline import snapshot_hash
from tests.fixtures.synthetic import SyntheticAdapter


def test_snapshot_hash_is_deterministic():
    assert snapshot_hash(SyntheticAdapter().fetch()) == snapshot_hash(SyntheticAdapter().fetch())


def test_object_store_blocks_path_traversal(tmp_path):
    store = LocalObjectStore(str(tmp_path))
    store.put("a/b.txt", b"x")
    assert store.get("a/b.txt") == b"x"
    with pytest.raises(ValueError):
        store.put("../evil", b"x")


def _result(name="School Gym"):
    return {
        "outcome": "completed",
        "explanation_source": "llm",
        "analysis": {
            "intent": "site_selection",
            "dataset_version": "v1",
            "scoring_version": "scoring-v1",
            "selected_sites": [
                {
                    "site_id": "SITE-1",
                    "name": name,
                    "lon": 1.0,
                    "lat": 2.0,
                    "capacity": 10,
                    "cost_usd": 5.0,
                    "population_assigned": 7,
                    "tracts_served": [],
                }
            ],
            "coverage": {
                "baseline_served": 0,
                "total_population": 10,
                "baseline_pct": 0,
                "scenario_served": 7,
                "scenario_pct": 70,
                "newly_served": 7,
            },
            "constraints": {"num_centers": 1, "travel_mode": "walk", "threshold_minutes": 10},
            "access_radius_km": 1.0,
            "solver": {"status": "OPTIMAL"},
            "assumptions": ["a"],
            "limitations": ["l"],
        },
    }


def test_csv_neutralizes_formula_injection():
    assert "'=HYPERLINK" in to_csv(_result('=HYPERLINK("http://x")'))


def test_exports_require_completed_result():
    with pytest.raises(ValidationFailed):
        to_csv({"outcome": "incomplete", "analysis": None})
    assert '"Point"' in to_geojson(_result()) and "Assumptions" in to_summary(_result())


def test_cognito_mode_rejects_missing_or_bad_token(monkeypatch):
    from resilience_map.backend.main import create_app
    from resilience_map.core.config import get_settings

    monkeypatch.setenv("HRM_AUTH_MODE", "cognito")
    monkeypatch.setenv("HRM_COGNITO_USER_POOL_ID", "us-east-1_x")
    monkeypatch.setenv("HRM_COGNITO_CLIENT_ID", "c")
    get_settings.cache_clear()
    try:
        client = TestClient(create_app())
        assert client.get("/v1/study-areas").status_code == 401
        assert (
            client.get("/v1/study-areas", headers={"Authorization": "Bearer garbage"}).status_code
            == 401
        )
        assert client.get("/health").status_code == 200
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_prod_requires_cognito_and_secret():
    from resilience_map.core.config import Settings

    with pytest.raises(ValueError, match=r"(?i)cognito"):
        Settings(_env_file=None, environment="prod")


def test_http_client_url_logging_is_silenced_to_protect_secrets():
    import logging

    from resilience_map.core.logging import configure_logging

    configure_logging("INFO")
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
