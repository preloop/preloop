"""Microsoft Bot Connector authentication; no SDK or configurable trust bypass.

Protocol: https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt

from preloop.services.chat_providers import ChatProviderConfig, ChatProviderError

OPENID_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
KEYS_URL = "https://login.botframework.com/v1/.well-known/keys"
ISSUER = "https://api.botframework.com"
KEY_TTL = 12 * 3600
REFETCH_INTERVAL = 60
MAX_RESPONSE_BYTES = 131_072
_keys: list[dict[str, Any]] = []
_keys_until = 0.0
_refetch_after = 0.0
_key_lock = asyncio.Lock()
_tokens: dict[tuple[str, str, str], tuple[float, str]] = {}


def service_url_allowed(value: str) -> bool:
    """Allow only configured HTTPS Connector hosts, including on stored sends."""
    try:
        parsed = urlsplit(value)
        hosts = {
            host.strip().lower()
            for host in os.getenv(
                "CHAT_TEAMS_SERVICE_HOSTS", "smba.trafficmanager.net"
            ).split(",")
            if host.strip()
        }
        return bool(
            parsed.scheme == "https"
            and parsed.hostname in hosts
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and "\\" not in value
            and not any(ord(c) < 33 for c in value)
            and len(value) <= 2048
        )
    except (ValueError, TypeError):
        return False


async def request_json(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> tuple[int, httpx.Headers, dict[str, Any]]:
    """Bound decoded responses and total time; never follow a token redirect."""
    async with (
        asyncio.timeout(15),
        client.stream(
            method, url, timeout=12, follow_redirects=False, **kwargs
        ) as response,
    ):
        raw = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ChatProviderError("Invalid Teams response")
        # Error bodies can contain secrets or user text. Never parse or retain.
        if not 200 <= response.status_code < 300:
            return response.status_code, response.headers, {}
        import json

        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChatProviderError("Invalid Teams response") from exc
        if not isinstance(data, dict):
            raise ChatProviderError("Invalid Teams response")
        return response.status_code, response.headers, data


def token_kid(token: str) -> str:
    """Read only the bounded key identifier; never trust token-supplied URLs."""
    try:
        if len(token) > 16384:
            raise ValueError()
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not 0 < len(kid) <= 256
        ):
            raise ValueError()
        return kid
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise ChatProviderError("Invalid Teams token") from exc


async def signing_keys(token: str) -> list[dict[str, Any]]:
    """Cache fixed-origin keys with one globally rate-limited unknown-kid refresh."""
    global _keys, _keys_until, _refetch_after
    kid = token_kid(token)
    async with _key_lock:
        now = time.monotonic()
        known = any(key.get("kid") == kid for key in _keys)
        if now < _keys_until and known:
            return _keys
        if now < _refetch_after:
            if now >= _keys_until:
                raise ChatProviderError("Teams signing keys unavailable")
            return _keys
        _refetch_after = now + REFETCH_INTERVAL
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False
            ) as client:
                status, _, metadata = await request_json(client, "GET", OPENID_URL)
                if status != 200 or metadata.get("jwks_uri") != KEYS_URL:
                    raise ValueError()
                status, _, body = await request_json(client, "GET", KEYS_URL)
                keys = body.get("keys")
                if (
                    status != 200
                    or not isinstance(keys, list)
                    or not 0 < len(keys) <= 64
                ):
                    raise ValueError()
                if any(not isinstance(key, dict) for key in keys):
                    raise ValueError()
                _keys = keys
                _keys_until = time.monotonic() + KEY_TTL
                return _keys
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            raise ChatProviderError("Teams signing keys unavailable") from exc


def verify_activity_token(
    token: str,
    keys: list[dict[str, Any]],
    app_id: str,
    service_url: str,
    *,
    now: float | None = None,
) -> None:
    """Pin signature, issuer, audience, validity, channel and service URL."""
    try:
        kid = token_kid(token)
        candidates = [key for key in keys if key.get("kid") == kid]
        if len(candidates) != 1:
            raise ValueError()
        key = candidates[0]
        if (
            key.get("kty") != "RSA"
            or key.get("alg", "RS256") != "RS256"
            or key.get("use", "sig") != "sig"
            or not isinstance(key.get("endorsements"), list)
            or "msteams" not in key["endorsements"]
        ):
            raise ValueError()
        claims = jwt.decode(
            token,
            jwt.algorithms.RSAAlgorithm.from_jwk(key),
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=app_id,
            options={
                "require": ["exp", "nbf", "iss", "aud"],
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
        current = time.time() if now is None else now
        for field in ("exp", "nbf"):
            if type(claims[field]) not in (int, float) or not math.isfinite(
                claims[field]
            ):
                raise ValueError()
        if claims["exp"] <= current - 300 or claims["nbf"] > current + 300:
            raise ValueError()
        if not service_url or claims.get("serviceUrl") != service_url:
            raise ValueError()
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        raise ChatProviderError("Invalid Teams token") from exc


async def bot_token(config: ChatProviderConfig, client: httpx.AsyncClient) -> str:
    """Acquire a single-tenant token; cache is bounded and secret-rotation aware."""
    try:
        tenant = str(UUID(config.teams_app_tenant_id))
        app_id = str(UUID(config.bot_user_id))
        if config.workspace_id and config.workspace_id != tenant:
            raise ValueError()
        if not config.bot_token:
            raise ValueError()
        cache_key = (
            tenant,
            app_id,
            hashlib.sha256(config.bot_token.encode()).hexdigest(),
        )
        cached = _tokens.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        status, _, data = await request_json(
            client,
            "POST",
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": config.bot_token,
                "scope": "https://api.botframework.com/.default",
            },
        )
        token = data.get("access_token")
        lifetime = data.get("expires_in")
        if status != 200 or not isinstance(token, str) or not 0 < len(token) <= 16384:
            raise ValueError()
        if (
            type(lifetime) not in (int, float)
            or not math.isfinite(lifetime)
            or lifetime <= 60
        ):
            raise ValueError()
        if len(_tokens) >= 128:
            _tokens.pop(next(iter(_tokens)))
        _tokens[cache_key] = (time.monotonic() + min(lifetime, 3600) - 60, token)
        return token
    except (httpx.HTTPError, ValueError, TypeError, TimeoutError) as exc:
        raise ChatProviderError(
            "Teams authentication failed; check application ID, tenant ID and client secret"
        ) from exc
