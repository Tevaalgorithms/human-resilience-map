"""Explicit cleanup of fictional application datasets; real inputs are never targeted."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def remove_synthetic_data(session: Session, *, apply: bool = False) -> dict[str, Any]:
    """Preview by default; callers commit the transaction when applying.

    Related fictional analysis jobs are removed with their outbox/results/reviews. Active jobs
    block deletion to avoid racing workers. Immutable object storage is cleaned separately.
    """
    areas = list(
        session.execute(text("SELECT id FROM study_areas WHERE is_synthetic ORDER BY id")).scalars()
    )
    versions = session.execute(
        text(
            "SELECT count(*) FROM dataset_versions WHERE study_area_id IN "
            "(SELECT id FROM study_areas WHERE is_synthetic)"
        )
    ).scalar_one()
    jobs = session.execute(
        text(
            "SELECT count(*) FROM jobs WHERE study_area_id IN "
            "(SELECT id FROM study_areas WHERE is_synthetic)"
        )
    ).scalar_one()
    result = {"study_areas": areas, "dataset_versions": versions, "jobs": jobs, "applied": apply}
    if not apply:
        return result
    # Lock affected catalogs to prevent new jobs/ingestions acquiring FK references during removal.
    session.execute(text("SELECT id FROM study_areas WHERE is_synthetic FOR UPDATE")).all()
    active = session.execute(
        text(
            "SELECT count(*) FROM jobs WHERE status = 'running' AND study_area_id IN "
            "(SELECT id FROM study_areas WHERE is_synthetic)"
        )
    ).scalar_one()
    if active:
        raise ValueError("Stop analysis workers before removing synthetic data with running jobs")
    session.execute(
        text(
            "DELETE FROM queue_messages WHERE body->>'job_id' IN "
            "(SELECT id::text FROM jobs WHERE study_area_id IN "
            "(SELECT id FROM study_areas WHERE is_synthetic))"
        )
    )
    session.execute(
        text(
            "DELETE FROM jobs WHERE study_area_id IN (SELECT id FROM study_areas WHERE is_synthetic)"
        )
    )
    session.execute(
        text(
            "DELETE FROM dataset_versions WHERE study_area_id IN "
            "(SELECT id FROM study_areas WHERE is_synthetic)"
        )
    )
    session.execute(text("DELETE FROM study_areas WHERE is_synthetic"))
    return result
