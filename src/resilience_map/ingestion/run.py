"""Ingestion entry point (used by the CLI and the scheduled ECS task)."""

from __future__ import annotations

import os

from resilience_map.core.object_store import get_object_store
from resilience_map.ingestion.pipeline import IngestResult, run_ingestion
from resilience_map.ingestion.sources import FileAdapter, SourceAdapter


def build_adapter(source: str) -> SourceAdapter:
    if source == "files":
        return FileAdapter(
            os.environ["HRM_INGEST_DIR"],
            os.environ["HRM_INGEST_STUDY_AREA_ID"],
            os.environ.get("HRM_INGEST_STUDY_AREA_NAME", os.environ["HRM_INGEST_STUDY_AREA_ID"]),
        )
    raise ValueError(f"Unknown ingestion source {source!r} (expected 'files')")


def ingest(source: str) -> IngestResult:
    return run_ingestion(build_adapter(source), get_object_store())
