from __future__ import annotations

import pytest
from sqlalchemy import text

from resilience_map.agents.graph import Deps
from resilience_map.agents.llm import StubLLM
from resilience_map.core.config import get_settings
from resilience_map.core.db import session_scope
from resilience_map.core.errors import ConflictError, NotFoundError
from resilience_map.core.queue import LocalQueue
from resilience_map.repository import jobs as jobs_repo
from resilience_map.repository.datasets import PostgresDatasetLoader
from resilience_map.workers.checkpoint import postgres_checkpointer
from resilience_map.workers.consumer import handle_message
from resilience_map.workers.dispatcher import dispatch_once

pytestmark = pytest.mark.integration
Q = "Where should we open two cooling centers? driving 12 minutes"


def make_job(published, owner="alice", question=Q, key=None, constraints=None):
    with session_scope() as s:
        job, created = jobs_repo.create_job(
            s,
            owner_id=owner,
            question=question,
            request={"constraints": constraints or {}},
            study_area_id="synthetic-metro",
            dataset_version_id=published.dataset_version_id,
            scoring_version="scoring-v1",
            idempotency_key=key,
            max_active=3,
        )
    return job, created


def count(sql, **p):
    with session_scope() as s:
        return s.execute(text(sql), p).scalar_one()


def test_job_and_outbox_are_one_transaction(published):
    with pytest.raises(RuntimeError), session_scope() as s:
        jobs_repo.create_job(
            s,
            owner_id="a",
            question="q",
            request={},
            study_area_id="synthetic-metro",
            dataset_version_id=published.dataset_version_id,
            scoring_version="scoring-v1",
            idempotency_key=None,
            max_active=3,
        )
        raise RuntimeError("crash before commit")
    assert count("SELECT count(*) FROM jobs") == 0 and count("SELECT count(*) FROM outbox") == 0
    make_job(published)
    assert count("SELECT count(*) FROM jobs") == 1 and count("SELECT count(*) FROM outbox") == 1


def test_idempotency_key_and_active_cap(published):
    j1, c1 = make_job(published, key="k1")
    j2, c2 = make_job(published, key="k1")
    assert c1 and not c2 and j1["id"] == j2["id"]
    make_job(published)
    make_job(published)
    with pytest.raises(ConflictError):
        make_job(published)  # 4th active job for the same owner (cap = 3)
    make_job(published, owner="bob")  # other owners unaffected


def test_atomic_claim_only_one_winner(published):
    job, _ = make_job(published)
    with session_scope() as s:
        first = jobs_repo.claim_job(s, job["id"], "w1", 60)
    with session_scope() as s:
        second = jobs_repo.claim_job(s, job["id"], "w2", 60)
    assert first is not None and second is None
    # after the lease expires another worker may take over (crash recovery)
    with session_scope() as s:
        s.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'"))
        assert jobs_repo.claim_job(s, job["id"], "w2", 60)["attempts"] == 2


def test_complete_is_idempotent(published):
    job, _ = make_job(published)
    with session_scope() as s:
        jobs_repo.claim_job(s, job["id"], "w1", 60)
    res = {"outcome": "completed", "n": 1}
    with session_scope() as s:
        assert jobs_repo.complete_job(s, job["id"], "w1", "completed", res)
    with session_scope() as s:
        assert not jobs_repo.complete_job(s, job["id"], "w1", "completed", {**res, "n": 2})
    assert count("SELECT count(*) FROM job_results") == 1
    assert count("SELECT (result->>'n')::int FROM job_results") == 1


def test_ownership_is_enforced(published):
    job, _ = make_job(published)
    with session_scope() as s, pytest.raises(NotFoundError):
        jobs_repo.get_job(s, job["id"], "mallory")


def test_resume_only_from_needs_input(published):
    job, _ = make_job(published)
    with session_scope() as s, pytest.raises(ConflictError):
        jobs_repo.request_resume(s, job["id"], "alice", {"travel_mode": "walk"})


def _deps():
    return Deps(llm=StubLLM(), datasets=PostgresDatasetLoader(), settings=get_settings())


def _drain(saver, n=5):
    queue = LocalQueue(60, 3)
    dispatch_once(queue)
    handled = 0
    for msg in queue.receive(max_messages=n, wait_seconds=1):
        handle_message(msg, queue, _deps(), saver, "test-worker")
        handled += 1
    return handled


def test_end_to_end_with_clarification_and_postgres_checkpoints(published):
    job, _ = make_job(published, question="Where should we open two cooling centers?")
    with postgres_checkpointer() as saver:
        assert _drain(saver) == 1
        with session_scope() as s:
            j = jobs_repo.get_job(s, job["id"])
            assert j["status"] == "needs_input" and j["clarification"]["needs"]
            jobs_repo.request_resume(
                s, job["id"], "alice", {"travel_mode": "drive", "threshold_minutes": 10}
            )
        assert _drain(saver) == 1
    with session_scope() as s:
        j = jobs_repo.get_job(s, job["id"])
        res = jobs_repo.get_result(s, job["id"])
    assert (
        j["status"] == "completed"
        and res["outcome"] == "completed"
        and res["verification"]["passed"]
    )
    assert (
        count("SELECT count(*) FROM checkpoints.checkpoints") > 0
    )  # persisted in the checkpoints schema


def test_duplicate_delivery_creates_one_result(published):
    job, _ = make_job(published)
    queue = LocalQueue(60, 3)
    dispatch_once(queue)
    queue.send({"job_id": job["id"], "kind": "run", "payload": {}})  # duplicate delivery
    with postgres_checkpointer() as saver:
        for msg in queue.receive(max_messages=5, wait_seconds=1):
            handle_message(msg, queue, _deps(), saver, "w")
    assert count("SELECT count(*) FROM job_results") == 1
    assert count("SELECT attempts FROM jobs") == 1  # the duplicate was skipped, not re-run
    assert count("SELECT count(*) FROM queue_messages") == 0  # both acknowledged


def test_worker_crash_midway_resumes_from_checkpoint(published):
    """A worker that dies after claiming leaves an expired lease; redelivery finishes the job."""
    job, _ = make_job(published)
    with session_scope() as s:
        jobs_repo.claim_job(s, job["id"], "dead-worker", 60)
        s.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'"))
    queue = LocalQueue(60, 3)
    dispatch_once(queue)
    with postgres_checkpointer() as saver:
        for msg in queue.receive(max_messages=1, wait_seconds=1):
            handle_message(msg, queue, _deps(), saver, "w2")
    with session_scope() as s:
        assert jobs_repo.get_job(s, job["id"])["status"] == "completed"
    assert count("SELECT attempts FROM jobs") == 2


def test_unexpected_failure_on_last_attempt_marks_job_failed(published, monkeypatch):
    job, _ = make_job(published)
    monkeypatch.setattr(get_settings(), "queue_max_receive_count", 1)  # first receive is the last
    queue = LocalQueue(0, 1)
    dispatch_once(queue)

    class Boom:
        def complete(self, *a, **k):
            raise RuntimeError("model endpoint down")

    deps = Deps(llm=Boom(), datasets=PostgresDatasetLoader(), settings=get_settings())
    with postgres_checkpointer() as saver:
        for msg in queue.receive(max_messages=1, wait_seconds=1):
            handle_message(msg, queue, deps, saver, "w")
    with session_scope() as s:
        j = jobs_repo.get_job(s, job["id"])
    assert j["status"] == "failed" and "model endpoint down" in j["error"]
