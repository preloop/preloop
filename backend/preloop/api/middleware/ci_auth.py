"""Default-deny ASGI boundary for restricted machine credentials."""

import logging
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
from preloop.models.db.session import get_db_session
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


def _inspect(
    tokens: tuple[str, ...], action: CiAction | None
) -> tuple[CiTokenInspection, bool]:
    """Own a short-lived session on the worker, with all queries in CRUD."""
    sessions = get_db_session()
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
