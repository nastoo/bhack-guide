"""OIDC authentication (PocketId-compatible) via authorization code flow + session cookies."""

from __future__ import annotations

import json
import logging
import os
from base64 import b64decode
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import itsdangerous
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "user"
SESSION_MAX_AGE_SEC = 14 * 24 * 3600
PUBLIC_PATHS = frozenset({"/health"})
PUBLIC_PREFIXES = ("/auth/", "/static/")

oauth = OAuth()


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    base_url: str
    session_secret: str
    scopes: str

    @property
    def discovery_url(self) -> str:
        issuer = self.issuer.rstrip("/")
        if issuer.endswith("/.well-known/openid-configuration"):
            return issuer
        return f"{issuer}/.well-known/openid-configuration"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ""


def load_auth_settings(settings: dict | None = None) -> AuthSettings:
    cfg = (settings or {}).get("auth") or {}
    enabled = _as_bool(_first_non_empty(os.environ.get("OIDC_ENABLED"), cfg.get("enabled", "false")))

    issuer = _first_non_empty(
        os.environ.get("OIDC_ISSUER"),
        os.environ.get("OIDC_DISCOVERY_URL"),
        cfg.get("issuer"),
    )
    client_id = _first_non_empty(os.environ.get("OIDC_CLIENT_ID"), cfg.get("client_id"))
    client_secret = _first_non_empty(os.environ.get("OIDC_CLIENT_SECRET"), cfg.get("client_secret"))
    scopes = _first_non_empty(os.environ.get("OIDC_SCOPES"), cfg.get("scopes"), "openid email profile")

    base_url = _first_non_empty(
        os.environ.get("OIDC_BASE_URL"),
        cfg.get("base_url"),
        _base_url_from_host(_first_non_empty(os.environ.get("TRAEFIK_HOST"), cfg.get("host"))),
        "http://localhost:8001",
    ).rstrip("/")

    redirect_uri = _first_non_empty(
        os.environ.get("OIDC_REDIRECT_URI"),
        cfg.get("redirect_uri"),
        f"{base_url}/auth/callback",
    )

    session_secret = _first_non_empty(
        os.environ.get("SESSION_SECRET"),
        os.environ.get("OIDC_SESSION_SECRET"),
        cfg.get("session_secret"),
    )

    return AuthSettings(
        enabled=enabled,
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        base_url=base_url,
        session_secret=session_secret,
        scopes=scopes,
    )


def _base_url_from_host(host: str) -> str:
    if not host:
        return ""
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    scheme = "http" if host.startswith("localhost") or host.startswith("127.0.0.1") else "https"
    return f"{scheme}://{host}"


def validate_auth_settings(auth: AuthSettings) -> list[str]:
    if not auth.enabled:
        return []
    missing: list[str] = []
    if not auth.issuer:
        missing.append("OIDC_ISSUER")
    if not auth.client_id:
        missing.append("OIDC_CLIENT_ID")
    if not auth.client_secret:
        missing.append("OIDC_CLIENT_SECRET")
    if not auth.session_secret:
        missing.append("SESSION_SECRET")
    return missing


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def current_user(request: Request) -> dict[str, Any] | None:
    user = request.session.get(SESSION_USER_KEY)
    return user if isinstance(user, dict) else None


def session_from_cookies(cookies: dict[str, str], secret_key: str) -> dict[str, Any]:
    raw = cookies.get("session")
    if not raw:
        return {}
    signer = itsdangerous.TimestampSigner(str(secret_key))
    try:
        payload = signer.unsign(raw.encode("utf-8"), max_age=SESSION_MAX_AGE_SEC)
        data = json.loads(b64decode(payload))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def user_from_websocket(websocket: WebSocket, auth: AuthSettings) -> dict[str, Any] | None:
    session = session_from_cookies(dict(websocket.cookies), auth.session_secret)
    user = session.get(SESSION_USER_KEY)
    return user if isinstance(user, dict) else None


def _register_oauth_client(auth: AuthSettings) -> None:
    oauth.register(
        name="oidc",
        client_id=auth.client_id,
        client_secret=auth.client_secret,
        server_metadata_url=auth.discovery_url,
        client_kwargs={
            "scope": auth.scopes,
            "code_challenge_method": "S256",
        },
    )


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, auth: AuthSettings):
        super().__init__(app)
        self.auth = auth

    async def dispatch(self, request: Request, call_next):
        if not self.auth.enabled or is_public_path(request.url.path):
            return await call_next(request)

        if current_user(request):
            return await call_next(request)

        if request.url.path.startswith("/api/") or request.url.path == "/ws":
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)

        next_path = quote(request.url.path)
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        return RedirectResponse(url=f"/auth/login?next={next_path}")


def _normalize_user(userinfo: dict[str, Any]) -> dict[str, Any]:
    return {
        "sub": userinfo.get("sub"),
        "email": userinfo.get("email"),
        "name": userinfo.get("name") or userinfo.get("preferred_username") or userinfo.get("email"),
        "preferred_username": userinfo.get("preferred_username"),
    }


def setup_auth(app: FastAPI, settings: dict | None = None) -> AuthSettings:
    auth = load_auth_settings(settings)

    @app.get("/auth/me")
    async def auth_me(request: Request):
        if not auth.enabled:
            return {"authenticated": True, "auth_disabled": True, "user": None}
        user = current_user(request)
        if not user:
            return JSONResponse({"authenticated": False}, status_code=401)
        return {"authenticated": True, "user": user}

    if not auth.enabled:
        logger.info("OIDC authentication disabled")
        return auth

    missing = validate_auth_settings(auth)
    if missing:
        logger.error(
            "OIDC is enabled but required settings are missing (%s); authentication disabled",
            ", ".join(missing),
        )
        return AuthSettings(
            enabled=False,
            issuer=auth.issuer,
            client_id=auth.client_id,
            client_secret=auth.client_secret,
            redirect_uri=auth.redirect_uri,
            base_url=auth.base_url,
            session_secret=auth.session_secret,
            scopes=auth.scopes,
        )

    _register_oauth_client(auth)
    # SessionMiddleware must wrap AuthMiddleware so request.session exists.
    app.add_middleware(AuthMiddleware, auth=auth)
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.session_secret,
        max_age=SESSION_MAX_AGE_SEC,
        same_site="lax",
        https_only=auth.base_url.startswith("https://"),
    )

    @app.get("/auth/login")
    async def auth_login(request: Request):
        next_path = request.query_params.get("next", "/")
        request.session["auth_next"] = next_path
        redirect_uri = auth.redirect_uri
        return await oauth.oidc.authorize_redirect(request, redirect_uri)

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        try:
            token = await oauth.oidc.authorize_access_token(request)
        except Exception as exc:
            logger.exception("OIDC callback failed")
            raise HTTPException(status_code=400, detail=f"Authentication failed: {exc}") from exc

        userinfo = token.get("userinfo")
        if not userinfo:
            userinfo = await oauth.oidc.userinfo(token=token)
        if not isinstance(userinfo, dict):
            raise HTTPException(status_code=400, detail="OIDC provider returned no user info")

        request.session[SESSION_USER_KEY] = _normalize_user(userinfo)
        next_path = request.session.pop("auth_next", "/") or "/"
        if not next_path.startswith("/"):
            next_path = "/"
        return RedirectResponse(url=next_path)

    @app.get("/auth/logout")
    async def auth_logout(request: Request):
        request.session.clear()
        metadata = oauth.oidc.load_server_metadata()
        end_session = metadata.get("end_session_endpoint")
        if end_session:
            params = f"post_logout_redirect_uri={quote(auth.base_url + '/', safe='')}"
            separator = "&" if "?" in end_session else "?"
            return RedirectResponse(url=f"{end_session}{separator}{params}")
        return RedirectResponse(url="/")

    logger.info(
        "OIDC authentication enabled (issuer=%s, redirect=%s)",
        auth.issuer,
        auth.redirect_uri,
    )
    return auth
