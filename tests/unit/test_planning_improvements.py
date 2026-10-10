from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from resilience_map.backend.main import create_app
from resilience_map.backend.monitoring import NWSMonitor
from resilience_map.core.models import (
    CandidateSite,
    CoolingCenter,
    DatasetBundle,
    Indicators,
    ScenarioConstraints,
    Tract,
)
from resilience_map.gis.routing import RoadRouter, access_matrix
from resilience_map.gis.selection import select_bundle, validate_bbox
from resilience_map.ingestion.inventory import validate_inventory
from resilience_map.ingestion.pipeline import parse_snapshot, snapshot_hash
from resilience_map.optimization.scenario import run_site_selection
from resilience_map.verification.checks import verify_site_selection
from tests.fixtures.synthetic import SyntheticAdapter


@pytest.fixture
def roads():
    return {
        "source": "fixture://osm",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "license": "ODbL",
        "coverage_bbox": [-98, 29, -97, 31],
        "assumptions": "Test weights; directed permitted modes",
        "snap_limit_m": 100,
        "nodes": [
            {"id": "a", "lon": -97.75, "lat": 30.25},
            {"id": "b", "lon": -97.74, "lat": 30.25},
            {"id": "c", "lon": -97.73, "lat": 30.25},
        ],
        "edges": [
            {"u": "a", "v": "b", "key": 0, "minutes": {"walk": 12, "drive": 2}},
            {"u": "a", "v": "b", "key": 1, "minutes": {"walk": 4, "drive": 1}},
            {"u": "b", "v": "c", "key": 0, "minutes": {"walk": 3, "bike": 2, "drive": 1}},
        ],
    }


@pytest.fixture
def network_bundle(roads):
    return DatasetBundle(
        dataset_version_id="fixture",
        version="v1",
        study_area_id="travis",
        scoring_version="scoring-v1",
        roads=roads,
        centers=[],
        tracts=[
            Tract(
                tract_id="t1",
                name="Fixture",
                population=100,
                lon=-97.75,
                lat=30.25,
                indicators=Indicators(
                    surface_temp_anomaly_c=2,
                    pct_age_65_plus=20,
                    median_income_usd=50000,
                    canopy_pct=10,
                ),
            )
        ],
        candidates=[
            CandidateSite(
                site_id="s1",
                name="Fixture destination",
                capacity=90,
                cost_usd=10.99,
                lon=-97.73,
                lat=30.25,
            )
        ],
    )


@pytest.mark.parametrize("access_model", ["straight_line_proxy", "road_network"])
def test_confirmed_closed_center_does_not_suppress_new_site_benefits(network_bundle, access_model):
    tract = network_bundle.tracts[0]
    center = CoolingCenter(
        center_id="closed-center",
        name="Closed fixture center",
        lon=tract.lon,
        lat=tract.lat,
        opening_status="confirmed_closed",
    )
    bundle = network_bundle.model_copy(update={"centers": [center]})
    constraints = ScenarioConstraints(
        num_centers=1, travel_mode="walk", threshold_minutes=60, access_model=access_model
    )
    result = run_site_selection(bundle, constraints, 2, 50)
    assert result["coverage"]["baseline_served"] == 0
    assert result["coverage"]["newly_served"] == 90
    assert result["selected_sites"][0]["site_id"] == "s1"
    assert result["inventory"][0]["opening_status"] == "confirmed_closed"
    report = verify_site_selection(result, bundle)
    assert report.passed, report.errors


@pytest.mark.parametrize("access_model", ["straight_line_proxy", "road_network"])
def test_unknown_opening_status_remains_eligible_with_disclosure(network_bundle, access_model):
    tract = network_bundle.tracts[0]
    center = CoolingCenter(
        center_id="unknown-center", name="Unknown fixture center", lon=tract.lon, lat=tract.lat
    )
    bundle = network_bundle.model_copy(update={"centers": [center]})
    constraints = ScenarioConstraints(
        num_centers=1, travel_mode="walk", threshold_minutes=60, access_model=access_model
    )
    result = run_site_selection(bundle, constraints, 2, 50)
    assert result["coverage"]["baseline_served"] == 100
    assert result["coverage"]["newly_served"] == 0
    assert result["inventory"][0]["opening_status"] == "unknown"
    assert any("not confirmed current opening status" in text for text in result["limitations"])
    assert any("unknown opening status remains eligible" in text for text in result["assumptions"])
    report = verify_site_selection(result, bundle)
    assert report.passed, report.errors


@pytest.mark.parametrize("access_model", ["straight_line_proxy", "road_network"])
def test_verification_routes_requested_subset_of_501_candidates(network_bundle, access_model):
    site = network_bundle.candidates[0]
    bundle = network_bundle.model_copy(
        update={
            "candidates": [site]
            + [site.model_copy(update={"site_id": f"unused-{i}"}) for i in range(500)]
        }
    )
    constraints = ScenarioConstraints(
        num_centers=1,
        travel_mode="walk",
        threshold_minutes=60,
        access_model=access_model,
        candidate_site_ids=[site.site_id],
    )
    assert len(bundle.candidates) == 501
    result = run_site_selection(bundle, constraints, 2, 50)
    assert result["coverage"]["newly_served"] == 90
    assert [s["site_id"] for s in result["selected_sites"]] == [site.site_id]
    report = verify_site_selection(result, bundle)
    assert report.passed, report.errors


@pytest.mark.parametrize("access_model", ["straight_line_proxy", "road_network"])
@pytest.mark.parametrize("ineligible_reason", ["unrequested", "inaccessible", "unknown"])
def test_verification_rejects_ineligible_selected_sites(
    network_bundle, access_model, ineligible_reason
):
    site = network_bundle.candidates[0]
    other = site.model_copy(
        update={"site_id": "other", "accessible": ineligible_reason != "inaccessible"}
    )
    bundle = network_bundle.model_copy(update={"candidates": [site, other]})
    requested = (
        [site.site_id, other.site_id] if ineligible_reason == "inaccessible" else [site.site_id]
    )
    constraints = ScenarioConstraints(
        num_centers=1,
        travel_mode="walk",
        threshold_minutes=60,
        access_model=access_model,
        candidate_site_ids=requested,
    )
    result = run_site_selection(bundle, constraints, 2, 50)
    assert verify_site_selection(result, bundle).passed
    forged = copy.deepcopy(result)
    # Identical location, cost and capacity isolate the selected-site eligibility violation.
    forged["selected_sites"][0]["site_id"] = (
        "missing" if ineligible_reason == "unknown" else "other"
    )
    report = verify_site_selection(forged, bundle)
    assert not report.passed
    expected = {
        "unrequested": "outside requested candidate list",
        "inaccessible": "not marked accessible",
        "unknown": "not a known candidate",
    }[ineligible_reason]
    assert any(expected in error for error in report.errors)


def test_analysis_and_verification_reject_unknown_requested_candidate_ids(network_bundle):
    constraints = ScenarioConstraints(
        num_centers=1,
        travel_mode="walk",
        threshold_minutes=60,
        candidate_site_ids=["s1", "missing"],
    )
    with pytest.raises(ValueError, match="Unknown candidate sites"):
        run_site_selection(network_bundle, constraints, 2, 50)
    result = run_site_selection(
        network_bundle, constraints.model_copy(update={"candidate_site_ids": ["s1"]}), 2, 50
    )
    result["constraints"]["candidate_site_ids"].append("missing")
    report = verify_site_selection(result, network_bundle)
    assert not report.passed
    assert any("Unknown candidate sites" in error for error in report.errors)


def test_directed_parallel_mode_weights(roads):
    walk = RoadRouter(roads, "walk")
    assert len(walk.adj["a"]) == 2
    assert walk.distances("a")["c"] == 7
    assert "a" not in walk.distances("c")
    assert RoadRouter(roads, "drive").distances("a")["c"] == 2
    assert RoadRouter(roads, "bike").distances("a") == {"a": 0}


@pytest.mark.parametrize("weight", [-1, 0, float("nan"), float("inf")])
def test_bad_weights_rejected(roads, weight):
    roads["edges"][0]["minutes"]["walk"] = weight
    with pytest.raises(ValueError, match="weights"):
        RoadRouter(roads, "drive")


def test_unmatched_unreachable_no_fallback(network_bundle):
    bundle = network_bundle
    destination = bundle.candidates[0].model_copy(update={"lon": -96})
    matrix, issues, _ = access_matrix(bundle, "walk", "road_network", [destination])
    assert matrix[("t1", "s1")] == float("inf") and issues[0]["status"] == "unmatched"
    reverse = bundle.model_copy(
        update={"tracts": [bundle.tracts[0].model_copy(update={"lon": -97.73})]}
    )
    dest = bundle.candidates[0].model_copy(update={"lon": -97.75})
    _, issues, _ = access_matrix(reverse, "walk", "road_network", [dest])
    assert issues[0]["status"] == "unreachable"
    with pytest.raises(ValueError, match="no pinned"):
        access_matrix(bundle.model_copy(update={"roads": None}), "walk", "road_network", [])


def test_network_optimization_and_independent_verification(network_bundle):
    constraints = ScenarioConstraints(
        num_centers=1, travel_mode="walk", threshold_minutes=7, access_model="road_network"
    )
    result = run_site_selection(network_bundle, constraints, 2, 50)
    assert result["coverage"]["newly_served"] == 90
    assert result["selected_sites"][0]["tracts_served"][0]["travel_minutes"] == 7
    assert verify_site_selection(result, network_bundle).passed
    forged = copy.deepcopy(result)
    forged["selected_sites"][0]["tracts_served"][0]["travel_minutes"] = 1
    assert not verify_site_selection(forged, network_bundle).passed
    forged = copy.deepcopy(result)
    forged["routing"]["road_sha256"] = "tampered"
    assert not verify_site_selection(forged, network_bundle).passed
    short = constraints.model_copy(update={"threshold_minutes": 6})
    assert run_site_selection(network_bundle, short, 2, 50)["coverage"]["newly_served"] == 0
    cheap = constraints.model_copy(update={"budget_usd": 10.98})
    assert run_site_selection(network_bundle, cheap, 2, 50)["coverage"]["newly_served"] == 0


def test_inventory_unknown_capacity_metadata_and_hash():
    files = SyntheticAdapter().fetch().files
    files["centers.csv"] = (
        b'center_id,name,capacity,lon,lat,hours,last_confirmed,source,provenance,opening_status\nfixture,Fixture,,-97.75,30.25,9-17,2025-01-01,fixture://local,"{""reviewer"":""fixture""}",unknown\n'
    )
    bundle, _ = parse_snapshot(files)
    center = bundle.centers[0]
    assert center.capacity is None and center.hours == "9-17"
    assert center.model_dump(mode="json")["last_confirmed"] == "2025-01-01"
    assert center.provenance == {"reviewer": "fixture"}
    snap = SyntheticAdapter().fetch()
    before = snapshot_hash(snap)
    snap.files["roads.json"] = b"{}"
    assert snapshot_hash(snap) != before
    with pytest.raises(ValueError, match="absence"):
        validate_inventory(bundle.model_copy(update={"inventory_status": "confirmed_absent"}))
    with pytest.raises(ValueError, match="reviewer"):
        validate_inventory(
            bundle.model_copy(update={"centers": [], "inventory_status": "confirmed_absent"})
        )
    travis, _ = parse_snapshot(
        {p.name: p.read_bytes() for p in Path("data/travis").iterdir() if p.name in files}
    )
    assert travis.inventory_status == "unknown" and not travis.centers


@pytest.mark.parametrize(
    "box", [(-97, 30, -98, 31), (-180, 0, 180, 80), (-98, 30, -96, 32), (float("nan"), 30, -97, 31)]
)
def test_geographic_limits(box):
    with pytest.raises(ValueError):
        validate_bbox(box)


def test_selection_retains_surrounding_destinations(network_bundle):
    subset = select_bundle(network_bundle, (-97.751, 30.249, -97.749, 30.251))
    assert subset.roads == network_bundle.roads
    assert subset.candidates == network_bundle.candidates
    c = ScenarioConstraints(
        num_centers=1,
        travel_mode="walk",
        threshold_minutes=7,
        bbox=(-97.751, 30.249, -97.749, 30.251),
        access_model="road_network",
    )
    result = run_site_selection(network_bundle, c, 2, 50)
    assert verify_site_selection(result, network_bundle).passed


def provider(now, fail=False, stale=False):
    def respond(request):
        if fail:
            return httpx.Response(503)
        path = request.url.path
        if path.startswith("/points/"):
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "observationStations": "https://api.weather.gov/stations",
                        "forecast": "https://api.weather.gov/gridpoints/T/1,1/forecast",
                    }
                },
            )
        if path == "/stations":
            return httpx.Response(
                200, json={"features": [{"id": "https://api.weather.gov/stations/TEST"}]}
            )
        if path.endswith("/latest"):
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "timestamp": (now - timedelta(hours=2) if stale else now).isoformat(),
                        "temperature": {
                            "value": 40,
                            "unitCode": "wmoUnit:degC",
                            "qualityControl": "V",
                        },
                    }
                },
            )
        if path.startswith("/alerts"):
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {
                                "event": "Heat Advisory",
                                "sent": now.isoformat(),
                                "effective": now.isoformat(),
                                "expires": (now + timedelta(hours=3)).isoformat(),
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "properties": {
                    "updated": now.isoformat(),
                    "periods": [
                        {
                            "startTime": now.isoformat(),
                            "endTime": (now + timedelta(hours=12)).isoformat(),
                            "temperature": 100,
                            "temperatureUnit": "F",
                        }
                    ],
                }
            },
        )

    return httpx.Client(transport=httpx.MockTransport(respond))


def test_monitoring_fresh_stale_failed_cache_and_separation():
    now = datetime.now(UTC)
    monitor = NWSMonitor(provider(now, stale=True), max_entries=2)
    result = monitor.read(30.25, -97.75)
    assert result["observations"]["state"] == "stale"
    assert result["alerts"]["state"] == result["forecasts"]["state"] == "available"
    assert result["historical_score_affected"] is False
    assert len(monitor.cache) <= 2
    assert result["observations"]["records"][0]["retrieved_at"]
    failed = NWSMonitor(provider(now, fail=True)).read(30.25, -97.75)
    assert all(failed[k]["state"] == "unavailable" for k in ("observations", "alerts", "forecasts"))
    assert failed["alerts"]["records"] == []
    fresh = NWSMonitor(provider(now))
    first = fresh.read(30.25, -97.75)
    assert (
        fresh.read(30.25, -97.75)["observations"]["retrieved_at"]
        == first["observations"]["retrieved_at"]
    )
    with pytest.raises(ValueError, match="Untrusted"):
        fresh.get("https://example.com/stations")


def test_api_monitoring_and_resource_errors(monkeypatch):
    from resilience_map.backend.routes import monitoring

    monkeypatch.setattr(monitoring, "monitor", NWSMonitor(provider(datetime.now(UTC), fail=True)))
    with TestClient(create_app()) as client:
        response = client.get("/v1/monitoring", params={"lat": 30.25, "lon": -97.75})
        assert (
            response.status_code == 200
            and response.json()["observations"]["state"] == "unavailable"
        )
        assert client.get("/v1/monitoring", params={"lat": 90, "lon": -97.75}).status_code == 422
        assert (
            client.get(
                "/v1/monitoring", params={"lat": 30, "lon": -97, "observation_max_age_seconds": 0}
            ).status_code
            == 422
        )
        assert (
            client.post("/v1/preparations", json={"state": "48", "county": "453"}).status_code
            == 409
        )
        assert (
            client.post("/v1/preparations", json={"state": "../", "county": "453"}).status_code
            == 422
        )


def test_osmnx_export_preserves_parallel_directed_edges_and_assumptions():
    from resilience_map.ingestion.roads import normalize_graphs

    class FixtureGraph:
        def nodes(self, data):
            return [(1, {"x": -97.75, "y": 30.25}), (2, {"x": -97.74, "y": 30.25})]

        def edges(self, keys, data):
            return [(1, 2, 0, {"length": 100, "osmid": 1}), (1, 2, 1, {"length": 200, "osmid": 2})]

    snapshot = normalize_graphs(
        {"walk": FixtureGraph()}, (-98, 29, -97, 31), "2026-01-01T00:00:00Z"
    )
    router = RoadRouter(snapshot, "walk")
    assert len(router.adj["1"]) == 2
    assert router.distances("1")["2"] == pytest.approx(1.25)
    assert "1" not in router.distances("2")
    assert snapshot["osm_as_of"] and snapshot["assumptions"]["restrictions"]


def test_road_boundary_buffer_rejected(network_bundle):
    network_bundle.roads["coverage_bbox"] = [-97.76, 30.24, -97.72, 30.26]
    with pytest.raises(ValueError, match="surrounding buffer"):
        run_site_selection(
            network_bundle,
            ScenarioConstraints(
                num_centers=1,
                travel_mode="drive",
                threshold_minutes=20,
                access_model="road_network",
            ),
            2,
            50,
        )


def test_feed_missing_issue_time_and_expired_validity():
    now = datetime.now(UTC)
    monitor = NWSMonitor(provider(now))
    missing = monitor.record(
        "observation", {"temperature": {"value": None}}, "fixture", now.isoformat(), 3600, now
    )
    assert (
        "missing_temperature" in missing["quality_flags"]
        and "missing_issue_time" in missing["quality_flags"]
    )
    expired = monitor.record(
        "alert",
        {
            "sent": now.isoformat(),
            "effective": now.isoformat(),
            "expires": (now - timedelta(minutes=1)).isoformat(),
        },
        "fixture",
        now.isoformat(),
        3600,
        now,
    )
    assert "expired" in expired["quality_flags"]


def test_async_preparation_owner_status_and_storage_limit(tmp_path, monkeypatch):
    from resilience_map.backend import preparation
    from resilience_map.ingestion.sources import REQUIRED_FILES

    monkeypatch.setattr(preparation, "ROOT", tmp_path)
    monkeypatch.setenv("HRM_ENABLE_PREPARATION", "true")

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    class FixtureProcess:
        def __init__(self, args, **kwargs):
            directory = Path(args[-1])
            directory.mkdir()
            for name in REQUIRED_FILES:
                (directory / name).write_text("fixture offline acquisition")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(preparation.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(preparation.subprocess, "Popen", FixtureProcess)
    ident = preparation.prepare("48", "453", "owner")
    status = preparation.get_status(ident, "owner")
    assert status["state"] == "downloaded_unpublished" and status["missing_inputs"]
    assert "owner" not in status
    with pytest.raises(Exception) as denied:
        preparation.get_status(ident, "other")
    assert denied.value.status_code == 404
    for i in range(15):
        d = tmp_path / str(i)
        d.mkdir()
        (d / "status.json").write_text("{}")
    with pytest.raises(Exception) as full:
        preparation.prepare("48", "453", "owner")
    assert full.value.status_code == 429
