"""Runs one job through the LangGraph workflow with retry-safe recovery.

Recovery rules (all keyed by the job's checkpoint thread):
* finished checkpoint     -> just (idempotently) persist the stored result;
* interrupted checkpoint  -> resume with the clarification answers;
* mid-run checkpoint      -> continue from the last completed node;
* no checkpoint           -> start fresh.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from resilience_map.agents.graph import Deps, build_graph
from resilience_map.core.config import Settings
from resilience_map.core.db import session_scope
from resilience_map.core.logging import job_id_var
from resilience_map.repository import jobs as jobs_repo

log = logging.getLogger(__name__)


class Heartbeat:
    """Extends the job lease (and optionally the queue visibility) while a job runs."""

    def __init__(
        self, job_id: str, worker_id: str, settings: Settings, on_beat: Any = None
    ) -> None:
        self._args = (job_id, worker_id)
        self._lease = settings.job_lease_seconds
        self._on_beat = on_beat
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._lease / 3):
            try:
                with session_scope() as s:
                    jobs_repo.extend_lease(s, *self._args, self._lease)
                if self._on_beat:
                    self._on_beat(self._lease)
            except Exception:
                log.exception("heartbeat failed")

    def __enter__(self) -> Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def execute_job(
    job_id: str,
    answers: dict[str, Any] | None,
    worker_id: str,
    deps: Deps,
    saver: BaseCheckpointSaver[Any],
    on_beat: Any = None,
) -> str:
    """Returns one of: skipped | needs_input | completed | incomplete."""
    token = job_id_var.set(job_id)
    try:
        with session_scope() as s:
            job = jobs_repo.claim_job(s, job_id, worker_id, deps.settings.job_lease_seconds)
        if job is None:
            log.info("job not claimable (duplicate delivery, terminal, or held by another worker)")
            return "skipped"

        def progress(jid: str, msg: str) -> None:
            with session_scope() as s:
                jobs_repo.set_progress(s, jid, msg)

        deps.progress = progress
        graph = build_graph(deps, saver)
        config: dict[str, Any] = {"configurable": {"thread_id": job_id}}
        snap = graph.get_state(config)
        interrupted = any(t.interrupts for t in snap.tasks)

        with Heartbeat(job_id, worker_id, deps.settings, on_beat):
            if snap.values and not snap.next:
                out: dict[str, Any] = dict(snap.values)
            elif interrupted and answers is not None:
                out = graph.invoke(Command(resume=answers), config)
            elif interrupted:
                out = {"__interrupt__": [i for t in snap.tasks for i in t.interrupts]}
            elif snap.next:
                out = graph.invoke(None, config)
            else:
                req = job["request"]
                out = graph.invoke(
                    {
                        "job_id": job_id,
                        "question": job["question"],
                        "study_area_id": job["study_area_id"],
                        "dataset_version_id": job["dataset_version_id"],
                        "explicit": req.get("constraints", {}),
                        "comparison_request": req.get("comparison"),
                        "uncertainty_request": req.get("uncertainty"),
                        "started_at": time.time(),
                        "usage": {
                            "llm_calls": 0,
                            "tokens": 0,
                            "tool_calls": 0,
                            "analysis_runs": 0,
                            "solver_seconds": 0.0,
                        },
                        "prompts": [],
                        "clarify_round": 0,
                    },
                    config,
                )

        if out.get("__interrupt__"):
            payload = out["__interrupt__"][0].value
            with session_scope() as s:
                jobs_repo.set_needs_input(s, job_id, worker_id, dict(payload))
            log.info("job awaiting clarification")
            return "needs_input"
        result = out["result"]
        with session_scope() as s:
            jobs_repo.complete_job(s, job_id, worker_id, result["outcome"], result)
        log.info("job finished", extra={"outcome": result["outcome"]})
        return str(result["outcome"])
    finally:
        job_id_var.reset(token)
