from __future__ import annotations

import pytest

from resilience_map.core.object_store import LocalObjectStore
from resilience_map.ingestion.pipeline import run_ingestion
from tests.fixtures.synthetic import SyntheticAdapter


@pytest.fixture
def published(clean_db, tmp_path):
    return run_ingestion(SyntheticAdapter(), LocalObjectStore(str(tmp_path)))


@pytest.fixture
def client(published):
    from fastapi.testclient import TestClient

    from resilience_map.backend.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)
