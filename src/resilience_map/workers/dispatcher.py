"""Outbox dispatcher: reliably forwards committed job rows to the queue (at-least-once)."""

from __future__ import annotations

import logging
import signal
import threading

from resilience_map.core.config import get_settings
from resilience_map.core.db import session_scope
from resilience_map.core.queue import JobQueue, get_queue
from resilience_map.repository import jobs as jobs_repo

log = logging.getLogger(__name__)


def dispatch_once(queue: JobQueue) -> int:
    """Send pending outbox rows. A crash between send and commit re-sends: consumers are idempotent."""
    with session_scope() as s:
        rows = jobs_repo.fetch_outbox(s)
        for r in rows:
            queue.send({"job_id": r["job_id"], "kind": r["kind"], "payload": r["payload"]})
            jobs_repo.mark_dispatched(s, r["id"])
        return len(rows)


def run_dispatcher(stop: threading.Event | None = None, interval_s: float = 1.0) -> None:
    settings = get_settings()
    stop = stop or threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    queue = get_queue(settings)
    log.info("dispatcher started")
    ticks = 0
    while not stop.is_set():
        try:
            n = dispatch_once(queue)
            ticks += 1
            if ticks % 30 == 0:
                with session_scope() as s:
                    reaped = jobs_repo.reap_exhausted(s, settings.queue_max_receive_count)
                if reaped:
                    log.warning("failed %d jobs that exhausted retries", reaped)
            if n:
                log.info("dispatched %d outbox rows", n)
        except Exception:
            log.exception("dispatcher iteration failed")
        stop.wait(interval_s)
