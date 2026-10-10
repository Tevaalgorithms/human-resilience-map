"""Job persistence: transactional outbox, atomic claims, idempotent result writes."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from resilience_map.core.errors import ConflictError, NotFoundError


def _count(result: Any) -> int:
    """Rows affected by an UPDATE/DELETE."""
    if not isinstance(result, CursorResult):
        raise TypeError("expected a CursorResult from an UPDATE/DELETE")
    return int(result.rowcount)


def _changed(result: Any) -> bool:
    return _count(result) > 0


TERMINAL = ("completed", "incomplete", "failed")
_JOB_COLS = (
    "id::text AS id, owner_id, status, question, request, study_area_id, "
    "dataset_version_id::text AS dataset_version_id, scoring_version, progress, "
    "clarification, attempts, error, created_at, updated_at, completed_at"
)


def create_job(
    s: Session,
    *,
    owner_id: str,
    question: str,
    request: dict[str, Any],
    study_area_id: str,
    dataset_version_id: str,
    scoring_version: str,
    idempotency_key: str | None,
    max_active: int,
) -> tuple[dict[str, Any], bool]:
    """Insert job + outbox row in ONE transaction. Returns (job, created)."""
    if idempotency_key:
        existing = s.execute(
            text(f"SELECT {_JOB_COLS} FROM jobs WHERE owner_id = :o AND idempotency_key = :k"),
            {"o": owner_id, "k": idempotency_key},
        ).first()
        if existing:
            return dict(existing._mapping), False
    # Serialize per-owner admission so the active-job cap cannot be raced.
    s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:o))"), {"o": owner_id})
    active = s.execute(
        text(
            "SELECT count(*) FROM jobs WHERE owner_id = :o "
            "AND status IN ('queued', 'running', 'needs_input')"
        ),
        {"o": owner_id},
    ).scalar_one()
    if active >= max_active:
        raise ConflictError(f"Too many active analyses ({active}); wait for one to finish")
    row = s.execute(
        text(f"""INSERT INTO jobs (owner_id, idempotency_key, question, request, study_area_id,
                                   dataset_version_id, scoring_version)
                 VALUES (:o, :k, :q, CAST(:r AS jsonb), :a, CAST(:d AS uuid), :sv)
                 RETURNING {_JOB_COLS}"""),
        {
            "o": owner_id,
            "k": idempotency_key,
            "q": question,
            "r": json.dumps(request),
            "a": study_area_id,
            "d": dataset_version_id,
            "sv": scoring_version,
        },
    ).one()
    job = dict(row._mapping)
    s.execute(
        text("INSERT INTO outbox (job_id, kind) VALUES (CAST(:j AS uuid), 'run')"), {"j": job["id"]}
    )
    return job, True


def get_job(s: Session, job_id: str, owner_id: str | None = None) -> dict[str, Any]:
    """Fetch a job; if ``owner_id`` is given, enforce ownership (404 for others' jobs)."""
    row = s.execute(
        text(f"SELECT {_JOB_COLS} FROM jobs WHERE id = CAST(:i AS uuid)"), {"i": job_id}
    ).first()
    if row is None or (owner_id is not None and row.owner_id != owner_id):
        raise NotFoundError("Analysis not found")  # do not reveal existence to non-owners
    return dict(row._mapping)


def list_jobs(s: Session, owner_id: str, limit: int = 15) -> list[dict[str, Any]]:
    rows = s.execute(
        text(
            "SELECT id::text AS id, status, question, study_area_id, created_at FROM jobs "
            "WHERE owner_id = :o ORDER BY created_at DESC LIMIT :n"
        ),
        {"o": owner_id, "n": limit},
    )
    return [dict(r._mapping) for r in rows]


def get_result(s: Session, job_id: str) -> dict[str, Any] | None:
    row = s.execute(
        text("SELECT result FROM job_results WHERE job_id = CAST(:i AS uuid)"), {"i": job_id}
    ).first()
    return None if row is None else dict(row.result)


def request_resume(s: Session, job_id: str, owner_id: str, answers: dict[str, Any]) -> None:
    """Move needs_input -> queued and enqueue a resume via the outbox (single transaction)."""
    job = get_job(s, job_id, owner_id)
    updated = _count(
        s.execute(
            text(
                "UPDATE jobs SET status = 'queued', progress = 'resuming', clarification = NULL, "
                "updated_at = now() WHERE id = CAST(:i AS uuid) AND status = 'needs_input'"
            ),
            {"i": job["id"]},
        )
    )
    if not updated:
        raise ConflictError(f"Analysis is {job['status']}, not awaiting input")
    s.execute(
        text(
            "INSERT INTO outbox (job_id, kind, payload) VALUES (CAST(:j AS uuid), 'resume', "
            "CAST(:p AS jsonb))"
        ),
        {"j": job["id"], "p": json.dumps({"answers": answers})},
    )


def add_review(
    s: Session, job_id: str, owner_id: str, site_id: str, decision: str, notes: str
) -> None:
    job = get_job(s, job_id, owner_id)
    s.execute(
        text(
            "INSERT INTO human_reviews (job_id, site_id, reviewer, decision, notes) "
            "VALUES (CAST(:j AS uuid), :s, :r, :d, :n)"
        ),
        {"j": job["id"], "s": site_id, "r": owner_id, "d": decision, "n": notes},
    )


def list_reviews(s: Session, job_id: str) -> list[dict[str, Any]]:
    rows = s.execute(
        text(
            "SELECT site_id, reviewer, decision, notes, created_at FROM human_reviews "
            "WHERE job_id = CAST(:j AS uuid) ORDER BY id"
        ),
        {"j": job_id},
    )
    return [dict(r._mapping) for r in rows]


# ---- worker side --------------------------------------------------------------------------


def claim_job(s: Session, job_id: str, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
    """Atomically claim a queued job (or one whose previous worker's lease expired)."""
    row = s.execute(
        text(f"""UPDATE jobs SET status = 'running', claimed_by = :w, attempts = attempts + 1,
                    lease_expires_at = now() + make_interval(secs => :l), updated_at = now()
                 WHERE id = CAST(:i AS uuid)
                   AND (status = 'queued' OR (status = 'running' AND lease_expires_at < now()))
                 RETURNING {_JOB_COLS}"""),
        {"i": job_id, "w": worker_id, "l": lease_seconds},
    ).first()
    return None if row is None else dict(row._mapping)


def extend_lease(s: Session, job_id: str, worker_id: str, lease_seconds: int) -> bool:
    return _changed(
        s.execute(
            text(
                "UPDATE jobs SET lease_expires_at = now() + make_interval(secs => :l) "
                "WHERE id = CAST(:i AS uuid) AND claimed_by = :w AND status = 'running'"
            ),
            {"i": job_id, "w": worker_id, "l": lease_seconds},
        )
    )


def set_progress(s: Session, job_id: str, progress: str) -> None:
    s.execute(
        text(
            "UPDATE jobs SET progress = :p, updated_at = now() "
            "WHERE id = CAST(:i AS uuid) AND status = 'running'"
        ),
        {"i": job_id, "p": progress},
    )


def set_needs_input(s: Session, job_id: str, worker_id: str, clarification: dict[str, Any]) -> bool:
    return _changed(
        s.execute(
            text(
                "UPDATE jobs SET status = 'needs_input', progress = 'needs_input', "
                "clarification = CAST(:c AS jsonb), lease_expires_at = NULL, claimed_by = NULL, "
                "updated_at = now() WHERE id = CAST(:i AS uuid) AND claimed_by = :w "
                "AND status = 'running'"
            ),
            {"i": job_id, "w": worker_id, "c": json.dumps(clarification)},
        )
    )


def complete_job(
    s: Session, job_id: str, worker_id: str, outcome: str, result: dict[str, Any]
) -> bool:
    """Idempotent: result row is insert-once; status flips only for the current claim holder."""
    if outcome not in ("completed", "incomplete"):
        raise ValueError(outcome)
    s.execute(
        text(
            "INSERT INTO job_results (job_id, outcome, result) "
            "VALUES (CAST(:i AS uuid), :o, CAST(:r AS jsonb)) ON CONFLICT (job_id) DO NOTHING"
        ),
        {"i": job_id, "o": outcome, "r": json.dumps(result, default=str)},
    )
    return _changed(
        s.execute(
            text(
                "UPDATE jobs SET status = :o, progress = :o, completed_at = now(), updated_at = now(), "
                "lease_expires_at = NULL WHERE id = CAST(:i AS uuid) AND status = 'running' "
                "AND claimed_by = :w"
            ),
            {"i": job_id, "o": outcome, "w": worker_id},
        )
    )


def fail_job(s: Session, job_id: str, error: str) -> None:
    s.execute(
        text(
            "UPDATE jobs SET status = 'failed', progress = 'failed', error = :e, "
            "completed_at = now(), updated_at = now(), lease_expires_at = NULL "
            "WHERE id = CAST(:i AS uuid) AND status IN ('queued', 'running')"
        ),
        {"i": job_id, "e": error[:2000]},
    )


def reap_exhausted(s: Session, max_attempts: int) -> int:
    """Fail jobs whose lease expired after the final allowed attempt (their message is in the DLQ)."""
    return _count(
        s.execute(
            text(
                "UPDATE jobs SET status = 'failed', progress = 'failed', "
                "error = 'Exceeded maximum processing attempts', completed_at = now(), "
                "updated_at = now() WHERE status = 'running' AND lease_expires_at < now() "
                "AND attempts >= :m"
            ),
            {"m": max_attempts},
        )
    )


# ---- outbox -------------------------------------------------------------------------------


def fetch_outbox(s: Session, limit: int = 20) -> list[dict[str, Any]]:
    rows = s.execute(
        text(
            "SELECT id, job_id::text AS job_id, kind, payload FROM outbox "
            "WHERE dispatched_at IS NULL ORDER BY id FOR UPDATE SKIP LOCKED LIMIT :n"
        ),
        {"n": limit},
    )
    return [dict(r._mapping) for r in rows]


def mark_dispatched(s: Session, outbox_id: int) -> None:
    s.execute(text("UPDATE outbox SET dispatched_at = now() WHERE id = :i"), {"i": outbox_id})
