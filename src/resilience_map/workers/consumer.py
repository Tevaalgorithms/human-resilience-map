"""Queue consumer: pulls job messages and executes them. Safe under duplicate delivery."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading

from resilience_map.agents.graph import Deps
from resilience_map.agents.llm import get_llm
from resilience_map.core.config import get_settings
from resilience_map.core.db import session_scope
from resilience_map.core.queue import JobQueue, QueueMessage, get_queue
from resilience_map.repository import jobs as jobs_repo
from resilience_map.repository.datasets import PostgresDatasetLoader
from resilience_map.workers.checkpoint import postgres_checkpointer
from resilience_map.workers.executor import execute_job

log = logging.getLogger(__name__)


def handle_message(
    msg: QueueMessage, queue: JobQueue, deps: Deps, saver: object, worker_id: str
) -> None:
    s = deps.settings
    job_id = str(msg.body["job_id"])
    payload = msg.body.get("payload")
    answers = payload.get("answers") if isinstance(payload, dict) else None
    try:
        outcome = execute_job(
            job_id,
            answers,
            worker_id,
            deps,
            saver,  # type: ignore[arg-type]
            on_beat=lambda secs: queue.extend_visibility(msg.receipt, secs),
        )
        log.info("job processed", extra={"outcome": outcome})
        queue.delete(msg.receipt)
    except Exception as e:
        log.exception(
            "job execution error (receive %d/%d)", msg.receive_count, s.queue_max_receive_count
        )
        if msg.receive_count >= s.queue_max_receive_count:
            with session_scope() as db:
                jobs_repo.fail_job(db, job_id, f"{type(e).__name__}: {e}")
            # message is intentionally not deleted: the redrive policy moves it to the DLQ


def run_consumer(stop: threading.Event | None = None) -> None:
    settings = get_settings()
    stop = stop or threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    queue = get_queue(settings)
    deps = Deps(llm=get_llm(settings), datasets=PostgresDatasetLoader(), settings=settings)
    log.info(
        "worker %s started (queue=%s, llm=%s, model=%s)",
        worker_id,
        settings.queue_backend,
        settings.llm_provider,
        settings.llm_model,
    )
    with postgres_checkpointer() as saver:
        while not stop.is_set():
            for msg in queue.receive(max_messages=1, wait_seconds=10):
                handle_message(msg, queue, deps, saver, worker_id)
    log.info("worker stopped")
