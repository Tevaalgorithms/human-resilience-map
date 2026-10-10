from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from resilience_map import __version__
from resilience_map.core.db import session_scope

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: process is up (used by the container/ALB health check)."""
    return {"status": "ok", "version": __version__}


@router.get("/ready")
def ready() -> dict[str, str]:
    """Readiness: database reachable and PostGIS available."""
    try:
        with session_scope() as s:
            s.execute(text("SELECT PostGIS_Version()")).scalar_one()
    except Exception as e:
        raise HTTPException(503, "database unavailable") from e
    return {"status": "ready"}
