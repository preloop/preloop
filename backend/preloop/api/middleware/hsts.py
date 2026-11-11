"""Strict-Transport-Security for API and gateway responses (#2279).

The console nginx adds HSTS for traffic it fronts. Ingress routes that reach
the API or gateway directly bypass it, so this middleware adds the header on
responses to requests that arrived over HTTPS, unless the response already
carries one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

ONE_YEAR_SECONDS = 31536000
_TRUE = {"1", "true", "yes", "on"}
_HEADER = b"strict-transport-security"


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


@dataclass(frozen=True)
class HSTSSettings:
    """HSTS policy read from ``HSTS_*`` environment variables.

    Attributes:
        enabled: Master switch (``HSTS_ENABLED``, default true).
        max_age: Policy lifetime in seconds (``HSTS_MAX_AGE``).
        include_subdomains: Append ``includeSubDomains``.
        preload: Append ``preload``.
    """

    enabled: bool = True
    max_age: int = ONE_YEAR_SECONDS
    include_subdomains: bool = False
    preload: bool = False

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "HSTSSettings":
        """Build and validate settings from an environment mapping.

        Args:
            env: Mapping to read; defaults to ``os.environ``.

        Returns:
            The validated settings.

        Raises:
            ValueError: ``HSTS_MAX_AGE`` is not a non-negative integer, or
                ``HSTS_PRELOAD`` is set without ``HSTS_INCLUDE_SUBDOMAINS`` and a
                ``HSTS_MAX_AGE`` of at least one year.
        """
        env = os.environ if env is None else env
        raw_max_age = (env.get("HSTS_MAX_AGE") or "").strip() or str(ONE_YEAR_SECONDS)
        try:
            max_age = int(raw_max_age)
        except ValueError:
            raise ValueError(
                f"HSTS_MAX_AGE must be a whole number of seconds, got {raw_max_age!r}"
            ) from None
        if max_age < 0:
            raise ValueError(f"HSTS_MAX_AGE must not be negative, got {max_age}")
        settings = cls(
            enabled=_flag(env, "HSTS_ENABLED", True),
            max_age=max_age,
            include_subdomains=_flag(env, "HSTS_INCLUDE_SUBDOMAINS", False),
            preload=_flag(env, "HSTS_PRELOAD", False),
        )
        if settings.preload and (
            not settings.include_subdomains or settings.max_age < ONE_YEAR_SECONDS
        ):
            raise ValueError(
                "HSTS_PRELOAD=true requires HSTS_INCLUDE_SUBDOMAINS=true and "
                f"HSTS_MAX_AGE >= {ONE_YEAR_SECONDS} (got HSTS_INCLUDE_SUBDOMAINS="
                f"{str(settings.include_subdomains).lower()}, "
                f"HSTS_MAX_AGE={settings.max_age}); browsers reject such a "
                "preload policy"
            )
        return settings

    @property
    def header_value(self) -> str:
        """Return the ``Strict-Transport-Security`` header value."""
        value = f"max-age={self.max_age}"
        if self.include_subdomains:
            value += "; includeSubDomains"
        if self.preload:
            value += "; preload"
        return value


def _arrived_over_https(scope: Scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-proto":
            first: bytes = value.split(b",", 1)[0].strip().lower()
            return first == b"https"
    return False


class HSTSMiddleware:
    """Pure ASGI middleware adding ``Strict-Transport-Security`` on HTTPS."""

    def __init__(self, app: ASGIApp, settings: Optional[HSTSSettings] = None) -> None:
        """Wrap ``app``.

        Args:
            app: Downstream ASGI application.
            settings: HSTS policy; read from the environment when omitted.
        """
        self.app = app
        self.settings = settings if settings is not None else HSTSSettings.from_env()
        self._value = self.settings.header_value.encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        if (
            scope["type"] != "http"
            or not self.settings.enabled
            or not _arrived_over_https(scope)
        ):
            await self.app(scope, receive, send)
            return

        async def send_with_hsts(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(name.lower() == _HEADER for name, _ in headers):
                    headers.append((_HEADER, self._value))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_hsts)
