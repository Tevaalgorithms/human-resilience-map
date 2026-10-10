"""PostgreSQL-backed LangGraph checkpointer, isolated in the ``checkpoints`` schema."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from resilience_map.core.config import get_settings


@contextmanager
def postgres_checkpointer() -> Iterator[PostgresSaver]:
    pool = ConnectionPool(
        get_settings().psycopg_dsn,
        min_size=1,
        max_size=4,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": "-c search_path=checkpoints",
        },
        open=True,
    )
    try:
        saver = PostgresSaver(pool)  # type: ignore[arg-type]
        saver.setup()  # idempotent; creates LangGraph tables in the checkpoints schema
        yield saver
    finally:
        pool.close()
