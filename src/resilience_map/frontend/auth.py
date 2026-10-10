"""Frontend authentication and shared server-side sessions.

Local mode: a fixed dev user. Cognito mode: authorization-code flow; tokens live ONLY in the
PostgreSQL-backed session store (never in the browser), the cookie holds an opaque session id.
Sessions are shared across Dash replicas, so no state lives in container memory.
"""

from __future__ import annotations

import base64
import json
import secrets
from urllib.parse import urlencode

import httpx
from flask import Flask, abort, jsonify, make_response, redirect, request
from sqlalchemy import text
from werkzeug.wrappers import Response

from resilience_map.core.config import Settings
from resilience_map.core.db import session_scope

COOKIE = "hrm_sid"
PUBLIC_PATHS = ("/login", "/callback", "/logout", "/healthz", "/assets/", "/_favicon.ico")


def _create_session(data: dict[str, str], ttl: int) -> str:
    sid = secrets.token_urlsafe(32)
    with session_scope() as s:
        s.execute(text("DELETE FROM sessions.sessions WHERE expires_at < now()"))
        s.execute(
            text(
                "INSERT INTO sessions.sessions (id, data, expires_at) VALUES "
                "(:i, CAST(:d AS jsonb), now() + make_interval(secs => :t))"
            ),
            {"i": sid, "d": json.dumps(data), "t": ttl},
        )
    return sid


def _load_session(sid: str | None) -> dict[str, str] | None:
    if not sid:
        return None
    with session_scope() as s:
        row = s.execute(
            text("SELECT data FROM sessions.sessions WHERE id = :i AND expires_at > now()"),
            {"i": sid},
        ).first()
    return None if row is None else dict(row.data)


def _delete_session(sid: str | None) -> None:
    if sid:
        with session_scope() as s:
            s.execute(text("DELETE FROM sessions.sessions WHERE id = :i"), {"i": sid})


def api_auth_headers(settings: Settings) -> dict[str, str]:
    """Auth headers for the current request's user (called from Dash callbacks)."""
    if settings.auth_mode == "local":
        return {"X-Dev-User": settings.local_dev_user}
    data = _load_session(request.cookies.get(COOKIE))
    if not data:
        abort(401)
    return {"Authorization": f"Bearer {data['access_token']}"}


def install_auth(server: Flask, settings: Settings) -> None:
    @server.get("/healthz")
    def healthz() -> Response:
        return jsonify(status="ok")

    if settings.auth_mode == "local":
        return

    redirect_uri = f"{settings.public_base_url.rstrip('/')}/callback"
    secure = settings.public_base_url.startswith("https://")

    @server.before_request
    def require_login() -> Response | None:
        if request.path.startswith(PUBLIC_PATHS):
            return None
        if _load_session(request.cookies.get(COOKIE)):
            return None
        if request.path.startswith("/_dash"):
            return make_response(jsonify(error="unauthenticated"), 401)
        return redirect("/login")

    @server.get("/login")
    def login() -> Response:
        state = secrets.token_urlsafe(16)
        url = f"{settings.cognito_domain}/oauth2/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": settings.cognito_client_id,
                "redirect_uri": redirect_uri,
                "scope": "openid email",
                "state": state,
            }
        )
        resp = redirect(url)
        resp.set_cookie(
            "hrm_state", state, max_age=600, httponly=True, secure=secure, samesite="Lax"
        )
        return resp

    @server.get("/callback")
    def callback() -> Response:
        if not request.args.get("code") or request.args.get("state") != request.cookies.get(
            "hrm_state"
        ):
            abort(400, "Invalid login response")
        creds = base64.b64encode(
            f"{settings.cognito_client_id}:{settings.cognito_client_secret}".encode()
        ).decode()
        r = httpx.post(
            f"{settings.cognito_domain}/oauth2/token",
            timeout=10,
            data={
                "grant_type": "authorization_code",
                "client_id": settings.cognito_client_id,
                "code": request.args["code"],
                "redirect_uri": redirect_uri,
            },
            headers={"Authorization": f"Basic {creds}"} if settings.cognito_client_secret else {},
        )
        if r.status_code != 200:
            abort(401, "Login failed")
        tok = r.json()
        ttl = min(int(tok.get("expires_in", 3600)), settings.session_ttl_seconds)
        sid = _create_session({"access_token": tok["access_token"]}, ttl)
        resp = redirect("/")
        resp.delete_cookie("hrm_state")
        resp.set_cookie(COOKIE, sid, max_age=ttl, httponly=True, secure=secure, samesite="Lax")
        return resp

    @server.get("/logout")
    def logout() -> Response:
        _delete_session(request.cookies.get(COOKIE))
        resp = redirect(
            f"{settings.cognito_domain}/logout?"
            + urlencode(
                {"client_id": settings.cognito_client_id, "logout_uri": settings.public_base_url}
            )
        )
        resp.delete_cookie(COOKIE)
        return resp
