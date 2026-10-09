"""RFC 7662 grant gate with bounded, configuration-isolated hash caches.

Only the upstream bearer is submitted to the configured authorization server.
It is never retained in a cache or emitted to logs. Revocation is observable
within the configured TTL (at most five minutes for active grants).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from preloop.models.schemas.grant_introspection import IntrospectionConfig


@dataclass(frozen=True)
class GrantResult:
    """A safe grant binding and an optional hard-denial classification."""

    binding: dict[str, Any]
    deny_reason: str | None = None


@dataclass(frozen=True)
class _CacheEntry:
    binding: dict[str, Any]
    expires_at: float


def grant_denial_reason(
    binding: dict[str, Any], config: IntrospectionConfig, *, now: float
) -> str | None:
    """Apply the shared pure grant gate to real or synthetic introspection data."""
    if not binding["available"]:
        return None if config.fail_open else "introspection_unavailable"
    if not binding["active"] or (binding["exp"] is not None and binding["exp"] <= now):
        return "grant_inactive"
    if not set(config.required_scopes).issubset(binding["scope"]):
        return "scope_not_granted"
    return None


class GrantIntrospector:
    """Introspect static bearer grants using an injectable HTTP client factory."""

    def __init__(
        self,
        *,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        max_entries: int = 4096,
    ) -> None:
        self._client_factory = client_factory
        self._clock = clock
        self._monotonic = monotonic_clock
        self._max_entries = max_entries
        self._cache: OrderedDict[tuple[str, str], _CacheEntry] = OrderedDict()

    @staticmethod
    def _cache_key(
        token: str, config: IntrospectionConfig, server_id: str
    ) -> tuple[str, str]:
        # A token may be shared by servers with distinct issuers/client credentials.
        # Hash the complete context separately; neither token nor secret is retained.
        context = config.model_dump(mode="json", exclude={"client_secret"})
        context["client_secret_digest"] = hashlib.sha256(
            config.client_secret.get_secret_value().encode()
        ).hexdigest()
        context["server_id"] = server_id
        namespace = hashlib.sha256(
            json.dumps(context, sort_keys=True).encode()
        ).hexdigest()
        return namespace, hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _unavailable() -> dict[str, Any]:
        return {
            "available": False,
            "active": False,
            "scope": [],
            "sub": None,
            "client_id": None,
            "exp": None,
            "consent_ref": None,
            "cached": False,
        }

    @staticmethod
    def _parse(payload: Any, config: IntrospectionConfig) -> dict[str, Any]:
        if not isinstance(payload, dict) or type(payload.get("active")) is not bool:
            raise ValueError("invalid active claim")
        scope = payload.get("scope", "")
        if not isinstance(scope, str) or len(scope) > 65536:
            raise ValueError("invalid scope claim")
        scopes = list(dict.fromkeys(scope.split()))
        if len(scopes) > 128 or any(len(value) > 512 for value in scopes):
            raise ValueError("scope claim exceeds bounds")
        exp = payload.get("exp")
        if exp is not None and type(exp) is not int:
            raise ValueError("invalid expiry claim")
        binding = {
            "available": True,
            "active": payload["active"],
            "scope": scopes,
            "exp": exp,
            "cached": False,
        }
        for target, claim in [
            ("sub", "sub"),
            ("client_id", "client_id"),
            ("consent_ref", config.consent_ref_claim),
        ]:
            value = payload.get(claim)
            if value is not None and (not isinstance(value, str) or len(value) > 512):
                raise ValueError("invalid identity claim")
            binding[target] = value
        return binding

    async def evaluate(
        self,
        token: str | None,
        config: IntrospectionConfig,
        *,
        server_id: str,
    ) -> GrantResult:
        """Resolve a grant and enforce active/expiry/scopes before policy matching."""
        now = self._monotonic()
        key = self._cache_key(token, config, server_id) if token else None
        entry = self._cache.get(key) if key else None
        if key is not None and entry is not None and entry.expires_at > now:
            self._cache.move_to_end(key)
            binding = {
                **entry.binding,
                "scope": list(entry.binding["scope"]),
                "cached": True,
            }
        else:
            if key is not None:
                self._cache.pop(key, None)
            binding = self._unavailable()
            if token:
                data = {"token": token}
                auth: httpx.Auth = httpx.Auth()
                if config.client_auth == "basic":
                    auth = httpx.BasicAuth(
                        config.client_id, config.client_secret.get_secret_value()
                    )
                else:
                    data.update(
                        client_id=config.client_id,
                        client_secret=config.client_secret.get_secret_value(),
                    )
                try:
                    async with self._client_factory(
                        timeout=config.timeout_seconds,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client:
                        response = await client.post(
                            str(config.endpoint), data=data, auth=auth
                        )
                        response.raise_for_status()
                        if len(response.content) > 1_048_576:
                            raise ValueError("introspection response exceeds bounds")
                        payload = response.json()
                        binding = self._parse(payload, config)
                        # Compare decoded claims: JSON escaping must not hide
                        # quoted/backslash credentials. Check the original scope
                        # string before whitespace splitting can fragment a secret.
                        claims = [
                            payload.get("scope", ""),
                            binding["sub"],
                            binding["client_id"],
                            binding["consent_ref"],
                        ]
                        credentials = (token, config.client_secret.get_secret_value())
                        if any(
                            secret in claim
                            for claim in claims
                            if isinstance(claim, str)
                            for secret in credentials
                        ):
                            raise ValueError("authorization server echoed a credential")
                except (httpx.HTTPError, ValueError, TypeError):
                    # Do not log exception objects: HTTP clients include URLs and
                    # request/response bodies that can contain bearer credentials.
                    binding = self._unavailable()
            ttl = config.negative_cache_ttl_seconds
            if binding["available"] and binding["active"]:
                ttl = config.max_cache_ttl_seconds
                if binding["exp"] is not None:
                    ttl = min(ttl, max(0, binding["exp"] - self._clock()))
            if key is not None and ttl > 0:
                self._cache[key] = _CacheEntry(
                    {**binding, "scope": list(binding["scope"])},
                    self._monotonic() + ttl,
                )
                self._cache.move_to_end(key)
                while len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)

        return GrantResult(
            binding, grant_denial_reason(binding, config, now=self._clock())
        )


# The API uses one event loop per process; cache operations do not await.
grant_introspector = GrantIntrospector()
