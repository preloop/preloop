"""Default-deny ASGI boundary for restricted machine credentials."""

import hashlib
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Mapping
from types import MappingProxyType
from urllib.parse import parse_qsl

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse
from starlette.routing import compile_path
from starlette.types import ASGIApp, Receive, Scope, Send

from preloop.api.auth.ci_policy import (
    matched_machine_handler,
    validate_machine_policies,
)
from preloop.api.loop_safety import run_db_off_loop
from preloop.models import crud
from preloop.models.crud.ci_principal import CiTokenInspection
from preloop.models.db.session import get_auth_db_session
from preloop.schemas.ci_principal import CiAction

logger = logging.getLogger(__name__)

# Only typed, principal-owned execution operations opt into machine authority.
CI_ROUTE_POLICIES: Mapping[tuple[str, str], CiAction] = MappingProxyType(
    {
        ("POST", "/api/v1/event-webhooks/endpoints"): CiAction.CREATE_SUBSCRIPTION,
        ("GET", "/api/v1/event-webhooks/endpoints"): CiAction.READ_SUBSCRIPTION,
        (
            "PATCH",
            "/api/v1/event-webhooks/endpoints/{endpoint_id}",
        ): CiAction.UPDATE_SUBSCRIPTION,
        (
            "DELETE",
            "/api/v1/event-webhooks/endpoints/{endpoint_id}",
        ): CiAction.DELETE_SUBSCRIPTION,
        (
            "POST",
            "/api/v1/event-webhooks/endpoints/{endpoint_id}/secret/rotate",
        ): CiAction.ROTATE_SUBSCRIPTION_SECRET,
        ("POST", "/api/v1/flows/{flow_id}/trigger"): CiAction.TRIGGER,
        ("GET", "/api/v1/flows/executions"): CiAction.READ_EXECUTION,
        ("GET", "/api/v1/flows/executions/{execution_id}"): CiAction.READ_EXECUTION,
        ("GET", "/api/v1/flows/executions/{execution_id}/result"): CiAction.READ_RESULT,
        (
            "GET",
            "/api/v1/flows/executions/{execution_id}/tool-evidence",
        ): CiAction.READ_EVIDENCE,
        (
            "POST",
            "/api/v1/flows/executions/{execution_id}/command",
        ): CiAction.STOP_EXECUTION,
    }
)


def _credentials(scope: Scope) -> tuple[tuple[str, ...], bool]:
    """Read every supported legacy transport; never log or retain token values."""
    tokens: list[str] = []
    canonical = True
    bearer_count = 0
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            parts = value.decode("latin-1").split()
            if len(parts) == 2:
                tokens.append(parts[1])
                bearer_count += 1
                canonical = canonical and parts[0].lower() == "bearer"
            elif parts:
                tokens.extend(parts[1:] if len(parts) > 1 else parts)
                canonical = False
        elif name.lower() == b"x-api-key":
            tokens.append(value.decode("latin-1"))
            canonical = False
    for name, value in parse_qsl(
        scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True
    ):
        if name in {"token", "access_token", "api_key"}:
            tokens.append(value)
            canonical = False
    return tuple(tokens), canonical and bearer_count == 1 and len(tokens) == 1


def _env_seconds(name: str, default: float) -> float:
    """Read a non-negative duration in seconds, falling back on bad input."""
    try:
        return max(0.0, float(os.getenv(name, default)))
    except ValueError:
        return default


class _PassThroughCache:
    """Remember tokens recently verified as not being machine credentials.

    Only pass-through results are cached. They grant nothing: the request
    still meets the route's normal authentication, which reads the key fresh.
    Machine results are never cached, so revoking a CI key or bumping its
    credential version takes effect on the next request.

    Entries are fresh for ``ttl`` seconds; after that they are only served
    while the database is unreachable, for at most ``stale_ttl`` seconds, so
    a pool or database blip does not turn every agent call into a 503.
    Keys are SHA-256 digests; token values are never stored.
    """

    def __init__(self, ttl: float, stale_ttl: float, max_entries: int) -> None:
        self.ttl = ttl
        self.stale_ttl = max(stale_ttl, ttl)
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, CiTokenInspection]] = OrderedDict()

    @staticmethod
    def key(tokens: tuple[str, ...], action: CiAction | None) -> str:
        """Digest the credential set and action; never keep raw tokens."""
        digest = hashlib.sha256()
        for token in tokens:
            digest.update(token.encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
        digest.update(str(action).encode())
        return digest.hexdigest()

    def get(self, key: str, *, stale: bool = False) -> CiTokenInspection | None:
        """Return a cached pass-through result that is still usable."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, inspection = entry
        age = time.monotonic() - stored_at
        if age > self.stale_ttl:
            self._entries.pop(key, None)
            return None
        if age > self.ttl and not stale:
            return None
        return inspection

    def put(self, key: str, inspection: CiTokenInspection) -> None:
        """Store a pass-through result; anything recognized is ignored."""
        if inspection.recognized or self.ttl <= 0:
            return
        self._entries[key] = (time.monotonic(), inspection)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every entry (used by tests)."""
        self._entries.clear()


PASS_THROUGH_CACHE = _PassThroughCache(
    ttl=_env_seconds("CI_AUTH_PASS_THROUGH_TTL_SECONDS", 60.0),
    stale_ttl=_env_seconds("CI_AUTH_PASS_THROUGH_STALE_TTL_SECONDS", 600.0),
    max_entries=10_000,
)


def _inspect(
    tokens: tuple[str, ...], action: CiAction | None
) -> tuple[CiTokenInspection, bool]:
    """Own a short-lived session on the worker, with all queries in CRUD."""
    sessions = get_auth_db_session()
    db: Session = next(sessions)
    try:
        inspection = crud.crud_ci_principal.inspect_tokens(db, tokens=tokens)
        context = inspection.context
        if context is not None and action is not None:
            try:
                crud.crud_ci_principal.authorize(db, context=context, action=action)
            except PermissionError:
                return inspection, False
        return inspection, True
    finally:
        sessions.close()


class RestrictedCiAuthMiddleware:
    """Reject unclassified operations and custom transports before handler work.

    Human tokens continue through their existing authentication dependencies.
    A recognized machine marker never follows a human fallback, even when its
    key or grant is invalid. WebSockets are denied before their handshake.
    """

    def __init__(
        self,
        app: ASGIApp,
        policies: Mapping[tuple[str, str], CiAction] = CI_ROUTE_POLICIES,
    ) -> None:
        self.app = app
        validate_machine_policies(app, policies)
        self.policies = tuple(
            (method, compile_path(path)[0], action)
            for (method, path), action in dict(policies).items()
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        tokens, canonical = _credentials(scope)
        if not tokens:
            await self.app(scope, receive, send)
            return
        action = None
        if scope["type"] == "http" and canonical:
            for method, pattern, candidate in self.policies:
                if (
                    method == scope["method"]
                    and pattern.fullmatch(scope["path"])
                    and matched_machine_handler(scope, candidate)
                ):
                    action = candidate
                    break
        cache_key = PASS_THROUGH_CACHE.key(tokens, action)
        cached = PASS_THROUGH_CACHE.get(cache_key)
        if cached is not None:
            await self.app(scope, receive, send)
            return
        try:
            # Token syntax cannot prove a credential is human: legacy key rows
            # may carry machine markers even when their values contain periods.
            # Retain fresh indexed classification before any human fallback.
            inspection, permitted = await run_db_off_loop(
                lambda: _inspect(tokens, action)
            )
        except SQLAlchemyError:
            # SQLAlchemy exceptions can include plaintext legacy-lookup binds.
            # Never let credential lookup errors reach traceback logging.
            if PASS_THROUGH_CACHE.get(cache_key, stale=True) is not None:
                logger.warning(
                    "Credential verification unavailable; using a recent "
                    "pass-through result"
                )
                await self.app(scope, receive, send)
                return
            logger.warning("Credential verification unavailable")
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                await JSONResponse(
                    {"detail": "Credential verification unavailable"},
                    status_code=503,
                )(scope, receive, send)
            return
        context = inspection.context
        if not inspection.recognized:
            PASS_THROUGH_CACHE.put(cache_key, inspection)
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if not canonical or action is None or context is None or not permitted:
            status = 401 if context is None else 403
            # No raw path/query, tokens, or result contents in decision records.
            logger.info(
                "Restricted CI request denied",
                extra={
                    "ci_status": status,
                    "ci_principal_id": str(inspection.principal_id)
                    if inspection.principal_id
                    else None,
                    "ci_key_id": str(inspection.key_id) if inspection.key_id else None,
                    "ci_project_id": str(inspection.project_id)
                    if inspection.project_id
                    else None,
                    "ci_flow_id": str(inspection.flow_id)
                    if inspection.flow_id
                    else None,
                },
            )
            response = JSONResponse(
                {"detail": "Restricted CI authorization denied"}, status_code=status
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["ci_context"] = context
        scope["state"]["ci_action"] = action
        logger.info(
            "Restricted CI operation authorized",
            extra={
                "ci_principal_id": str(context.principal_id),
                "ci_key_id": str(context.key_id),
                "ci_action": action.value,
                "ci_project_id": str(context.project_id),
                "ci_flow_id": str(context.flow_id),
            },
        )
        await self.app(scope, receive, send)
