"""HTTP client for the FastAPI service. The frontend never touches analytical tables directly."""

from __future__ import annotations

from typing import Any

import httpx

from resilience_map.core.config import get_settings


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class ApiClient:
    def __init__(self, base_url: str | None = None, timeout: float = 15.0) -> None:
        self._http = httpx.Client(base_url=base_url or get_settings().api_base_url, timeout=timeout)

    def request(self, method: str, path: str, auth: dict[str, str], **kw: Any) -> Any:
        binary = kw.pop("binary", False)
        try:
            r = self._http.request(method, path, headers=auth | kw.pop("headers", {}), **kw)
        except httpx.HTTPError as e:
            raise ApiError(503, "The analysis service is unreachable. Try again shortly.") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(r.status_code, str(detail))
        if binary:
            return r.content
        ctype = r.headers.get("content-type", "")
        return r.json() if ctype.startswith("application/json") else r.text

    def get(self, path: str, auth: dict[str, str], **params: Any) -> Any:
        return self.request(
            "GET", path, auth, params={k: v for k, v in params.items() if v is not None}
        )

    def post(self, path: str, auth: dict[str, str], json: dict[str, Any], **kw: Any) -> Any:
        return self.request("POST", path, auth, json=json, **kw)
