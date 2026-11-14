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
            "max_total_tokens": 20000000,
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

Default ceilings. There is no built-in default: a flow that sets no ceiling
runs without one, unless the operator sets ``PRELOOP_DEFAULT_MAX_TOTAL_TOKENS``,
``PRELOOP_DEFAULT_MAX_TURNS`` or ``PRELOOP_DEFAULT_MAX_USD``, which then apply
to every flow that does not set that ceiling itself (``0`` or unset means no
default). A value on the flow wins per key. A run stopped by such a default fails
with ``default_limit_reached`` rather than ``budget_exceeded``, and its message
names the knob that changes it, so a review that ran out of a ceiling nobody
chose reads differently from one that hit the flow's own.

Repeated identical upstream failures (#1336). The same preflight also stops a
run whose model calls keep failing the same way: when the execution's newest
gateway attempts are N failures in a row with the same request fingerprint,
error class and status, the next request is refused and the run ends FAILED
with ``repeated_upstream_failure``. N is
``agent_config.limits.max_consecutive_identical_upstream_failures`` (default 3;
a malformed value uses the default rather than disabling the breaker), and a
terminal error class (one that can never succeed on retry) trips on its first
repeat. A success, or any failure that differs in fingerprint, class or status,
ends the streak. Rate limiting and overload are not counted: they clear with
time, and the gateway already backs off on them.
"""

from __future__ import annotations

import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_BUDGET_EXCEEDED,
    FAILURE_CATEGORY_DEFAULT_LIMIT_REACHED,
    FAILURE_CATEGORY_REPEATED_UPSTREAM_FAILURE,
)
from preloop.services.upstream_errors import (
    ERROR_CLASS_CLIENT_CANCELLED,
    ERROR_CLASS_NETWORK,
    ERROR_CLASS_UPSTREAM_AUTH,
    ERROR_CLASS_UPSTREAM_DISCONNECT,
    ERROR_CLASS_UPSTREAM_ERROR,
    ERROR_CLASS_UPSTREAM_OVERLOADED,
    ERROR_CLASS_UPSTREAM_PROTOCOL,
    ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
    ERROR_CLASS_UPSTREAM_RATE_LIMITED,
    is_terminal_error_class,
)

logger = logging.getLogger(__name__)

#: Key under which the ceilings live inside ``agent_config``.
LIMITS_KEY = "limits"
#: Ceiling names, matching the issue's vocabulary and the schema.
MAX_TOTAL_TOKENS_KEY = "max_total_tokens"
MAX_USD_KEY = "max_usd"
MAX_TURNS_KEY = "max_turns"

#: Environment-provided default ceilings, per ceiling. There is no built-in
#: default: a flow without ``agent_config.limits`` runs unbounded unless the
#: operator sets one of these. Unset, blank, ``0`` or unparsable means no
#: default for that ceiling.
DEFAULT_LIMIT_ENV_VARS: dict[str, str] = {
    MAX_TOTAL_TOKENS_KEY: "PRELOOP_DEFAULT_MAX_TOTAL_TOKENS",
    MAX_TURNS_KEY: "PRELOOP_DEFAULT_MAX_TURNS",
    MAX_USD_KEY: "PRELOOP_DEFAULT_MAX_USD",
}
#: Where an effective ceiling came from.
LIMIT_SOURCE_FLOW = "flow"
LIMIT_SOURCE_ENVIRONMENT = "environment"
#: Legacy source of the removed built-in defaults; never produced now.
LIMIT_SOURCE_DEFAULT = "default"

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


class ExecutionBudgetExceededError(Exception):
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
        self.category = violation.category


@dataclass(frozen=True)
class ExecutionLimits:
    """The ceilings that bound one execution.

    ``sources`` names where each set ceiling came from: ``flow`` (the flow's
    own ``agent_config.limits``), ``environment`` (a ``PRELOOP_DEFAULT_*``
    default). ``default`` is kept for compatibility and no longer produced:
    there is no built-in value. A reader of explicit flow
    limits (:func:`parse_execution_limits`) leaves it empty.
    """

    max_total_tokens: Optional[int] = None
    max_usd: Optional[float] = None
    max_turns: Optional[int] = None
    sources: dict[str, str] = field(default_factory=dict, compare=False)

    def source_of(self, kind: str) -> str:
        """Where the ceiling ``kind`` came from; ``flow`` when not recorded."""
        return self.sources.get(kind, LIMIT_SOURCE_FLOW)

    def describe(self) -> dict[str, dict[str, Any]]:
        """Wire shape for the console: ``{kind: {"value", "source"}}``."""
        return {
            kind: {"value": value, "source": self.source_of(kind)}
            for kind, value in self.as_dict().items()
        }

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
    #: ``flow`` | ``environment`` | ``default``; see :class:`ExecutionLimits`.
    source: str = LIMIT_SOURCE_FLOW

    @property
    def is_default(self) -> bool:
        """True when the ceiling was not chosen on the flow itself."""
        return self.source != LIMIT_SOURCE_FLOW

    @property
    def category(self) -> str:
        """Failure category this violation ends the execution with."""
        if self.is_default:
            return FAILURE_CATEGORY_DEFAULT_LIMIT_REACHED
        return FAILURE_CATEGORY_BUDGET_EXCEEDED


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
    if parsed <= 0 or math.isnan(parsed) or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def parse_execution_limits(
    agent_config: Optional[Mapping[str, Any]],
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


def _env_default(kind: str) -> tuple[Optional[Any], str]:
    """One default ceiling and its source, honouring the environment override.

    Read on every call so a self-hosted operator's change applies on restart
    and tests can set it with ``monkeypatch.setenv``.

    Args:
        kind: ``max_total_tokens`` | ``max_turns`` | ``max_usd``.

    Returns:
        ``(value, source)``; ``value`` is ``None`` when the environment
        disables this default with ``0``.
    """
    name = DEFAULT_LIMIT_ENV_VARS[kind]
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None, LIMIT_SOURCE_ENVIRONMENT
    try:
        number = float(raw.strip())
    except ValueError:
        logger.warning("Ignoring %s=%r: not a number", name, raw)
        return None, LIMIT_SOURCE_ENVIRONMENT
    if not math.isfinite(number):
        logger.warning("Ignoring %s=%r: not a finite ceiling", name, raw)
        return None, LIMIT_SOURCE_ENVIRONMENT
    if number == 0:
        return None, LIMIT_SOURCE_ENVIRONMENT
    parsed = (
        _positive_float(number)
        if kind == MAX_USD_KEY
        else (_positive_int(number) if number == int(number) else None)
    )
    if parsed is None:
        logger.warning("Ignoring %s=%r: not a positive ceiling", name, raw)
        return None, LIMIT_SOURCE_ENVIRONMENT
    return parsed, LIMIT_SOURCE_ENVIRONMENT


def resolve_execution_limits(
    agent_config: Optional[Mapping[str, Any]],
) -> ExecutionLimits:
    """The ceilings that actually bound an execution of this flow.

    Per ceiling: the flow's own value wins; otherwise the instance default
    (``PRELOOP_DEFAULT_*``, where ``0`` means none); otherwise no ceiling.
    Subscription runs priced at $0 are still bounded by tokens and
    turns, because those are compared independently of cost.

    Args:
        agent_config: The flow's ``agent_config`` JSON, possibly None.

    Returns:
        The effective ceilings with ``sources`` filled in for each one set.
    """
    explicit = parse_execution_limits(agent_config)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for kind in (MAX_TOTAL_TOKENS_KEY, MAX_USD_KEY, MAX_TURNS_KEY):
        value = getattr(explicit, kind)
        source = LIMIT_SOURCE_FLOW
        if value is None:
            value, source = _env_default(kind)
        values[kind] = value
        if value is not None:
            sources[kind] = source
    return ExecutionLimits(**values, sources=sources)


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
    take effect, and the value is a property of the flow definition. The
    defaults apply to every ceiling the flow leaves unset (see
    :func:`resolve_execution_limits`).

    Args:
        db: Database session.
        execution: The flow execution (its ``flow`` relationship is used when
            already loaded, otherwise the flow is fetched).

    Returns:
        The effective ceilings, defaults included.
    """
    flow = getattr(execution, "flow", None)
    if flow is None:
        flow_id = getattr(execution, "flow_id", None)
        if flow_id is None:
            return resolve_execution_limits(None)
        from preloop.models.crud import crud_flow

        flow = crud_flow.get(db, id=flow_id)
    if flow is None:
        return resolve_execution_limits(None)
    return resolve_execution_limits(getattr(flow, "agent_config", None))


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
        return _violation(
            limits,
            MAX_TOTAL_TOKENS_KEY,
            limit=limits.max_total_tokens,
            observed=usage.total_tokens,
            noun="token",
            used=(
                f"{usage.total_tokens} tokens used of {limits.max_total_tokens} allowed"
            ),
        )
    if (
        limits.max_usd is not None
        and usage.cost_usd is not None
        and usage.cost_usd >= limits.max_usd - _EPSILON
    ):
        return _violation(
            limits,
            MAX_USD_KEY,
            limit=limits.max_usd,
            observed=usage.cost_usd,
            noun="USD",
            used=f"${usage.cost_usd:.2f} spent of ${limits.max_usd:.2f} allowed",
        )
    if limits.max_turns is not None and usage.turns >= limits.max_turns:
        return _violation(
            limits,
            MAX_TURNS_KEY,
            limit=limits.max_turns,
            observed=usage.turns,
            noun="turn",
            used=f"{usage.turns} turns used of {limits.max_turns} allowed",
        )
    return None


def _format_limit(value: Any) -> str:
    """Render a ceiling the way an operator would type it back."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _violation(
    limits: ExecutionLimits,
    kind: str,
    *,
    limit: Any,
    observed: Any,
    noun: str,
    used: str,
) -> LimitViolation:
    """Build one violation, naming the knob when a default fired.

    A flow's own ceiling reads as before. An instance default names its
    environment variable and the flow key that overrides it.
    """
    source = limits.source_of(kind)
    if source == LIMIT_SOURCE_FLOW:
        message = f"execution {noun} ceiling reached: {used}"
    else:
        # The only non-flow source: there is no built-in default any more.
        env_name = DEFAULT_LIMIT_ENV_VARS[kind]
        origin = f"instance default {env_name}={_format_limit(limit)}"
        change = f"set agent_config.limits.{kind} on the flow to override it"
        message = (
            f"default execution {noun} ceiling reached: {used} ({origin}; {change})"
        )
    return LimitViolation(
        kind=kind, limit=limit, observed=observed, message=message, source=source
    )


def limit_violation_message(violation: LimitViolation) -> str:
    """The readable, agent-facing sentence for one violation.

    Prefixed uniformly so the failure classifier can recognise it structurally
    (``_EXECUTION_BUDGET_RE`` in :mod:`preloop.services.flow_failure_category`)
    even after an agent harness has wrapped it in its own provider-error text.
    """
    return f"Execution budget exceeded: {violation.message}"


def mark_execution_budget_exceeded(
    db: Session,
    execution: Any,
    message: str,
    *,
    category: str = FAILURE_CATEGORY_BUDGET_EXCEEDED,
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
        category: ``budget_exceeded`` for the flow's own ceiling,
            ``default_limit_reached`` for a default one.
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
            failure_category=category,
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
        ExecutionBudgetExceededError: The run has reached one of its ceilings. The
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
    mark_execution_budget_exceeded(db, execution, message, category=violation.category)
    raise ExecutionBudgetExceededError(message, violation=violation)


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


# --- repeated identical upstream failures (#1336) ---------------------------

#: Key inside ``agent_config.limits`` for the breaker threshold.
MAX_CONSECUTIVE_IDENTICAL_FAILURES_KEY = "max_consecutive_identical_upstream_failures"
#: Identical failures in a row that stop a run when the flow sets nothing.
DEFAULT_MAX_CONSECUTIVE_IDENTICAL_FAILURES = 3
#: A terminal error class stops the run on its first repeat.
TERMINAL_CLASS_FAILURE_THRESHOLD = 2
#: Milestone name written to the execution timeline when the breaker trips.
REPEATED_UPSTREAM_FAILURE_MILESTONE = "repeated_upstream_failure"
#: Failures that clear with time or come from the client, never counted.
_UNCOUNTED_ERROR_CLASSES = frozenset(
    {
        ERROR_CLASS_UPSTREAM_RATE_LIMITED,
        ERROR_CLASS_UPSTREAM_OVERLOADED,
        ERROR_CLASS_CLIENT_CANCELLED,
        "stream_abandoned",
        "kill_switch",
        "budget_exceeded",
        "hosted_credit_reserved",
    }
)
#: Recorded outcomes that are not upstream failures at all; never a cause.
_NOT_UPSTREAM_ERROR_CLASSES = _UNCOUNTED_ERROR_CLASSES - {
    ERROR_CLASS_UPSTREAM_RATE_LIMITED,
    ERROR_CLASS_UPSTREAM_OVERLOADED,
}


@dataclass(frozen=True)
class GatewayAttemptOutcome:
    """One recorded gateway attempt, reduced to what the breaker compares."""

    status_code: int
    error_class: Optional[str]
    request_fingerprint: Optional[str]

    @property
    def counts_as_failure(self) -> bool:
        """True for an upstream failure the breaker may count."""
        return (
            int(self.status_code or 0) >= 400
            and bool(self.error_class)
            and self.error_class not in _UNCOUNTED_ERROR_CLASSES
            and bool(self.request_fingerprint)
        )

    @property
    def signature(self) -> tuple[Optional[str], int, Optional[str]]:
        """What makes two failures identical."""
        return (self.error_class, int(self.status_code), self.request_fingerprint)


@dataclass(frozen=True)
class FailureStreak:
    """A run of identical failures long enough to stop the execution."""

    error_class: str
    status_code: int
    request_fingerprint: str
    count: int
    threshold: int


#: Classes the upstream provider decided; anything else is Preloop's own.
_UPSTREAM_ERROR_CLASSES = frozenset(
    {
        ERROR_CLASS_NETWORK,
        ERROR_CLASS_UPSTREAM_AUTH,
        ERROR_CLASS_UPSTREAM_DISCONNECT,
        ERROR_CLASS_UPSTREAM_ERROR,
        ERROR_CLASS_UPSTREAM_OVERLOADED,
        ERROR_CLASS_UPSTREAM_PROTOCOL,
        ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
        ERROR_CLASS_UPSTREAM_RATE_LIMITED,
    }
)
#: Message prefixes of ``upstream_origin_message`` (model_gateway_errors).
_UPSTREAM_LABEL_PREFIXES = ("Upstream provider ", "The upstream provider ")
#: Cap on the first cause's provider text carried into the stop summary.
_MAX_FIRST_CAUSE_DETAIL_CHARS = 600


@dataclass(frozen=True)
class FirstFailureCause:
    """The failure that started a run of failures (#2288).

    The breaker trips on the trailing streak; this is what the run's first
    failing call reported, so the stop summary never replaces the original
    cause with a later error.
    """

    status_code: int
    error_class: Optional[str]
    provider: Optional[str]
    detail: Optional[str]
    request_id: Optional[str]

    @property
    def origin(self) -> Optional[str]:
        """Who decided the failure, or ``None`` when the row cannot tell.

        Provider-side classes are ``upstream`` and any other class is
        Preloop's own. A provider 4xx such as a routing 404 is recorded
        without a class; it is ``upstream`` when its message carries the
        gateway's upstream-origin label.
        """
        if self.error_class:
            if self.error_class in _UPSTREAM_ERROR_CLASSES:
                return "upstream"
            return "gateway"
        if self.detail and self.detail.startswith(_UPSTREAM_LABEL_PREFIXES):
            return "upstream"
        return None

    def to_dict(self) -> dict[str, Any]:
        """The ``cause`` object for the 424 body and the timeline."""
        cause: dict[str, Any] = {"status": self.status_code}
        origin = self.origin
        if origin:
            cause["origin"] = origin
        if self.error_class:
            cause["category"] = self.error_class
        if origin == "upstream":
            cause["upstream_status"] = self.status_code
        if self.provider:
            cause["provider"] = self.provider
        if self.detail:
            cause["message"] = self.detail
        if self.request_id:
            cause["request_id"] = self.request_id
        return cause


def failure_streak_category(streak: FailureStreak) -> str:
    """Keep a deterministic provider rejection as the run's terminal cause."""
    if streak.error_class == ERROR_CLASS_UPSTREAM_AUTH:
        return "model_auth"
    if streak.status_code in (400, 422):
        return "model_config"
    return FAILURE_CATEGORY_REPEATED_UPSTREAM_FAILURE


class RepeatedUpstreamFailureError(Exception):
    """A run's model calls kept failing identically; stop it."""

    category = FAILURE_CATEGORY_REPEATED_UPSTREAM_FAILURE

    def __init__(
        self,
        message: str,
        *,
        streak: FailureStreak,
        first_cause: Optional[FirstFailureCause] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.streak = streak
        self.category = failure_streak_category(streak)
        self.first_cause = first_cause


def parse_failure_streak_threshold(agent_config: Optional[Mapping[str, Any]]) -> int:
    """Read the breaker threshold from ``agent_config.limits``, tolerantly.

    Anything that is not an integer of at least 2 (absent, junk, zero, a
    bool, a fraction, 1) yields the default: a typo must never switch the
    breaker off, and 1 would stop a run on its first failure.

    Args:
        agent_config: The flow's ``agent_config`` JSON, possibly None.

    Returns:
        The number of identical failures in a row that stops a run.
    """
    default = DEFAULT_MAX_CONSECUTIVE_IDENTICAL_FAILURES
    if not isinstance(agent_config, Mapping):
        return default
    raw_limits = agent_config.get(LIMITS_KEY)
    if not isinstance(raw_limits, Mapping):
        return default
    raw = raw_limits.get(MAX_CONSECUTIVE_IDENTICAL_FAILURES_KEY)
    if isinstance(raw, float) and not raw.is_integer():
        return default
    parsed = _positive_int(raw)
    if parsed is None or parsed < 2:
        return default
    return parsed


def evaluate_failure_streak(
    outcomes: Sequence[GatewayAttemptOutcome], *, threshold: int
) -> Optional[FailureStreak]:
    """Return the trailing identical-failure streak when it has tripped.

    Args:
        outcomes: The execution's newest attempts, newest first.
        threshold: Identical failures in a row that stop the run.

    Returns:
        The streak when it is long enough, otherwise None.
    """
    if not outcomes or not outcomes[0].counts_as_failure:
        return None
    head = outcomes[0]
    count = 0
    for outcome in outcomes:
        if not outcome.counts_as_failure or outcome.signature != head.signature:
            break
        count += 1
    effective = threshold
    if is_terminal_error_class(head.error_class):
        effective = min(threshold, TERMINAL_CLASS_FAILURE_THRESHOLD)
    if count < effective:
        return None
    return FailureStreak(
        error_class=str(head.error_class),
        status_code=int(head.status_code),
        request_fingerprint=str(head.request_fingerprint),
        count=count,
        threshold=effective,
    )


def repeated_failure_message(
    streak: FailureStreak, first_cause: Optional[FirstFailureCause] = None
) -> str:
    """The readable sentence stored on the execution and sent to the agent.

    Args:
        streak: The tripped streak (why the run stopped).
        first_cause: The run's first failure (why it started failing), kept
            so a later error never stands in for the original cause.

    Returns:
        The stop summary.
    """
    message = (
        f"Repeated upstream failure: {streak.count} identical "
        f"{streak.error_class} (HTTP {streak.status_code}) failures in a row "
        "for the same model request; the gateway stopped the run instead of "
        f"retrying it (limit {streak.threshold}, set "
        f"agent_config.limits.{MAX_CONSECUTIVE_IDENTICAL_FAILURES_KEY} to change it)"
    )
    if streak.error_class == ERROR_CLASS_UPSTREAM_AUTH:
        message = (
            "Provider rejected this model's API key (HTTP "
            f"{streak.status_code}). Replace the API key on the model."
        )
    elif failure_streak_category(streak) == "model_config":
        message = (
            f"Model configuration rejected by the provider (HTTP {streak.status_code}). "
            "Check the model's structured output schema and request settings."
        )
    if first_cause is None:
        return message
    source = f"HTTP {first_cause.status_code}"
    if first_cause.origin == "gateway":
        source = f"{source} from the Preloop gateway"
    elif first_cause.origin == "upstream" and first_cause.provider:
        source = f"{source} from upstream provider {first_cause.provider}"
    if first_cause.error_class:
        source = f"{first_cause.error_class}, {source}"
    message = f"{message.rstrip('.')}. First failure in this run ({source})"
    if first_cause.detail:
        message = f"{message}: {first_cause.detail}"
    return message


def _read_first_failure_cause(
    db: Session, execution_id: str
) -> Optional[FirstFailureCause]:
    """Read the run's first failure since its last success. Never raises."""
    try:
        from preloop.models.crud import crud_api_usage

        row = crud_api_usage.get_first_gateway_failure(
            db,
            execution_id=execution_id,
            excluded_error_classes=sorted(_NOT_UPSTREAM_ERROR_CLASSES),
        )
    except Exception:  # noqa: BLE001 - the refusal matters more than the cause
        logger.warning(
            "Could not read the first failure cause for %s",
            execution_id,
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:  # pragma: no cover - rollback is best effort
            logger.debug("Rollback after first-cause read failure failed")
        return None
    if not row:
        return None
    from preloop.utils.secret_scrubbing import scrub_secrets

    raw_detail = row.get("error_detail")
    detail = (
        (scrub_secrets(str(raw_detail)) or "")[:_MAX_FIRST_CAUSE_DETAIL_CHARS]
        if raw_detail
        else None
    )
    return FirstFailureCause(
        status_code=int(row.get("status_code") or 0),
        error_class=row.get("error_class") or None,
        provider=row.get("provider_name") or None,
        detail=detail or None,
        request_id=row.get("gateway_request_id") or None,
    )


def _append_breaker_milestone(
    db: Session,
    execution: Any,
    streak: FailureStreak,
    message: str,
    first_cause: Optional[FirstFailureCause] = None,
) -> None:
    """Persist the trip to the execution timeline. Never raises."""
    try:
        from preloop.models.crud import crud_flow_execution

        metadata: dict[str, Any] = {
            "milestone": REPEATED_UPSTREAM_FAILURE_MILESTONE,
            "error_class": streak.error_class,
            "status_code": streak.status_code,
            "attempts": streak.count,
            "threshold": streak.threshold,
            "request_fingerprint": streak.request_fingerprint,
        }
        if first_cause is not None:
            metadata["first_cause"] = first_cause.to_dict()
        crud_flow_execution.append_log(
            db,
            execution_id=execution.id,
            log_data={
                "type": "milestone",
                "message": message,
                "metadata": metadata,
            },
        )
    except Exception:  # noqa: BLE001 - the refusal matters more than the log
        logger.exception(
            "Could not log the repeated-upstream-failure milestone for %s",
            getattr(execution, "id", None),
        )
        try:
            db.rollback()
        except Exception:  # pragma: no cover - rollback is best effort
            logger.debug("Rollback after milestone failure failed")


def enforce_failure_streak_for_id(db: Session, *, execution_id: Any) -> None:
    """Refuse the next request of a run whose calls keep failing identically.

    Read failures never break gateway traffic: any error while reading the
    execution or its attempts lets the request through. A run already
    terminal keeps being refused while the streak stands, but its row and
    timeline are written only once, by the request that tripped the breaker.

    Args:
        db: Database session.
        execution_id: The execution id from the credential context.

    Raises:
        RepeatedUpstreamFailureError: The trailing attempts are a tripped
            streak. A running execution has been marked FAILED and the
            milestone appended first.
    """
    if not execution_id:
        return
    try:
        execution_uuid = uuid.UUID(str(execution_id))
    except (ValueError, TypeError, AttributeError):
        return
    from preloop.models.crud import crud_api_usage, crud_flow, crud_flow_execution

    try:
        execution = crud_flow_execution.get(db, id=str(execution_uuid))
        if execution is None:
            return
        flow = getattr(execution, "flow", None)
        if flow is None and getattr(execution, "flow_id", None) is not None:
            flow = crud_flow.get(db, id=execution.flow_id)
        threshold = parse_failure_streak_threshold(getattr(flow, "agent_config", None))
        rows = crud_api_usage.get_recent_gateway_outcomes(
            db, execution_id=str(execution_uuid), limit=threshold
        )
        streak = evaluate_failure_streak(
            [
                GatewayAttemptOutcome(
                    status_code=int(row.get("status_code") or 0),
                    error_class=row.get("error_class"),
                    request_fingerprint=row.get("request_fingerprint"),
                )
                for row in rows
            ],
            threshold=threshold,
        )
    except Exception:  # noqa: BLE001 - a breaker read never breaks a request
        logger.warning(
            "Could not evaluate the upstream failure streak for %s",
            execution_uuid,
            exc_info=True,
        )
        try:
            db.rollback()
        except Exception:  # pragma: no cover - rollback is best effort
            logger.debug("Rollback after streak read failure failed")
        return
    if streak is None:
        return
    first_cause = _read_first_failure_cause(db, str(execution_uuid))
    message = repeated_failure_message(streak, first_cause)
    if str(getattr(execution, "status", "") or "").upper() not in TERMINAL_STATUSES:
        _fail_once_with_milestone(db, execution, streak, message, first_cause)
    raise RepeatedUpstreamFailureError(message, streak=streak, first_cause=first_cause)


def _fail_once_with_milestone(
    db: Session,
    execution: Any,
    streak: FailureStreak,
    message: str,
    first_cause: Optional[FirstFailureCause] = None,
) -> None:
    """Move the run to FAILED and log the milestone, once across requests.

    The conditional update decides which of several concurrent refusals owns
    the transition; only that one appends the milestone, in the same commit.
    Never raises: the refusal matters more than the annotation.
    """
    from preloop.models.crud import crud_flow_execution

    try:
        moved = crud_flow_execution.fail_if_active(
            db,
            execution_id=execution.id,
            error_message=message,
            failure_category=failure_streak_category(streak),
        )
        if not moved:
            db.rollback()
            return
        _append_breaker_milestone(db, execution, streak, message, first_cause)
        db.refresh(execution)
    except Exception:  # noqa: BLE001 - annotation never fails the refusal
        logger.exception(
            "Failed to mark execution %s as repeated_upstream_failure",
            getattr(execution, "id", None),
        )
        try:
            db.rollback()
        except Exception:  # pragma: no cover - rollback is best effort
            logger.debug("Rollback after breaker annotation failure failed")
