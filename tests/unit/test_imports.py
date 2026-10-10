from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from resilience_map.backend import imports
from resilience_map.backend.main import create_app
from resilience_map.core.object_store import LocalObjectStore
from resilience_map.ingestion.pipeline import IngestResult, snapshot_hash
from tests.fixtures.synthetic import SyntheticAdapter


def package(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def test_zip_upload_preview_and_owner_scoped_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "ROOT", tmp_path)
    client = TestClient(create_app())
    response = client.post(
        "/v1/imports?kind=region&study_area_id=test-region",
        content=package(SyntheticAdapter().fetch().files),
        headers={"X-Dev-User": "alice", "Content-Type": "application/zip"},
    )
    assert response.status_code == 201
    preview = response.json()
    assert preview["report"]["passed"] and preview["centers"] == 3
    assert "owner" not in preview
    ident = preview["id"]
    assert client.get(f"/v1/imports/{ident}", headers={"X-Dev-User": "bob"}).status_code == 404
    assert (
        client.post(f"/v1/imports/{ident}/publish", headers={"X-Dev-User": "bob"}).status_code
        == 404
    )
    captured = []

    def ingest(adapter, store):
        captured.append(adapter.fetch())
        return IngestResult("published", "dv-new", "v-new", {"passed": True})

    monkeypatch.setattr(imports, "run_ingestion", ingest)
    result = client.post(f"/v1/imports/{ident}/publish", headers={"X-Dev-User": "alice"})
    assert result.status_code == 200 and result.json()["dataset_version_id"] == "dv-new"
    assert captured[0].study_area_id == "test-region"


@pytest.mark.parametrize("name", ["../centers.csv", "folder/centers.csv", "extra.txt"])
def test_zip_rejects_paths_and_unexpected_entries(name):
    with pytest.raises(ValueError, match="root"):
        imports.unpack(package({name: b"data"}))


def test_zip_rejects_duplicates_and_expansion_limit(monkeypatch):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("centers.csv", b"one")
        with pytest.warns(UserWarning):
            archive.writestr("centers.csv", b"two")
    with pytest.raises(ValueError, match="unique"):
        imports.unpack(output.getvalue())
    monkeypatch.setattr(imports, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(ValueError, match="expanded file"):
        imports.unpack(package({"centers.csv": b"x" * 20}))


def test_cooling_unknown_capacity_partial_and_empty_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "ROOT", tmp_path)
    original = SyntheticAdapter().fetch().files
    data = b"center_id,name,capacity,lon,lat\nC1,Library,,-97.7,30.3\n"
    files = imports.replace_cooling(original, data, "https://county.example/inventory")
    preview = imports.stage(files, "area", "Area", "alice")
    assert preview["inventory_status"] == "partial" and preview["centers"] == 1
    assert imports.parse_snapshot(files)[0].centers[0].capacity is None
    assert files["tracts.geojson"] == original["tracts.geojson"]
    empty = imports.replace_cooling(original, data.splitlines()[0] + b"\n", "")
    assert imports.stage(empty, "area", "Area", "alice")["inventory_status"] == "unknown"


@pytest.mark.parametrize(
    "rows",
    [
        "C1,Library,-97.7,30.3\nC1,Other,-97.7,30.3\n",
        "C1,Library,999,30.3\n",
        ",Library,-97.7,30.3\n",
    ],
)
def test_cooling_rejects_duplicates_invalid_coordinates_and_blank_ids(tmp_path, monkeypatch, rows):
    monkeypatch.setattr(imports, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        files = imports.replace_cooling(
            SyntheticAdapter().fetch().files, ("center_id,name,lon,lat\n" + rows).encode(), ""
        )
        imports.stage(files, "area", "Area", "alice")


def test_incomplete_region_is_previewed_and_cannot_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "ROOT", tmp_path)
    files = SyntheticAdapter().fetch().files
    files["indicators.csv"] = b"tract_id\n"
    preview = imports.stage(files, "area", "Area", "alice")
    assert preview["state"] == "needs_data" and not preview["report"]["passed"]
    with pytest.raises(HTTPException) as exc:
        imports.publish(preview["id"], "alice", LocalObjectStore(str(tmp_path / "objects")))
    assert exc.value.status_code == 409


def test_raw_source_download_uses_original_snapshot(tmp_path):
    store = LocalObjectStore(str(tmp_path))
    snapshot = SyntheticAdapter().fetch()
    digest = snapshot_hash(snapshot)
    for name, data in snapshot.files.items():
        store.put(f"raw/area/{digest}/{name}", data)
    dataset = {
        "study_area_id": "area",
        "source_manifest": [
            {
                "snapshot_sha256": digest,
                "checksums": dict.fromkeys(snapshot.files, "test"),
            }
        ],
    }
    assert imports.source_files(dataset, store) == snapshot.files
    dataset["source_manifest"] = []
    with pytest.raises(HTTPException) as exc:
        imports.source_files(dataset, store)
    assert exc.value.status_code == 409


def test_upload_limits_bad_zip_and_template(monkeypatch):
    client = TestClient(create_app())
    template = client.get("/v1/cooling-centers/template")
    assert template.status_code == 200 and "center_id" in template.text
    assert (
        client.post("/v1/imports?kind=region&study_area_id=../escape", content=b"bad").status_code
        == 422
    )
    assert (
        client.post("/v1/imports?kind=region&study_area_id=area", content=b"bad").status_code == 422
    )
    monkeypatch.setattr(imports, "MAX_UPLOAD_BYTES", 3)
    assert (
        client.post("/v1/imports?kind=region&study_area_id=area", content=b"1234").status_code
        == 413
    )


def test_cooling_provider_rejects_unsupported_county():
    from resilience_map.backend.preparation import prepare

    with pytest.raises(HTTPException) as exc:
        prepare("48", "453", "alice", include_cooling=True)
    assert exc.value.status_code == 422
    assert "Los Angeles" in exc.value.detail


def test_online_cooling_preparation_wires_provider_into_bounded_subprocess(tmp_path, monkeypatch):
    from resilience_map.backend import preparation
    from resilience_map.ingestion.sources import REQUIRED_FILES

    monkeypatch.setattr(preparation, "ROOT", tmp_path)
    monkeypatch.setenv("HRM_ENABLE_PREPARATION", "true")
    commands = []

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    class FixtureProcess:
        def __init__(self, args, **kwargs):
            from pathlib import Path

            commands.append(args)
            directory = Path(args[5])
            directory.mkdir()
            for name in REQUIRED_FILES:
                (directory / name).write_bytes(b"fixture")

        def wait(self, timeout=None):
            assert timeout == preparation.PROCESS_TIMEOUT_SECONDS
            return 0

    monkeypatch.setattr(preparation.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(preparation.subprocess, "Popen", FixtureProcess)
    ident = preparation.prepare(
        "06", "037", "alice", (-118.3, 34.0, -118.2, 34.1), include_cooling=True
    )
    assert commands[0][-1] == "cooling"
    assert commands[0][-2] == "-118.3,34.0,-118.2,34.1"
    assert preparation.get_status(ident, "alice")["state"] == "downloaded_unpublished"
    assert preparation.get_status(ident, "alice")["cooling_requested"] is True
