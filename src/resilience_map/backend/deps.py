"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from resilience_map.core.db import session_scope


def db_session() -> Iterator[Session]:
    with session_scope() as s:
        yield s
