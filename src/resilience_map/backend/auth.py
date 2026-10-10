"""Authentication. ``local`` mode trusts an X-Dev-User header (development ONLY; startup guard in
config forbids it in staging/prod). ``cognito`` mode validates Cognito JWTs against the pool JWKS."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status
from jwt import PyJWKClient

from resilience_map.core.config import Settings, get_settings


@lru_cache
def _jwks(region: str, pool_id: str) -> PyJWKClient:
    return PyJWKClient(
        f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"
    )


def verify_cognito_token(token: str, s: Settings) -> str:
    issuer = f"https://cognito-idp.{s.cognito_region}.amazonaws.com/{s.cognito_user_pool_id}"
    try:
        key = _jwks(s.cognito_region, s.cognito_user_pool_id).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from e
    if claims.get("token_use") == "access":
        ok = claims.get("client_id") == s.cognito_client_id
    else:
        ok = claims.get("aud") == s.cognito_client_id
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token not issued for this client")
    return str(claims["sub"])


def current_user(
    authorization: Annotated[str | None, Header()] = None,
    x_dev_user: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> str:
    if settings.auth_mode == "local":
        return (x_dev_user or settings.local_dev_user).strip()[:64]
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return verify_cognito_token(authorization[7:], settings)
