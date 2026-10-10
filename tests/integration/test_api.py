from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration
BODY = {
    "question": "Where should we open two cooling centers? walking 15 minutes",
    "study_area_id": "synthetic-metro",
}


def post(client, body=None, user="alice", **headers):
    return client.post("/v1/analyses", json=body or BODY, headers={"X-Dev-User": user, **headers})


def test_health_and_readiness(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["status"] == "ready"


def test_baseline_endpoints(client):
    h = {"X-Dev-User": "alice"}
    areas = client.get("/v1/study-areas", headers=h).json()
    assert areas[0]["is_synthetic"] is True
    tr = client.get("/v1/tracts", params={"study_area_id": "synthetic-metro"}, headers=h).json()
    assert len(tr["features"]) == 36 and tr["dataset"]["scoring_version"] == "scoring-v1"
    tid = tr["features"][0]["properties"]["tract_id"]
    d = client.get(
        f"/v1/tracts/{tid}", params={"study_area_id": "synthetic-metro"}, headers=h
    ).json()
    assert d["components"] and d["source_dates"]
    assert (
        client.get(
            "/v1/tracts/NOPE-1", params={"study_area_id": "synthetic-metro"}, headers=h
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/v1/tracts", params={"study_area_id": "synthetic-metro", "bbox": "bad"}, headers=h
        ).status_code
        == 422
    )
    assert (
        len(
            client.get(
                "/v1/cooling-centers", params={"study_area_id": "synthetic-metro"}, headers=h
            ).json()
        )
        == 3
    )


def test_create_returns_202_with_location_and_pins_versions(client):
    r = post(client)
    assert r.status_code == 202 and r.headers["location"].endswith(r.json()["job_id"])
    st = client.get(r.json()["status_url"], headers={"X-Dev-User": "alice"}).json()
    assert (
        st["status"] == "queued"
        and st["dataset_version_id"]
        and st["scoring_version"] == "scoring-v1"
    )


def test_idempotency_key_returns_same_job(client):
    a = post(client, **{"Idempotency-Key": "abc"}).json()["job_id"]
    b = post(client, **{"Idempotency-Key": "abc"}).json()["job_id"]
    assert a == b


def test_other_users_cannot_read_or_export_or_resume(client):
    jid = post(client).json()["job_id"]
    for method, path in (
        ("get", f"/v1/analyses/{jid}"),
        ("get", f"/v1/analyses/{jid}/exports?format=csv"),
    ):
        assert getattr(client, method)(path, headers={"X-Dev-User": "mallory"}).status_code == 404
    r = client.post(
        f"/v1/analyses/{jid}/clarifications",
        json={"travel_mode": "walk"},
        headers={"X-Dev-User": "mallory"},
    )
    assert r.status_code == 404


def test_validation_errors(client):
    assert post(client, {**BODY, "constraints": {"num_centers": 9}}).status_code == 422
    assert post(client, {**BODY, "study_area_id": "Bad Id!"}).status_code == 422
    assert post(client, {**BODY, "extra": 1}).status_code == 422
    assert client.get("/v1/analyses/not-a-uuid", headers={"X-Dev-User": "alice"}).status_code == 422
    assert post(client, {**BODY, "study_area_id": "nowhere"}).status_code == 404


def test_active_job_cap_returns_409(client):
    for _ in range(3):
        assert post(client).status_code == 202
    assert post(client).status_code == 409


def test_clarification_on_queued_job_conflicts(client):
    jid = post(client).json()["job_id"]
    r = client.post(
        f"/v1/analyses/{jid}/clarifications",
        json={"travel_mode": "walk"},
        headers={"X-Dev-User": "alice"},
    )
    assert r.status_code == 409


def test_map_payload_is_bounded(client, monkeypatch):
    from resilience_map.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_map_features", 5)
    r = client.get(
        "/v1/tracts", params={"study_area_id": "synthetic-metro"}, headers={"X-Dev-User": "a"}
    ).json()
    assert len(r["features"]) == 5 and r["truncated"] is True


def test_full_flow_exports_and_review(client):
    from resilience_map.agents.graph import Deps
    from resilience_map.agents.llm import StubLLM
    from resilience_map.core.config import get_settings
    from resilience_map.core.queue import LocalQueue
    from resilience_map.repository.datasets import PostgresDatasetLoader
    from resilience_map.workers.checkpoint import postgres_checkpointer
    from resilience_map.workers.consumer import handle_message
    from resilience_map.workers.dispatcher import dispatch_once

    h = {"X-Dev-User": "alice"}
    jid = post(
        client,
        {
            **BODY,
            "question": "Where should we open two cooling centers? driving 12 minutes",
            "comparison": {
                "alternatives": [
                    {
                        "scenario_id": "walk-option",
                        "constraints": {"travel_mode": "walk"},
                    }
                ]
            },
            "uncertainty": {
                "variants": [
                    {
                        "variant_id": "lower-capacity",
                        "candidate_capacity_multiplier": 0.8,
                    }
                ]
            },
        },
    ).json()["job_id"]
    queue = LocalQueue(60, 3)
    dispatch_once(queue)
    with postgres_checkpointer() as saver:
        for m in queue.receive(1, 1):
            handle_message(
                m, queue, Deps(StubLLM(), PostgresDatasetLoader(), get_settings()), saver, "w"
            )
    st = client.get(f"/v1/analyses/{jid}", headers=h).json()
    assert st["status"] == "completed" and st["result"]["analysis"]["selected_sites"]
    assert st["result"]["readiness"]["status"] in {"ready", "exploratory"}
    assert st["result"]["comparison"]["scenarios"][1]["verification"]["passed"]
    assert st["result"]["uncertainty"]["runs"][1]["verification"]["passed"]
    csv = client.get(f"/v1/analyses/{jid}/exports?format=csv", headers=h)
    assert (
        csv.status_code == 200
        and "site_id" in csv.text
        and csv.headers["content-type"].startswith("text/csv")
    )
    assert '"Point"' in client.get(f"/v1/analyses/{jid}/exports?format=geojson", headers=h).text
    site = st["result"]["analysis"]["selected_sites"][0]["site_id"]
    assert (
        client.post(
            f"/v1/analyses/{jid}/reviews",
            headers=h,
            json={"site_id": site, "decision": "approved", "notes": "ok"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/v1/analyses/{jid}/reviews",
            headers=h,
            json={"site_id": "SITE-ZZ", "decision": "approved"},
        ).status_code
        == 404
    )
    assert (
        client.get(f"/v1/analyses/{jid}", headers=h).json()["reviews"][0]["decision"] == "approved"
    )


def test_tracts_carry_indicators_for_map_layers(client):
    h = {"X-Dev-User": "alice"}
    f = client.get("/v1/tracts", params={"study_area_id": "synthetic-metro"}, headers=h).json()[
        "features"
    ][0]["properties"]
    assert set(f["indicators"]) == {"heat", "age65", "income", "canopy", "transit"}
    assert f["indicators"]["canopy"] is not None and f["score"] is not None


def test_county_rollup_and_filter(client):
    h = {"X-Dev-User": "alice"}
    counties = client.get(
        "/v1/counties", params={"study_area_id": "synthetic-metro"}, headers=h
    ).json()
    assert (
        counties and counties[0]["tracts"] == 36 and counties[0]["name"]
    )  # falls back to "County <fips>"
    cid = counties[0]["county_id"]
    hit = client.get(
        "/v1/tracts", params={"study_area_id": "synthetic-metro", "county": cid}, headers=h
    ).json()
    miss = client.get(
        "/v1/tracts", params={"study_area_id": "synthetic-metro", "county": "00000"}, headers=h
    ).json()
    assert len(hit["features"]) == 36 and miss["features"] == []
    assert (
        client.get(
            "/v1/tracts",
            params={"study_area_id": "synthetic-metro", "county": "12'; DROP"},
            headers=h,
        ).status_code
        == 422
    )


def test_methodology_is_public_to_signed_in_users_and_complete(client):
    m = client.get("/v1/methodology", headers={"X-Dev-User": "alice"}).json()
    assert (
        m["scoring_version"] == "scoring-v1"
        and abs(sum(i["weight"] for i in m["indicators"]) - 1) < 1e-9
    )
    assert m["assumptions"] and m["limitations"] and m["bands"]


def test_history_lists_only_my_analyses(client):
    post(client, user="alice")
    post(client, user="bob")
    mine = client.get("/v1/analyses", headers={"X-Dev-User": "alice"}).json()
    assert len(mine) == 1 and mine[0]["status"] == "queued"
    assert client.get("/v1/analyses", headers={"X-Dev-User": "nobody"}).json() == []


def test_selected_area_source_readiness_and_inventory_metadata(client):
    params = {"study_area_id": "synthetic-metro", "bbox": "-97.8,30.2,-97.74,30.26"}
    response = client.get("/v1/readiness", params=params)
    assert response.status_code == 200
    readiness = response.json()
    assert 0 < readiness["downloaded_tracts"] < 36
    assert readiness["inventory_status"] == "unknown"
    assert readiness["roads_available"] is False
    assert "buffered road snapshot" in readiness["missing_inputs"]
    assert all(
        s["coverage_state"] == "discoverable_only" for s in readiness["discoverable_sources"]
    )
    assert all(s["coverage_state"] == "downloaded_snapshot" for s in readiness["sources"])
    centers = client.get("/v1/cooling-centers", params={"study_area_id": "synthetic-metro"}).json()
    assert centers[0]["hours"] == "9-17 weekdays" and centers[0]["last_confirmed"] == "2026-06-15"
    assert centers[0]["opening_status"] == "unknown"
    assert client.get("/v1/tracts", params={**params, "bbox": "-97,30,-98,31"}).status_code == 422
    invalid = {**BODY, "constraints": {"bbox": [-180, 0, 180, 80]}}
    assert post(client, invalid).status_code == 422
