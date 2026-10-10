from __future__ import annotations

import os

# Tests never touch the dev database: a dedicated database is used for integration tests.
TEST_DB_URL = os.environ.get(
    "HRM_TEST_DATABASE_URL",
    "postgresql+psycopg://resilience:resilience@localhost:5432/resilience_test",
)
os.environ["HRM_DATABASE_URL"] = TEST_DB_URL
os.environ["HRM_ENVIRONMENT"] = "test"
os.environ["HRM_AUTH_MODE"] = "local"
os.environ["HRM_LLM_PROVIDER"] = "stub"
os.environ["HRM_SECRET_SOURCE"] = "environment"
os.environ["HRM_RUNTIME_ROLE"] = "api"
os.environ["HRM_OBJECT_STORE_PATH"] = "./.data/test-objects"
os.environ["HRM_QUEUE_BACKEND"] = "local"

import pytest  # noqa: E402

from resilience_map.core.models import DatasetBundle  # noqa: E402
from resilience_map.ingestion.pipeline import parse_snapshot  # noqa: E402
from tests.fixtures.synthetic import SyntheticAdapter  # noqa: E402


@pytest.fixture(scope="session")
def synthetic():
    """(bundle, geometries) parsed from the deterministic synthetic snapshot, no DB involved."""
    bundle, geoms = parse_snapshot(SyntheticAdapter().fetch().files)
    bundle = bundle.model_copy(
        update={
            "dataset_version_id": "dv-test",
            "version": "v-test",
            "study_area_id": "synthetic-metro",
        }
    )
    return bundle, geoms


@pytest.fixture
def bundle(synthetic) -> DatasetBundle:
    return synthetic[0]


def _admin_url() -> str:
    return TEST_DB_URL.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="session")
def db():
    """Fresh, migrated test database. Skips integration tests when PostGIS is unreachable."""
    import psycopg

    from resilience_map.core.config import get_settings
    from resilience_map.db.migrate import run_migrations

    name = TEST_DB_URL.rsplit("/", 1)[1].split("?")[0]
    try:
        with psycopg.connect(
            _admin_url().replace("+psycopg", ""), autocommit=True, connect_timeout=3
        ) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            c.execute(f'CREATE DATABASE "{name}"')
    except psycopg.OperationalError:
        pytest.skip("PostGIS not reachable (start it with `make db-up`)")
    get_settings.cache_clear()
    run_migrations()
    yield
    from resilience_map.core.db import get_engine

    get_engine().dispose()


@pytest.fixture
def clean_db(db):
    from sqlalchemy import text

    from resilience_map.core.db import session_scope

    with session_scope() as s:
        s.execute(
            text(
                "TRUNCATE jobs, outbox, job_results, human_reviews, queue_messages, "
                "dataset_versions, study_areas CASCADE"
            )
        )
    from resilience_map.repository import datasets

    datasets._cache.clear()
    yield
