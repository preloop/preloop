"""Per-execution token, turn and USD ceilings (issue #840).

A flow bounds one execution by wall clock (``timeout_seconds``) and, for a
delegated child, by a USD ceiling on its subtree
(:mod:`preloop.services.flow_delegation_budget`). Neither bounds what a
single *parent* run may consume. An operator whose PR reviews range from
167k to 645k tokens asked for a hard ceiling on the run itself, because a
warm runner removes fixed cost but not variable cost.

Ceilings live under ``flow.agent_config["limits"]`` (a JSON column, so no
migration), with three optional keys::

    {
        "limits": {
            "max_total_tokens": 2000000,
            "max_usd": 25,
            "max_turns": 100
        }
    }

The gateway is the enforcement point: every model request for a flow
execution is attributed to that execution through the runtime API key's
``flow_execution_id`` (and to ``ApiUsage.flow_execution_id``), so before it
forwards a request the gateway can sum what the execution has already used
and refuse a request from a run that is already at or over its ceiling.

The deliberate asymmetry: only *already spent* usage is compared, never a
forecast. A request that would cross the ceiling is allowed once, so the last
response completes and the agent can still emit its verdict; the *next*
request is refused. Refusing the crossing request instead would kill a run
mid-sentence and waste every token already paid for. That also mirrors the
unpriced-model ruling in :mod:`preloop.services.model_gateway_budget_enforcer`
— a ceiling that cannot be measured is not a ceiling that was exceeded — so a
run whose gateway usage is entirely unpriced passes the USD check rather than
being refused on a number nobody can compute.

``max_turns`` is counted at the gateway as one turn per model request. No
runtime in this tree exposes a usable max-turns flag today, so the gateway
count is the enforcement; if a harness grows one, it can read the same value
from ``agent_config.limits`` and pass it through as an early stop.

Flows without ``limits`` behave exactly as before: the helper returns an
empty :class:`ExecutionLimits` and callers do no accounting.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_BUDGET_EXCEEDED,
)

logger = logging.getLogger(__name__)

#: Key under which the ceilings live inside ``agent_config``.
LIMITS_KEY = "limits"
#: Ceiling names, matching the issue's vocabulary and the schema.
MAX_TOTAL_TOKENS_KEY = "max_total_tokens"
MAX_USD_KEY = "max_usd"
MAX_TURNS_KEY = "max_turns"

#: Statuses at which an execution can spend nothing more. Mirrors the
#: terminal set used by the delegation budget's own accounting.
TERMINAL_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "FAILED",
        "STOPPED",
        "TIMEOUT",
        "TIMED_OUT",
        "ABORTED",
        "CANCELLED",
        "CANCELED",
    }
)

#: Rounding slack for the USD comparison; costs are stored as Numeric(10, 4).
_EPSILON = 1e-9


class ExecutionBudgetExceeded(Exception):
    """A run has reached one of its own per-execution ceilings.

    Carries the structured :class:`LimitViolation` that produced it so the
    gateway can render the same sentence in its client-specific error shape
    and the execution row can name the ceiling that fired.
    """

    #: Category written to ``flow_execution.failure_category``. Attached to
    #: the exception so any layer that classifies failures by exception type
    #: (see :func:`preloop.services.flow_failure_category.derive_failure_category`)
    #: agrees without parsing the message.
    category = FAILURE_CATEGORY_BUDGET_EXCEEDED

    def __init__(self, message: str, *, violation: "LimitViolation") -> None:
        super().__init__(message)
        self.message = message
        self.violation = violation


@dataclass(frozen=True)
class ExecutionLimits:
    """The ceilings configured on a flow's ``agent_config.limits``."""

    max_total_tokens: Optional[int] = None
    max_usd: Optional[float] = None
    max_turns: Optional[int] = None

    @property
    def is_empty(self) -> bool:
        """True when nothing bounds this execution."""
        return (
            self.max_total_tokens is None
            and self.max_usd is None
            and self.max_turns is None
        )

    def as_dict(self) -> dict[str, Any]:
        """Wire shape for API responses: only the ceilings that are set."""
        limits: dict[str, Any] = {}
        if self.max_total_tokens is not None:
            limits[MAX_TOTAL_TOKENS_KEY] = self.max_total_tokens
        if self.max_usd is not None:
            limits[MAX_USD_KEY] = self.max_usd
        if self.max_turns is not None:
            limits[MAX_TURNS_KEY] = self.max_turns
        return limits


@dataclass(frozen=True)
class ExecutionUsage:
    """What one execution has used so far, as the gateway measures it."""

    total_tokens: int = 0
    #: ``None`` means no attributable request could be priced, which is
    #: "unknown", not zero. A USD ceiling cannot be compared against it.
    cost_usd: Optional[float] = None
    #: One turn per gateway request; see the module docstring.
    turns: int = 0


@dataclass(frozen=True)
class LimitViolation:
    """One ceiling that a run has reached, and the numbers behind it."""

    #: ``max_total_tokens`` | ``max_usd`` | ``max_turns``
    kind: str
    limit: Any
    observed: Any
    message: str


def _positive_int(value: Any) -> Optional[int]:
    """Read a positive integer ceiling, or None. Never raises."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any) -> Optional[float]:
    """Read a positive finite float ceiling, or None. Never raises."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def parse_execution_limits(
    agent_config: Optional[Mapping[str, Any]]
) -> ExecutionLimits:
    """Extract the ceilings from an ``agent_config`` mapping, tolerantly.

    A malformed entry is ignored rather than raising: an execution must never
    fail to start because a ceiling was written as a string, and an operator
    who typed ``"2000000"`` still gets the ceiling they meant. The strict
    shape is enforced at the API boundary
    (:class:`preloop.models.schemas.flow.FlowExecutionLimits`); this reader is
    the tolerant second line for rows written before validation existed.

    Args:
        agent_config: The flow's ``agent_config`` JSON, possibly None.

    Returns:
        The parsed ceilings, with unset or invalid entries as ``None``.
    """
    if not isinstance(agent_config, Mapping):
        return ExecutionLimits()
    raw = agent_config.get(LIMITS_KEY)
    if not isinstance(raw, Mapping):
        return ExecutionLimits()
    return ExecutionLimits(
        max_total_tokens=_positive_int(raw.get(MAX_TOTAL_TOKENS_KEY)),
        max_usd=_positive_float(raw.get(MAX_USD_KEY)),
        max_turns=_positive_int(raw.get(MAX_TURNS_KEY)),
    )


def get_execution_usage(db: Session, execution_id: Any) -> ExecutionUsage:
    """Sum the gateway usage attributed to one execution.

    Reuses the same attribution rule the execution page and the cost rollup
    use (``crud_api_usage.get_gateway_usage_for_execution``), so the ceiling
    is compared against exactly the number the operator sees.

    Args:
        db: Database session.
        execution_id: The flow execution whose usage to sum.

    Returns:
        Token total, priced USD (``None`` when nothing was priced), and the
        request count that stands in for turns.
    """
    from preloop.models.crud import crud_api_usage

    usage = crud_api_usage.get_gateway_usage_for_execution(db, str(execution_id))
    token_usage = usage.get("token_usage") or {}
    raw_cost = usage.get("estimated_cost")
    return ExecutionUsage(
        total_tokens=int(token_usage.get("total_tokens") or 0),
        cost_usd=None if raw_cost is None else float(raw_cost),
        turns=int(usage.get("api_requests") or 0),
    )


def load_execution_limits(db: Session, execution: Any) -> ExecutionLimits:
    """Read the ceilings configured on the flow that owns ``execution``.

    Read live rather than snapshotted at launch: lowering a runaway flow's
    ceiling while it runs is exactly when an operator wants the change to
    take effect, and the value is a property of the flow definition.

    Args:
        db: Database session.
        execution: The flow execution (its ``flow`` relationship is used when
            already loaded, otherwise the flow is fetched).

    Returns:
        The parsed ceilings, empty when the flow has none.
    """
    flow = getattr(execution, "flow", None)
    if flow is None:
        flow_id = getattr(execution, "flow_id", None)
        if flow_id is None:
            return ExecutionLimits()
        from preloop.models.crud import crud_flow

        flow = crud_flow.get(db, id=flow_id)
    if flow is None:
        return ExecutionLimits()
    return parse_execution_limits(getattr(flow, "agent_config", None))


def evaluate_execution_limits(
    limits: ExecutionLimits, usage: ExecutionUsage
) -> Optional[LimitViolation]:
    """Return the first ceiling ``usage`` has already reached, or None.

    A ceiling is reached when usage has met or passed it (``>=``): at exactly
    the ceiling there is no room for another request, and the next one is the
    request that gets refused. Being under any ceiling allows the request, so
    the request that crosses a ceiling is served once and its response
    completes.

    USD is only compared when the spend is known. An unpriced run has
    ``cost_usd is None`` and passes, matching the unpriced-model ruling.

    Args:
        limits: The run's ceilings.
        usage: Usage measured before the pending request.

    Returns:
        The violation to report, or None when the request may proceed.
    """
    if (
        limits.max_total_tokens is not None
        and usage.total_tokens >= limits.max_total_tokens
    ):
        return LimitViolation(
            kind=MAX_TOTAL_TOKENS_KEY,
            limit=limits.max_total_tokens,
            observed=usage.total_tokens,
            message=(
                "execution token ceiling reached: "
                f"{usage.total_tokens} tokens used of "
                f"{limits.max_total_tokens} allowed"
            ),
        )
    if (
        limits.max_usd is not None
        and usage.cost_usd is not None
        and usage.cost_usd >= limits.max_usd - _EPSILON
    ):
        return LimitViolation(
            kind=MAX_USD_KEY,
            limit=limits.max_usd,
            observed=usage.cost_usd,
            message=(
                "execution USD ceiling reached: "
                f"${usage.cost_usd:.2f} spent of "
                f"${limits.max_usd:.2f} allowed"
            ),
        )
    if limits.max_turns is not None and usage.turns >= limits.max_turns:
        return LimitViolation(
            kind=MAX_TURNS_KEY,
            limit=limits.max_turns,
            observed=usage.turns,
            message=(
                "execution turn ceiling reached: "
                f"{usage.turns} turns used of "
                f"{limits.max_turns} allowed"
            ),
        )
    return None


def limit_violation_message(violation: LimitViolation) -> str:
    """The readable, agent-facing sentence for one violation.

    Prefixed uniformly so the failure classifier can recognise it structurally
    (``_EXECUTION_BUDGET_RE`` in :mod:`preloop.services.flow_failure_category`)
    even after an agent harness has wrapped it in its own provider-error text.
    """
    return f"Execution budget exceeded: {violation.message}"


def mark_execution_budget_exceeded(
    db: Session, execution: Any, message: str
) -> None:
    """Record a run refused for crossing its own ceiling as FAILED.

    The gateway sees the ceiling before the orchestrator does; writing the
    terminal row here means the block is visible immediately instead of only
    when the agent gives up and the monitor finalizes. A run already terminal
    is left alone: a success is never rewritten to a ceiling failure, and a
    failure already recorded by its owner keeps its own diagnosis.

    Never raises: failing to annotate the row must not change the refusal the
    caller is about to receive.

    Args:
        db: Database session.
        execution: The execution to mark.
        message: The readable ceiling message to store.
    """
    if execution is None:
        return
    if str(getattr(execution, "status", "") or "").upper() in TERMINAL_STATUSES:
        return
    try:
        from preloop.models.crud import crud_flow_execution
        from preloop.models.schemas.flow_execution import FlowExecutionUpdate

        update = FlowExecutionUpdate(
            status="FAILED",
            error_message=message,
            failure_category=FAILURE_CATEGORY_BUDGET_EXCEEDED,
            end_time=datetime.now(timezone.utc),
        )
        crud_flow_execution.update(db, db_obj=execution, obj_in=update)
    except Exception:  # noqa: BLE001 - annotation never fails the refusal
        logger.exception(
            "Failed to mark execution %s as budget_exceeded",
            getattr(execution, "id", None),
        )
        try:
            db.rollback()
        except Exception:  # pragma: no cover - rollback is best effort
            logger.debug("Rollback after budget annotation failure failed")


def enforce_execution_limits_for_id(db: Session, *, execution_id: Any) -> None:
    """Refuse when the execution named by a runtime token is over a ceiling.

    Entry point for the gateway: it holds only the ``flow_execution_id`` from
    the runtime API key's context. Executions without ceilings return without
    raising. A terminal row is still evaluated and still refused once its
    ceiling is spent — otherwise an agent retrying after the failure could
    keep spending — but it is never rewritten (see
    :func:`mark_execution_budget_exceeded`).

    Args:
        db: Database session.
        execution_id: The execution id from the credential context.

    Raises:
        ExecutionBudgetExceeded: The run has reached one of its ceilings. The
            execution row has been marked FAILED first when it was still
            running.
    """
    if not execution_id:
        return
    # The context is JSON, so defend against a non-UUID value: it cannot match
    # a row anyway, and a cast error here must never break gateway traffic.
    try:
        execution_uuid = uuid.UUID(str(execution_id))
    except (ValueError, TypeError, AttributeError):
        logger.debug("Ignoring non-UUID flow_execution_id %r", execution_id)
        return
    from preloop.models.crud import crud_flow_execution

    try:
        execution = crud_flow_execution.get(db, id=str(execution_uuid))
    except Exception:  # noqa: BLE001 - a limits read never breaks a request
        logger.warning(
            "Could not resolve execution %s for per-execution limits",
            execution_uuid,
            exc_info=True,
        )
        return
    if execution is None:
        return
    try:
        limits = load_execution_limits(db, execution)
        if limits.is_empty:
            return
        usage = get_execution_usage(db, execution.id)
        violation = evaluate_execution_limits(limits, usage)
    except Exception:  # noqa: BLE001 - a limits read never breaks a request
        logger.warning(
            "Could not evaluate per-execution limits for %s",
            execution_uuid,
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:  # pragma: no cover - rollback is best effort
            logger.debug("Rollback after limits read failure failed")
        return
    if violation is None:
        return
    message = limit_violation_message(violation)
    # A terminal row keeps being refused: once a ceiling failure is recorded,
    # an agent that keeps retrying must not be allowed to spend past it just
    # because the row is no longer RUNNING. mark_* is a no-op for terminal
    # rows, so a success is still never rewritten.
    mark_execution_budget_exceeded(db, execution, message)
    raise ExecutionBudgetExceeded(message, violation=violation)


def describe_execution_limits(
    db: Session, execution: Any
) -> tuple[ExecutionLimits, ExecutionUsage]:
    """Read a run's ceilings and its usage for a read API.

    Best-effort: a metrics read must render even when the accounting query
    cannot run, so every failure falls back to empty limits and zero usage.

    Args:
        db: Database session.
        execution: The execution to describe.

    Returns:
        ``(limits, usage)`` for the execution page.
    """
    try:
        limits = load_execution_limits(db, execution)
    except Exception:  # noqa: BLE001 - a read model, never fatal
        logger.debug(
            "Could not read limits for execution %s",
            getattr(execution, "id", None),
            exc_info=True,
        )
        return ExecutionLimits(), ExecutionUsage()
    if limits.is_empty:
        return limits, ExecutionUsage()
    try:
        usage = get_execution_usage(db, execution.id)
    except Exception:  # noqa: BLE001 - a read model, never fatal
        logger.debug(
            "Could not read usage for execution %s",
            getattr(execution, "id", None),
            exc_info=True,
        )
        usage = ExecutionUsage()
    return limits, usage