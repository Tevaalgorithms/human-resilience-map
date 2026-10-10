"""FastAPI application factory."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from resilience_map import __version__
from resilience_map.backend.routes import (
    analyses,
    assistant,
    datasets,
    health,
    imports,
    model,
    monitoring,
)
from resilience_map.core.config import get_settings
from resilience_map.core.errors import (
    ConflictError,
    ForbiddenError,
    HRMError,
    NotFoundError,
    ValidationFailed,
)
from resilience_map.core.logging import configure_logging, request_id_var

log = logging.getLogger(__name__)
_STATUS = {NotFoundError: 404, ForbiddenError: 403, ConflictError: 409, ValidationFailed: 422}


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Human Resilience Map API",
        version=__version__,
        docs_url="/docs" if settings.environment in {"local", "test", "dev"} else None,
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Dev-User"],
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HRMError)
    async def domain_error(_: Request, exc: HRMError) -> JSONResponse:
        code = next((c for t, c in _STATUS.items() if isinstance(exc, t)), 400)
        issues = getattr(exc, "issues", None)
        return JSONResponse(
            {"detail": str(exc), **({"issues": issues} if issues else {})}, status_code=code
        )

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    app.include_router(monitoring.router)
    app.include_router(health.router)
    app.include_router(model.router)
    app.include_router(assistant.router)
    app.include_router(datasets.router)
    app.include_router(imports.router)
    app.include_router(analyses.router)
    return app


app = create_app()
