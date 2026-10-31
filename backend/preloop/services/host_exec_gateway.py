"""Execution attribution for host-profile model traffic through the gateway.

A private-runner host profile runs the runner user's own CLI. When that CLI
is onboarded through Preloop (Codex ``model_provider = "preloop"``, Claude
Code ``ANTHROPIC_BASE_URL`` on the gateway) its model calls already reach the
gateway with the agent's own key, so they are governed and metered for the
agent. What they lacked was the flow execution: the agent key carries no
``flow_execution_id``, so usage rows were not attributed to the run, the
run's ``agent_config.limits`` ceilings never saw them, and the result said
``gateway_metered: false``.

The fix keeps the agent key and adds an *execution marker*:

* At delivery the control plane mints a short-lived signed marker naming the
  account, flow and execution (:func:`mint_execution_marker`). It is not a
  credential: it authenticates nothing on its own, and the gateway only
  reads it next to a valid agent key of the same account.
* The runner sends it on every gateway request as ``X-Preloop-Execution``
  without editing the user's configuration: Codex through a per-run
  ``-c model_providers.preloop.env_http_headers...`` override that names an
  environment variable, Claude Code through ``ANTHROPIC_CUSTOM_HEADERS``.
* The gateway verifies the marker (:func:`attach_execution_marker`) and folds
  the execution id into the request's credential context, so the existing
  attribution, per-execution ceiling and cost rollup code paths apply.

At completion :func:`apply_host_gateway_metering` counts the gateway rows the
run produced and records the real ``gateway_metered`` value. A run that
produced model output with no gateway row bypassed Preloop and fails with
``gateway_bypassed`` unless the flow set the documented break-glass
``agent_config.allow_unmetered_host_model``; read-only review never allows
that opt-out.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Mapping, Optional, Tuple
from uuid import UUID

import jwt
from sqlalchemy.orm import Session

from preloop.config import settings

logger = logging.getLogger(__name__)

#: Delivery key carrying the marker to the runner (never persisted).
HOST_EXEC_GATEWAY_LEASE_KEY = "host_exec_gateway"

#: Request header the runner adds to every gateway call of the run.
EXECUTION_MARKER_HEADER = "X-Preloop-Execution"

#: Break-glass flag for harnesses that cannot route through Preloop.
ALLOW_UNMETERED_KEY = "allow_unmetered_host_model"

#: Context key recording that the execution came from a marker.
MARKER_CONTEXT_KEY = "execution_marker"

#: Harnesses whose model traffic can go through the Preloop gateway, and so
#: must. Copilot, Cursor and Devin use their vendor's model service.
GATEWAY_ROUTABLE_HARNESSES = frozenset({"codex_cli", "claude_code"})

#: Failure category and message prefix for a bypassed gateway.
GATEWAY_BYPASSED = "gateway_bypassed"

_AUDIENCE = "preloop:host-exec-gateway"
_MAX_MARKER_BYTES = 2048
_TTL = timedelta(hours=24, minutes=5)
_TERMINAL_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "FAILED",
        "STOPPED",
        "CANCELLED",
        "TIMEOUT",
        "TIMED_OUT",
        "ABORTED",
    }
)


class ExecutionMarkerError(ValueError):
    """The execution marker on a gateway request cannot be honoured."""


def mint_execution_marker(*, account_id: Any, flow_id: Any, execution_id: Any) -> str:
    """Sign the marker a host run sends with its gateway requests.

    Args:
        account_id: Account owning the execution.
        flow_id: Flow of the execution.
        execution_id: The flow execution the traffic belongs to.

    Returns:
        A compact HS256 token. It carries identifiers only.
    """
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "aud": _AUDIENCE,
            "iat": now,
            "exp": now + _TTL,
            "account_id": str(account_id),
            "flow_id": str(flow_id),
            "execution_id": str(execution_id),
        },
        settings.security.secret_key,
        algorithm="HS256",
    )


def decode_execution_marker(marker: str) -> Dict[str, UUID]:
    """Verify a marker's signature, audience, expiry and identifier claims.

    Args:
        marker: The ``X-Preloop-Execution`` header value.

    Returns:
        ``{"account_id", "flow_id", "execution_id"}`` as UUIDs.

    Raises:
        ExecutionMarkerError: When the marker is malformed, forged or expired.
    """
    if not marker or len(marker.encode("utf-8")) > _MAX_MARKER_BYTES:
        raise ExecutionMarkerError("invalid execution marker")
    try:
        claims = jwt.decode(
            marker,
            settings.security.secret_key,
            algorithms=["HS256"],
            audience=_AUDIENCE,
            options={"require": ["exp", "account_id", "flow_id", "execution_id"]},
        )
        parsed: Dict[str, UUID] = {}
        for key in ("account_id", "flow_id", "execution_id"):
            if not isinstance(claims[key], str):
                raise ValueError("invalid identity claim")
            parsed[key] = UUID(claims[key])
        return parsed
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        raise ExecutionMarkerError("invalid execution marker") from exc


def resolve_execution_marker(
    db: Session, *, account_id: Any, api_key_context: Mapping[str, Any], marker: str
) -> Dict[str, str]:
    """Check a marker against the authenticated key and the live execution.

    Args:
        db: Database session.
        account_id: Account of the authenticated gateway credential.
        api_key_context: ``context_data`` of the authenticated key.
        marker: The header value.

    Returns:
        The context entries to fold into the request (``flow_execution_id``,
        ``flow_id`` and the marker flag).

    Raises:
        ExecutionMarkerError: When the marker is invalid, names another
            account, conflicts with an execution already bound to the key,
            or names an execution that is gone or finished.
    """
    from preloop.models.crud import crud_flow_execution

    claims = decode_execution_marker(marker)
    if str(claims["account_id"]) != str(account_id):
        raise ExecutionMarkerError("execution marker does not match this credential")
    bound = api_key_context.get("flow_execution_id")
    if bound and str(bound) != str(claims["execution_id"]):
        raise ExecutionMarkerError("credential is already bound to another execution")
    execution = crud_flow_execution.get(
        db, id=claims["execution_id"], account_id=str(account_id)
    )
    if execution is None or str(getattr(execution, "flow_id", "")) != str(
        claims["flow_id"]
    ):
        raise ExecutionMarkerError("execution marker names an unknown execution")
    if str(getattr(execution, "status", "") or "").upper() in _TERMINAL_STATUSES:
        raise ExecutionMarkerError("this flow execution has finished")
    return {
        "flow_execution_id": str(claims["execution_id"]),
        "flow_id": str(claims["flow_id"]),
        MARKER_CONTEXT_KEY: "host_exec",
    }


def with_execution_context(auth_context: Any, entries: Mapping[str, str]) -> Any:
    """Return a copy of a detached gateway context carrying the execution.

    Only detached snapshots are rewritten: a live ORM key must never be
    mutated, because a later flush would persist the request's context.

    Args:
        auth_context: ``ModelGatewayAuthContext`` from authentication.
        entries: Output of :func:`resolve_execution_marker`.

    Returns:
        A new context whose key context includes ``entries``.

    Raises:
        ExecutionMarkerError: When the context is not a detached key snapshot.
    """
    from preloop.services.gateway_execution import GatewayApiKeySnapshot

    key = getattr(auth_context, "api_key", None)
    if not isinstance(key, GatewayApiKeySnapshot):
        raise ExecutionMarkerError("execution markers require an agent API key")
    context = {**key.context_data, **dict(entries)}
    snapshot = dataclasses.replace(key, context_json=json.dumps(context))
    return dataclasses.replace(auth_context, api_key=snapshot)


async def attach_execution_marker(
    auth_context: Any, marker: Optional[str], db: Session, *, provider: str
) -> Any:
    """Fold a verified execution marker into an authenticated gateway context.

    A request without the header is returned unchanged and keeps its agent
    attribution. A header that cannot be honoured is refused rather than
    ignored, so a run cannot silently fall out of its own ceilings.

    Args:
        auth_context: Authenticated, detached gateway context.
        marker: ``X-Preloop-Execution`` header value, if any.
        db: Request database session (only its bind is used).
        provider: Error rendering provider (``openai`` or ``anthropic``).

    Returns:
        The context to serve the request with.

    Raises:
        ModelGatewayAPIError: 403 when the marker cannot be honoured.
    """
    if marker is None or not marker.strip():
        return auth_context
    from preloop.api.loop_safety import run_db_off_loop
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    key = getattr(auth_context, "api_key", None)
    bind = db.get_bind()

    def resolve() -> Dict[str, str]:
        from preloop.models.db.gateway_session import release_gateway_session

        with Session(bind=bind, expire_on_commit=False) as session:
            entries = resolve_execution_marker(
                session,
                account_id=auth_context.account_id,
                api_key_context=getattr(key, "context_data", None) or {},
                marker=marker.strip(),
            )
            release_gateway_session(session)
            return entries

    try:
        if key is None:
            raise ExecutionMarkerError("execution markers require an agent API key")
        entries = await run_db_off_loop(resolve)
        return with_execution_context(auth_context, entries)
    except ExecutionMarkerError as exc:
        logger.warning("Gateway execution marker refused: %s", exc)
        raise ModelGatewayAPIError(
            provider=provider,  # type: ignore[arg-type]
            status_code=403,
            message=f"Preloop execution marker refused: {exc}",
            code="execution_marker_invalid",
            terminal=True,
        ) from exc


def host_gateway_delivery(execution: Any, flow: Any) -> Dict[str, str]:
    """The delivery entry that tells a runner how to mark its gateway traffic.

    Args:
        execution: The flow execution being delivered.
        flow: Its flow.

    Returns:
        ``{"header", "marker"}``.
    """
    return {
        "header": EXECUTION_MARKER_HEADER,
        "marker": mint_execution_marker(
            account_id=flow.account_id, flow_id=flow.id, execution_id=execution.id
        ),
    }


def _produced_model_output(status: str, result: Mapping[str, Any]) -> bool:
    usage = result.get("usage")
    if isinstance(usage, Mapping):
        for value in usage.values():
            if isinstance(value, (int, float)) and value > 0:
                return True
    return status == "SUCCEEDED"


def apply_host_gateway_metering(
    db: Session,
    *,
    status: str,
    error: Optional[str],
    result: Optional[Dict[str, Any]],
    pending_job: Optional[Mapping[str, Any]],
    agent_config: Optional[Mapping[str, Any]],
    execution_id: Any,
    request_count: Optional[int] = None,
) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """Record whether a host run went through the gateway, and enforce it.

    Args:
        db: Database session.
        status: Status after completion validation.
        error: Error after completion validation.
        result: Result after completion validation.
        pending_job: The persisted host lease.
        agent_config: The flow's ``agent_config``.
        execution_id: The execution.
        request_count: Gateway rows already counted by the caller (see
            :func:`count_gateway_requests_settled`); counted here when None.

    Returns:
        ``(status, error, result)``; the result always carries the
        server-computed ``gateway_metered`` and ``gateway_request_count``.
    """
    from preloop.services.runner_service import HOST_EXEC_LEASE_KEY
    from preloop.services.read_only_review import HOST_READ_ONLY_LEASE_KEY

    job = pending_job if isinstance(pending_job, Mapping) else {}
    if job.get("completion_protocol") != "host_exec" or result is None:
        return status, error, result
    count = (
        _gateway_request_count(db, execution_id)
        if request_count is None
        else request_count
    )
    result = {
        **result,
        "gateway_metered": count > 0,
        "gateway_request_count": count,
    }
    lease = job.get(HOST_EXEC_LEASE_KEY)
    harness = lease.get("harness") if isinstance(lease, Mapping) else None
    if count > 0 or harness not in GATEWAY_ROUTABLE_HARNESSES:
        return status, error, result
    if status not in {"SUCCEEDED", "FAILED"} or not _produced_model_output(
        status, result
    ):
        return status, error, result
    read_only = bool(job.get(HOST_READ_ONLY_LEASE_KEY))
    config = agent_config if isinstance(agent_config, Mapping) else {}
    if config.get(ALLOW_UNMETERED_KEY) is True and not read_only:
        result["gateway_bypass_allowed"] = True
        return status, error, result
    message = (
        f"{GATEWAY_BYPASSED}: the host CLI produced model output but made no "
        "request through the Preloop gateway, so this run was not governed or "
        "metered. Onboard the CLI through Preloop as the runner user (keep the "
        "onboarded configuration; do not point the run at a separate CLI home "
        "without the Preloop model provider)."
    )
    return "FAILED", message, {**result, "failure_category": GATEWAY_BYPASSED}


def _gateway_request_count(db: Session, execution_id: Any) -> int:
    from preloop.models.crud import crud_api_usage

    usage = crud_api_usage.get_gateway_usage_for_execution(db, str(execution_id))
    return int(usage.get("api_requests") or 0)


async def count_gateway_requests_settled(
    db: Session,
    *,
    execution_id: Any,
    pending_job: Optional[Mapping[str, Any]],
    attempts: int = 5,
    delay_seconds: float = 1.0,
) -> Optional[int]:
    """Count the execution's gateway rows, waiting briefly for a late row.

    A streamed response's usage row is written after its terminal frame, so
    a runner can report completion a moment before the row commits. For a
    host run that could route through the gateway, an empty count is
    re-read a few times before it is believed, so a metered run is not
    failed as ``gateway_bypassed``.

    Args:
        db: Database session (each read is a new statement).
        execution_id: The execution.
        pending_job: The persisted lease.
        attempts: Reads before giving up.
        delay_seconds: Pause between reads.

    Returns:
        The count, or None when the lease is not a host lease.
    """
    import asyncio

    from preloop.services.runner_service import HOST_EXEC_LEASE_KEY

    job = pending_job if isinstance(pending_job, Mapping) else {}
    if job.get("completion_protocol") != "host_exec":
        return None
    lease = job.get(HOST_EXEC_LEASE_KEY)
    harness = lease.get("harness") if isinstance(lease, Mapping) else None
    count = _gateway_request_count(db, execution_id)
    if harness not in GATEWAY_ROUTABLE_HARNESSES:
        return count
    for _ in range(max(attempts - 1, 0)):
        if count:
            break
        await asyncio.sleep(delay_seconds)
        db.expire_all()
        count = _gateway_request_count(db, execution_id)
    return count
