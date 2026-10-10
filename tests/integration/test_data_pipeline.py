from __future__ import annotations

import dataclasses
import json

import pytest
from sqlalchemy import text

from resilience_map.core.db import session_scope
from resilience_map.core.object_store import LocalObjectStore
from resilience_map.db.migrate import run_migrations
from resilience_map.ingestion.pipeline import run_ingestion
from resilience_map.repository import datasets as repo
from tests.fixtures.synthetic import SyntheticAdapter

pytestmark = pytest.mark.integration


def test_migrations_are_idempotent(db):
    assert run_migrations() == []


def test_postgis_geometry_query(published):
    with session_scope() as s:
        n = s.execute(
            text("SELECT count(*) FROM tracts t WHERE ST_Contains(t.geom, t.centroid)")
        ).scalar_one()
    assert n == 36


def test_ingest_publishes_then_is_idempotent(published, tmp_path):
    assert published.status == "published"
    again = run_ingestion(SyntheticAdapter(), LocalObjectStore(str(tmp_path)))
    assert again.status == "unchanged" and again.dataset_version_id == published.dataset_version_id
    with session_scope() as s:
        assert repo.latest_published(s, "synthetic-metro")["id"] == published.dataset_version_id
        bundle = repo.load_bundle(s, published.dataset_version_id)
        detail = repo.tract_detail(
            s, published.dataset_version_id, bundle.tracts[0].tract_id, "scoring-v1"
        )
    assert len(bundle.tracts) == 36 and len(bundle.centers) == 3 and len(bundle.candidates) == 8
    assert detail["score"] is not None and detail["components"]


def test_raw_snapshot_is_stored_immutably(published, tmp_path):
    store = LocalObjectStore(str(tmp_path))
    snap = SyntheticAdapter().fetch()
    from resilience_map.ingestion.pipeline import snapshot_hash

    key = f"raw/synthetic-metro/{snapshot_hash(snap)}/tracts.geojson"
    assert store.exists(key) and store.get(key) == snap.files["tracts.geojson"]


class BadAdapter:
    """Same study area, but with an invalid unit (age 140%) -> validation must reject it."""

    def fetch(self):
        snap = SyntheticAdapter().fetch()
        files = dict(snap.files)
        files["indicators.csv"] = (
            files["indicators.csv"].replace(b",", b",", 1).replace(b"\n", b"\n", 1)
        )
        rows = files["indicators.csv"].decode().splitlines()
        cols = rows[0].split(",")
        first = rows[1].split(",")
        first[cols.index("pct_age_65_plus")] = "140"
        rows[1] = ",".join(first)
        files["indicators.csv"] = ("\n".join(rows) + "\n").encode()
        return dataclasses.replace(snap, files=files)


def test_failed_refresh_keeps_last_published_version(published, tmp_path):
    res = run_ingestion(BadAdapter(), LocalObjectStore(str(tmp_path)))
    assert res.status == "failed" and any("outside 0-100" in e for e in res.report["errors"])
    with session_scope() as s:
        assert repo.latest_published(s, "synthetic-metro")["id"] == published.dataset_version_id
        failed = s.execute(
            text("SELECT count(*) FROM dataset_versions WHERE status = 'failed'")
        ).scalar_one()
        # a failed version must not leave partial rows behind
        orphan = s.execute(
            text(
                "SELECT count(*) FROM tracts t JOIN dataset_versions d ON d.id = t.dataset_version_id "
                "WHERE d.status <> 'published'"
            )
        ).scalar_one()
    assert failed == 1 and orphan == 0


def test_bounded_map_query(published):
    with session_scope() as s:
        full = repo.tracts_geojson(s, published.dataset_version_id, "scoring-v1", None, 10)
        assert len(full["features"]) == 10 and full["truncated"]
        box = repo.tracts_geojson(
            s, published.dataset_version_id, "scoring-v1", (-97.80, 30.20, -97.775, 30.225), 100
        )
        assert 0 < len(box["features"]) < 36 and not box["truncated"]
    json.dumps(full)  # serializable


def test_inventory_metadata_persists_through_database_and_api_shape(published):
    with session_scope() as s:
        bundle = repo.load_bundle(s, published.dataset_version_id)
        centers = repo.points(s, published.dataset_version_id, "cooling_centers")
    assert bundle.centers[0].hours == "9-17 weekdays"
    assert bundle.centers[0].last_confirmed.isoformat() == "2026-06-15"
    assert centers[0]["hours"] == "9-17 weekdays"
    assert centers[0]["opening_status"] == "unknown"
    assert bundle.inventory_status == "unknown"


def test_full_inventory_evidence_and_roads_persist(clean_db, tmp_path):
    import csv
    import io

    class EvidenceAdapter:
        def fetch(self):
            snap = SyntheticAdapter().fetch()
            rows = list(csv.DictReader(io.StringIO(snap.files["centers.csv"].decode())))
            for row in rows:
                row.update(
                    source="synthetic://inventory-fixture",
                    provenance=json.dumps({"reviewer": "fixture"}),
                    opening_status="unknown",
                )
                row["capacity"] = ""
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            snap.files["centers.csv"] = output.getvalue().encode()
            manifest = json.loads(snap.files["manifest.json"])
            manifest.append(
                {
                    "kind": "cooling_inventory",
                    "inventory_status": "verified",
                    "verified_by": "fixture",
                    "url": "synthetic://inventory-fixture",
                    "observation_period": "2025-01-01",
                    "geographic_coverage": "fictional metro",
                    "license": "CC0",
                }
            )
            snap.files["manifest.json"] = json.dumps(manifest).encode()
            snap.files["roads.json"] = json.dumps(
                {
                    "source": "synthetic://roads",
                    "retrieved_at": "2025-01-01T00:00:00Z",
                    "license": "CC0",
                    "coverage_bbox": [-98, 29, -97, 31],
                    "assumptions": "fixture",
                    "snap_limit_m": 100,
                    "nodes": [
                        {"id": "a", "lon": -97.75, "lat": 30.25},
                        {"id": "b", "lon": -97.74, "lat": 30.25},
                    ],
                    "edges": [{"u": "a", "v": "b", "key": 0, "minutes": {"walk": 1}}],
                }
            ).encode()
            return snap

    result = run_ingestion(EvidenceAdapter(), LocalObjectStore(str(tmp_path)))
    assert result.status == "published"
    with session_scope() as s:
        bundle = repo.load_bundle(s, result.dataset_version_id)
        centers = repo.points(s, result.dataset_version_id, "cooling_centers")
    assert bundle.inventory_status == "verified" and bundle.roads["source"] == "synthetic://roads"
    assert bundle.centers[0].capacity is None and centers[0]["capacity"] is None
    assert centers[0]["provenance"] == {"reviewer": "fixture"}
    assert centers[0]["source"] == "synthetic://inventory-fixture"


def test_synthetic_cleanup_preserves_non_synthetic_records(published, tmp_path):
    from resilience_map.repository.maintenance import remove_synthetic_data

    class NonSyntheticFixture:
        def fetch(self):
            return dataclasses.replace(
                SyntheticAdapter().fetch(),
                study_area_id="non-synthetic-fixture",
                is_synthetic=False,
            )

    other = run_ingestion(NonSyntheticFixture(), LocalObjectStore(str(tmp_path)))
    assert other.status == "published"
    with session_scope() as s:
        preview = remove_synthetic_data(s)
        assert preview["study_areas"] == ["synthetic-metro"] and not preview["applied"]
        assert s.execute(text("SELECT count(*) FROM study_areas")).scalar_one() == 2
        removed = remove_synthetic_data(s, apply=True)
        assert removed["dataset_versions"] == 1
        assert repo.get_published(s, other.dataset_version_id)["id"] == other.dataset_version_id
        assert repo.list_study_areas(s)[0]["id"] == "non-synthetic-fixture"
        assert s.execute(text("SELECT count(*) FROM tracts")).scalar_one() == 36


def test_cooling_upload_publishes_new_version_preserving_original(
    client, published, tmp_path, monkeypatch
):
    from resilience_map.backend import imports
    from resilience_map.backend.routes import imports as routes

    monkeypatch.setattr(imports, "ROOT", tmp_path / "uploads")
    monkeypatch.setattr(routes, "get_object_store", lambda: LocalObjectStore(str(tmp_path)))
    original = SyntheticAdapter().fetch().files["centers.csv"]
    preview = client.post(
        "/v1/imports",
        params={
            "kind": "cooling",
            "study_area_id": "synthetic-metro",
            "dataset_version_id": published.dataset_version_id,
            "source": "County authority",
        },
        content=b"center_id,name,capacity,lon,lat\nLOCAL-1,Local Library,,-97.74,30.26\n",
    )
    assert preview.status_code == 201, preview.text
    assert preview.json()["inventory_status"] == "partial"
    result = client.post(f"/v1/imports/{preview.json()['id']}/publish")
    assert result.status_code == 200, result.text
    new_id = result.json()["dataset_version_id"]
    assert new_id != published.dataset_version_id
    with session_scope() as db:
        assert repo.latest_published(db, "synthetic-metro")["id"] == new_id
        assert len(repo.load_bundle(db, published.dataset_version_id).centers) == 3
        updated = repo.load_bundle(db, new_id)
        assert len(updated.centers) == 1 and updated.centers[0].capacity is None
        assert (
            imports.source_files(
                repo.get_published(db, published.dataset_version_id),
                LocalObjectStore(str(tmp_path)),
            )["centers.csv"]
            == original
        )
    download = client.get(f"/v1/datasets/{new_id}/download")
    assert download.status_code == 200
    assert imports.unpack(download.content)["centers.csv"].startswith(b"center_id")
