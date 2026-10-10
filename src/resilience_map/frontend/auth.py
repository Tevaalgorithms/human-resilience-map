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

_LANDING_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Human Resilience Map</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #edf5ff; color: #161616;
         font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
  main { width: min(560px, calc(100% - 32px)); background: #fff; border-radius: 8px; overflow: hidden;
         box-shadow: 0 8px 28px rgba(28, 42, 68, 0.18); }
  header { background: #1c2a44; color: #fff; padding: 24px 28px; }
  header h1 { margin: 0; font-size: 26px; }
  header p { margin: 4px 0 0; color: #a8c8ff; font-size: 15px; }
  section { padding: 28px; }
  section p { margin: 0 0 14px; color: #3d3d3d; }
  a.signin { display: block; text-align: center; background: #1c2a44; color: #fff; text-decoration: none;
             font-weight: 600; padding: 12px 16px; border-radius: 4px; margin-top: 22px; }
  a.signin:hover, a.signin:focus-visible { background: #2b3f66; }
  small { display: block; margin-top: 18px; color: #6f6f6f; font-size: 13px; }
</style></head>
<body><main>
<header><h1>Human Resilience Map</h1><p>Extreme-heat planning &amp; cooling access</p></header>
<section>
  <p>A planning application for exploring extreme-heat vulnerability and cooling-center access,
  combining versioned regional datasets with deterministic GIS and optimization tools.</p>
  <p>Access is limited to invited planners.</p>
  <a class="signin" href="/login">Sign in</a>
  <small>Planning pilot. Scores are historical planning indices, not medical predictions.</small>
</section>
</main></body></html>
"""


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
        if request.path == "/":
            # Signed-out visitors get a public landing page; the app itself stays behind login.
            return make_response(_LANDING_HTML, 200, {"Content-Type": "text/html; charset=utf-8"})
        if request.path.startswith("/_dash"):
            return make_response(jsonify(error="unauthenticated"), 401)
        return redirect("/login")

    # Dash registers its own first-request setup hook (which renders the layout and needs a
    # session) at app creation, ahead of ours. Run the login check first so signed-out visitors
    # get the landing page or a redirect instead of a 401 from that setup.
    hooks = server.before_request_funcs.setdefault(None, [])
    hooks.remove(require_login)
    hooks.insert(0, require_login)

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
