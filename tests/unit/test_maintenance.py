"""Regression checks for shared contracts and safe maintenance operations."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from resilience_map.backend import preparation
from resilience_map.backend.schemas import Clarification, ConstraintsIn
from resilience_map.core.models import ScenarioConstraints


@pytest.mark.parametrize("schema", [Clarification, ConstraintsIn, ScenarioConstraints])
def test_shared_bbox_contract(schema):
    required = {"num_centers": 1, "travel_mode": "walk", "threshold_minutes": 15}
    assert schema(**required, bbox=(-97.8, 30.2, -97.7, 30.3)).bbox == (-97.8, 30.2, -97.7, 30.3)
    for bounds in [(0, 0, 1, 1), (-98, 30, -97, 29), (-120, 25, -70, 45)]:
        with pytest.raises(ValidationError):
            schema(**required, bbox=bounds)


def test_preparation_process_failure_is_persisted_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation, "ROOT", tmp_path)
    monkeypatch.setenv("HRM_ENABLE_PREPARATION", "true")

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    def fail_process(*args, **kwargs):
        raise OSError("fixture process unavailable")

    monkeypatch.setattr(preparation.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(preparation.subprocess, "Popen", fail_process)
    for _ in range(2):
        ident = preparation.prepare("48", "453", "owner")
        assert preparation.get_status(ident, "owner")["state"] == "failed"
    assert not preparation._lock.locked()


def test_preparation_setup_failure_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation, "ROOT", tmp_path)
    monkeypatch.setenv("HRM_ENABLE_PREPARATION", "true")

    def fail_save(*args, **kwargs):
        raise OSError("fixture disk failure")

    monkeypatch.setattr(Path, "write_text", fail_save)
    with pytest.raises(OSError, match="fixture disk failure"):
        preparation.prepare("48", "453", "owner")
    assert not preparation._lock.locked()


def test_cleaner_preserves_sources_runtime_data_and_symlinks(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "clean_generated.py"
    spec = importlib.util.spec_from_file_location("clean_generated", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    removable = [tmp_path / ".coverage", tmp_path / "src" / "pkg" / "__pycache__" / "x.pyc"]
    preserved = [
        tmp_path / ".data" / "state.json",
        tmp_path / ".venv" / "__pycache__" / "x.pyc",
        tmp_path / "data" / "inventory.json",
        tmp_path / "src" / "pkg" / "module.py",
        tmp_path / "deploy" / "lightsail" / "runtime.env",
    ]
    for path in removable + preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    external = tmp_path / "external" / "__pycache__"
    external.mkdir(parents=True)
    (external / "sentinel").write_text("keep")
    (tmp_path / "src" / "linked").symlink_to(external.parent, target_is_directory=True)
    assert len(module.clean(tmp_path, dry_run=True)) == 2
    assert all(path.exists() for path in removable + preserved)
    module.clean(tmp_path)
    assert all(not path.exists() for path in removable)
    assert all(path.exists() for path in preserved)
    assert (external / "sentinel").exists()


def test_cleaner_terraform_option_preserves_state_and_lockfiles(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "clean_generated.py"
    spec = importlib.util.spec_from_file_location("clean_generated", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stack = tmp_path / "infrastructure" / "ssm"
    cache = stack / ".terraform" / "providers" / "fixture"
    preserved = [stack / "terraform.tfstate", stack / ".terraform.lock.hcl", stack / "main.tf"]
    for path in [cache, *preserved]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    assert module.clean(tmp_path) == []
    assert module.clean(tmp_path, include_terraform=True, dry_run=True) == [stack / ".terraform"]
    assert cache.exists()
    module.clean(tmp_path, include_terraform=True)
    assert not cache.exists()
    assert all(path.exists() for path in preserved)


def test_source_zip_is_complete_owner_scoped_and_not_analysis_ready(tmp_path, monkeypatch):
    import io
    import json
    import zipfile

    from fastapi import HTTPException

    from resilience_map.ingestion.sources import REQUIRED_FILES

    monkeypatch.setattr(preparation, "ROOT", tmp_path)
    ident = "a" * 32
    directory = tmp_path / ident
    sources = directory / "sources"
    sources.mkdir(parents=True)
    status = {"owner": "owner", "state": "downloaded_unpublished", "analysis_ready": False}
    (directory / "status.json").write_text(json.dumps(status))
    for name in REQUIRED_FILES:
        (sources / name).write_text("offline provider fixture")
    with zipfile.ZipFile(io.BytesIO(preparation.download_sources(ident, "owner"))) as archive:
        assert set(archive.namelist()) == set(REQUIRED_FILES)
    assert not preparation.get_status(ident, "owner")["analysis_ready"]
    with pytest.raises(HTTPException) as denied:
        preparation.download_sources(ident, "other")
    assert denied.value.status_code == 404
    (sources / "centers.csv").unlink()
    with pytest.raises(HTTPException) as incomplete:
        preparation.download_sources(ident, "owner")
    assert incomplete.value.status_code == 409


def test_production_ingestion_has_no_fictional_adapters():
    from resilience_map.ingestion.run import build_adapter

    for source in ("synthetic", "demo"):
        with pytest.raises(ValueError, match="expected 'files'"):
            build_adapter(source)
