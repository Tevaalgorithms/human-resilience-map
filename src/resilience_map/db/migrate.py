"""Minimal forward-only SQL migration runner (idempotent, advisory-locked)."""

from __future__ import annotations

import hashlib
import logging
from importlib import resources

import psycopg

from resilience_map.core.config import get_settings

log = logging.getLogger(__name__)
_LOCK_ID = 74_210_001


def _migrations() -> list[tuple[str, str]]:
    root = resources.files("resilience_map.db") / "migrations"
    files = sorted((p for p in root.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name)
    return [(p.name, p.read_text()) for p in files]


def run_migrations() -> list[str]:
    """Apply pending migrations; returns the names applied in this run."""
    applied_now: list[str] = []
    with psycopg.connect(get_settings().psycopg_dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            conn.execute("CREATE SCHEMA IF NOT EXISTS public")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
                "name text PRIMARY KEY, checksum text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done: dict[str, str] = dict(
                conn.execute("SELECT name, checksum FROM public.schema_migrations")
            )
            for name, sql in _migrations():
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if name in done:
                    if done[name] != checksum:
                        raise RuntimeError(f"Migration {name} was modified after being applied")
                    continue
                log.info("applying migration %s", name)
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute(
                        "INSERT INTO public.schema_migrations (name, checksum) VALUES (%s, %s)",
                        (name, checksum),
                    )
                applied_now.append(name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return applied_now
