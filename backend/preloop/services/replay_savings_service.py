"""Orchestration for per-user re-execution replay savings (T14 wiring, T15).

Layers:
  * ``execute_replay`` — pure orchestration over the harness and the budget
    guard. Always computes the deterministic input-token delta first (free), so
    even a budget-aborted replay records the exact input number. Injected
    re-execution keeps this unit-testable without spending tokens.
  * ``persist_replay_run`` — maps a :class:`ReplayOutcome` onto a stored
    ``runtime_session_replay_run`` row via the CRUD layer.
  * ``build_gateway_reexecute`` — wraps a live gateway into the ``reexecute``
    callable the harness expects (re-runs a payload, returns tokens + cost).

The live wiring that selects a session's stored request and drives a real
gateway lives in the endpoint; this module keeps the money-path logic (consent
recording, hard-cap abort, deterministic fallback) in one tested place.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from sqlalchemy.orm import Session

from preloop.models.models.account import Account
from preloop.models.models.ai_model import AIModel
from preloop.services.optimization_gating import authorize_analysis_model
from preloop.services.replay_harness import (
    BudgetGuardedReexecutor,
    ReplayBudgetExceededError,
    ReplayMeasurement,
    ReplayRunTokens,
    measure_replay_savings,
)
from preloop.services.savings_measurement import (
    InputTokenSavings,
    compute_input_token_savings,
    estimate_payload_tokens,
)
from preloop.services.model_pricing import estimate_ai_model_usage_cost

logger = logging.getLogger(__name__)

STATUS_COMPLETED = "completed"
STATUS_ABORTED_BUDGET = "aborted_budget"
STATUS_NO_PAYLOAD = "no_payload"
STATUS_FAILED_USAGE_MISSING = "failed_usage_missing"
STATUS_FAILED_UPSTREAM = "failed_upstream_error"

# Hard ceiling on what one replay may spend, and the FALLBACK per-run cost
# projection used only when model pricing cannot price the payload (unknown
# model, no litellm entry). The primary projection comes from
# ``build_payload_cost_estimator`` (real token counts x real pricing). Account
# budgets are additionally enforced at request time by the gateway's budget
# enforcer, so this ceiling bounds replay spend on top of, not instead of,
# the user's policies.
REPLAY_MAX_SPEND_USD = 0.50
REPLAY_PER_RUN_COST_ESTIMATE = 0.05

# Completion-side projection when the payload does not set max_tokens: the
# guard must assume a realistic upper bound, not zero.
DEFAULT_COMPLETION_TOKENS_ESTIMATE = 1024


class ConsentRequiredError(Exception):
    """Raised when a replay is requested without explicit user consent.

    A replay re-sends the user's stored request payload upstream and spends
    their budget, so it must never run on an unconsented request.
    """


class ReplayUsageMissingError(Exception):
    """Raised when an upstream response carries no usable token usage.

    The replay is a measurement instrument: a run whose usage is missing must
    fail the measurement loudly rather than be counted as zero tokens, which
    would silently fabricate savings. Raising aborts the harness loop cleanly
    (no partial measurement is materialized).
    """


class ReplayUpstreamError(Exception):
    """Raised when the upstream model rejects or fails a replay re-execution.

    Stored payloads can predate a provider's current wire contract (e.g. a
    model update starts rejecting histories captured before it), and upstreams
    fail for transient reasons. Either way the measurement did not happen:
    surface a clean, persisted ``failed_upstream_error`` outcome instead of a
    raw 500, keeping the deterministic input-token delta (which never needs
    the upstream) as the authoritative number.
    """


@dataclass(frozen=True)
class ReplayOutcome:
    """Result of an attempted replay, whatever its terminal state.

    Attributes:
        status: ``completed`` | ``aborted_budget`` | ``no_payload`` |
            ``failed_usage_missing``.
        input_savings: The exact deterministic input-token delta. Always
            present (it costs nothing to compute), so an aborted or
            payload-less replay still carries the authoritative input number.
        measurement: The full end-to-end measurement, or ``None`` when the
            replay did not complete.
        cost_spent: Dollars spent re-executing before completion/abort, or
            ``None`` when nothing was spent.
    """

    status: str
    input_savings: InputTokenSavings
    measurement: Optional[ReplayMeasurement]
    cost_spent: Optional[float]


def execute_replay(
    target_payload: Optional[dict[str, Any]],
    *,
    reexecute: Callable[[dict[str, Any]], ReplayRunTokens],
    estimate_cost: Callable[[dict[str, Any]], float],
    remaining_budget: float,
    removed_tool_names: Optional[set[str]] = None,
    filtered_output_fields: Optional[Mapping[str, Sequence[str]]] = None,
    n_runs: int = 3,
) -> ReplayOutcome:
    """Run a replay under the user's hard budget cap.

    Args:
        target_payload: The stored request to replay, or ``None`` when the
            session had no stored payload to re-execute.
        reexecute: Live re-execution callable (see ``build_gateway_reexecute``).
        estimate_cost: Projects a payload's cost before it is run, for the guard.
        remaining_budget: The user's remaining hard-cap headroom in dollars.
        removed_tool_names: Tool names the candidate removes.
        filtered_output_fields: Tool-output fields the candidate drops.
        n_runs: Re-execution pairs per side.

    Returns:
        A :class:`ReplayOutcome`. ``aborted_budget`` when the whole replay was
        infeasible under the cap (nothing spent) or the guard stopped it
        mid-loop; ``no_payload`` when there was nothing to replay;
        ``failed_usage_missing`` when an upstream response carried no usable
        usage; otherwise ``completed``.
    """
    if target_payload is None:
        empty = InputTokenSavings(
            original_tokens=0, modified_tokens=0, delta_tokens=0, pct_saved=0.0
        )
        return ReplayOutcome(STATUS_NO_PAYLOAD, empty, None, None)

    # Deterministic and free: compute first so every outcome carries it.
    input_savings = compute_input_token_savings(
        target_payload,
        removed_tool_names=removed_tool_names,
        filtered_output_fields=filtered_output_fields,
    )

    # Feasibility precheck: a full measurement needs 2 * n_runs upstream calls
    # (original + modified per pair; the modified side costs at most the
    # original). Abort BEFORE the first call when the projection cannot fit —
    # spending on runs that can never complete a measurement is waste.
    projected_total = 2 * n_runs * estimate_cost(target_payload)
    if projected_total > remaining_budget:
        return ReplayOutcome(STATUS_ABORTED_BUDGET, input_savings, None, None)

    guard = BudgetGuardedReexecutor(
        raw_reexecute=reexecute,
        remaining_budget=remaining_budget,
        estimate_cost=estimate_cost,
    )
    try:
        measurement = measure_replay_savings(
            target_payload,
            reexecute=guard,
            removed_tool_names=removed_tool_names,
            filtered_output_fields=filtered_output_fields,
            n_runs=n_runs,
        )
    except ReplayBudgetExceededError:
        # Clean abort: no partial measurement was produced. Record the exact
        # input delta and whatever was spent before the cap was hit.
        return ReplayOutcome(
            STATUS_ABORTED_BUDGET,
            input_savings,
            None,
            guard.spent or None,
        )
    except ReplayUsageMissingError:
        # The instrument failed, loudly: never zero-count a run with missing
        # usage. The exact input delta still stands; spend so far is recorded.
        return ReplayOutcome(
            STATUS_FAILED_USAGE_MISSING,
            input_savings,
            None,
            guard.spent or None,
        )
    except ReplayUpstreamError:
        # The upstream rejected or failed a re-execution: no measurement was
        # produced. Record the exact input delta and whatever was spent.
        return ReplayOutcome(
            STATUS_FAILED_UPSTREAM,
            input_savings,
            None,
            guard.spent or None,
        )
    return ReplayOutcome(
        STATUS_COMPLETED,
        measurement.input_savings,
        measurement,
        measurement.cost_spent,
    )


def persist_replay_run(
    db: Any,
    crud: Any,
    *,
    account_id: Any,
    runtime_session_id: Any,
    candidate: dict[str, Any],
    outcome: ReplayOutcome,
    n_runs: int,
    consented_by: Optional[str],
    requested_by: Optional[str],
    suggestion_id: Optional[str] = None,
) -> Any:
    """Persist a replay outcome as a ``runtime_session_replay_run`` row.

    Args:
        db: Database session.
        crud: The ``crud_runtime_session_replay_run`` instance.
        account_id: Owning account id.
        runtime_session_id: Replayed runtime session id.
        candidate: The applied candidate (removed tools / filtered fields).
        outcome: The replay outcome to persist.
        n_runs: Configured re-execution pairs.
        consented_by: Username that consented to the replay.
        requested_by: Username that requested the replay.
        suggestion_id: Originating suggestion id, if any.

    Returns:
        The stored replay-run row.
    """
    measurement = outcome.measurement
    return crud.create_run(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        suggestion_id=suggestion_id,
        candidate=candidate,
        n_runs=n_runs if outcome.status == STATUS_COMPLETED else 0,
        input_delta_tokens=outcome.input_savings.delta_tokens,
        input_pct_saved=outcome.input_savings.pct_saved,
        end_to_end_delta_median=(
            measurement.end_to_end_delta_median if measurement else None
        ),
        end_to_end_delta_low=(
            measurement.end_to_end_delta_low if measurement else None
        ),
        end_to_end_delta_high=(
            measurement.end_to_end_delta_high if measurement else None
        ),
        inconclusive=(measurement.inconclusive if measurement else True),
        cost_spent=outcome.cost_spent,
        status=outcome.status,
        consented_by=consented_by,
        requested_by=requested_by,
        result={
            "input_savings": {
                "original_tokens": outcome.input_savings.original_tokens,
                "modified_tokens": outcome.input_savings.modified_tokens,
                "delta_tokens": outcome.input_savings.delta_tokens,
                "pct_saved": outcome.input_savings.pct_saved,
            },
            "end_to_end": (
                {
                    "median": measurement.end_to_end_delta_median,
                    "low": measurement.end_to_end_delta_low,
                    "high": measurement.end_to_end_delta_high,
                    "inconclusive": measurement.inconclusive,
                    "original_total_median": measurement.original_total_median,
                    "modified_total_median": measurement.modified_total_median,
                }
                if measurement
                else None
            ),
        },
    )


def prepare_replay_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a stored payload normalized for synchronous replay.

    Two normalizations, applied identically to the original and the modified
    side so the comparison stays fair (and never applied to the deterministic
    input-token measurement, which runs on the raw payloads):

      * ``stream`` is forced off (and ``stream_options`` dropped): agents
        stream, but the replay path measures a complete response through
        ``create_chat_completion``, which rejects ``stream=true``.
      * Deepseek thinking models now require ``reasoning_content`` to be
        passed back on assistant turns; payloads stored before that contract
        lack it and are rejected with a 400 on replay. An explicit empty
        ``reasoning_content`` satisfies the contract (verified against the
        live API), so it is added to assistant messages that miss it. Scoped
        to deepseek models only — other providers may reject unknown fields.

    Args:
        payload: The stored gateway request payload. Never mutated.

    Returns:
        A deep-copied payload safe to re-execute synchronously.
    """
    prepared = copy.deepcopy(payload)
    prepared["stream"] = False
    prepared.pop("stream_options", None)
    if "deepseek" in str(prepared.get("model") or "").lower():
        messages = prepared.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and "reasoning_content" not in message
                ):
                    message["reasoning_content"] = ""
    return prepared


def build_gateway_reexecute(
    gateway: Any,
    *,
    purpose: str = "replay_validation",
    ai_model: Optional[AIModel] = None,
) -> Callable[[dict[str, Any]], ReplayRunTokens]:
    """Wrap a live gateway service into a ``reexecute`` callable.

    The returned callable re-runs a payload through the gateway (tagged
    ``purpose`` so the spend is attributed to replay validation, not the
    analyzed session) and extracts token usage from the response.

    Usage is mandatory: a response with no usable token counts raises
    :class:`ReplayUsageMissingError` instead of being zero-counted — a
    measurement instrument must fail loudly, never fabricate a zero. A
    ``budget_limit_exceeded`` error raised by the gateway's budget enforcer
    mid-run is mapped to :class:`ReplayBudgetExceededError` so the account
    policy tripping produces the same clean abort as the replay cap.

    When ``ai_model`` is provided, each run's real cost is estimated from its
    actual usage via model pricing; otherwise cost is ``None`` and the budget
    guard falls back to its pre-call projection.

    Args:
        gateway: A gateway service exposing ``create_chat_completion``.
        purpose: Attribution purpose tag for the replay traffic.
        ai_model: The model serving the replay, for actual-cost estimation.

    Returns:
        A callable suitable as the harness ``reexecute`` argument.

    Raises:
        ReplayUsageMissingError: (from the returned callable) when the
            response has neither ``total_tokens`` nor both ``prompt_tokens``
            and ``completion_tokens``.
    """

    def reexecute(payload: dict[str, Any]) -> ReplayRunTokens:
        tagged = prepare_replay_payload(payload)
        metadata = dict(tagged.get("metadata") or {})
        metadata["purpose"] = purpose
        tagged["metadata"] = metadata
        try:
            response = gateway.create_chat_completion(tagged)
        except ReplayBudgetExceededError:
            raise
        except Exception as exc:
            if getattr(exc, "code", None) == "budget_limit_exceeded":
                raise ReplayBudgetExceededError(
                    "account budget policy tripped during replay re-execution"
                ) from exc
            # The measurement did not happen; fail the replay cleanly instead
            # of surfacing a raw 500 (stored payloads can predate the
            # provider's current wire contract, and upstreams fail transiently).
            raise ReplayUpstreamError(
                f"upstream re-execution failed for model="
                f"{payload.get('model')!r}: {exc}"
            ) from exc
        usage = (response or {}).get("usage") or {}
        total = usage.get("total_tokens")
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if total is None:
            if prompt is None or completion is None:
                raise ReplayUsageMissingError(
                    "upstream response carried no usable token usage for "
                    f"model={payload.get('model')!r}; refusing to zero-count "
                    "a measurement run"
                )
            total = int(prompt) + int(completion)
        cost: Optional[float] = None
        if ai_model is not None:
            cost = estimate_ai_model_usage_cost(
                ai_model,
                prompt_tokens=int(prompt or 0),
                completion_tokens=int(completion or 0),
                total_tokens=int(total),
                usage_details=usage if isinstance(usage, dict) else None,
            )
        return ReplayRunTokens(total_tokens=int(total), cost=cost)

    return reexecute


def build_payload_cost_estimator(
    ai_model: Optional[AIModel],
    *,
    fallback_per_run: float = REPLAY_PER_RUN_COST_ESTIMATE,
) -> Callable[[dict[str, Any]], float]:
    """Build the budget guard's pre-call cost projection for a payload.

    Projects prompt tokens with the same heuristic the savings math uses
    (``estimate_payload_tokens``) and the completion side from the payload's
    ``max_tokens`` (or a conservative default), then prices both via model
    pricing. Falls back to ``fallback_per_run`` when the model is unknown or
    unpriced, so the guard always has a positive projection.

    Args:
        ai_model: The model expected to serve the replay, or ``None``.
        fallback_per_run: Projection used when pricing cannot resolve.

    Returns:
        A callable mapping a payload to its projected dollar cost.
    """

    def estimate(payload: dict[str, Any]) -> float:
        if ai_model is None:
            return fallback_per_run
        prompt_tokens = estimate_payload_tokens(payload)
        raw_max = payload.get("max_tokens") or payload.get("max_completion_tokens")
        try:
            completion_tokens = (
                int(raw_max) if raw_max else DEFAULT_COMPLETION_TOKENS_ESTIMATE
            )
        except (TypeError, ValueError):
            completion_tokens = DEFAULT_COMPLETION_TOKENS_ESTIMATE
        cost = estimate_ai_model_usage_cost(
            ai_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )
        return cost if cost is not None and cost > 0 else fallback_per_run

    return estimate


def resolve_payload_ai_model(
    db: Any, *, account: Any, payload: Optional[dict[str, Any]]
) -> Optional[AIModel]:
    """Resolve the configured AIModel a stored payload's model name refers to.

    Matches the payload's ``model`` string against the account's configured
    models (including system defaults) by ``model_identifier`` or gateway
    alias, falling back to the account's default active model. Returns
    ``None`` when nothing resolves — callers then use fallback pricing.

    Args:
        db: Database session.
        account: The owning account.
        payload: The stored request payload (may be ``None``).

    Returns:
        The matching :class:`AIModel`, or ``None``.
    """
    from preloop.models.crud import crud_ai_model

    name = str((payload or {}).get("model") or "").strip()
    if name:
        for candidate in crud_ai_model.get_all_for_account(db, account_id=account.id):
            identifier = (candidate.model_identifier or "").strip()
            meta = candidate.meta_data if isinstance(candidate.meta_data, dict) else {}
            gateway_cfg = (
                meta.get("gateway") if isinstance(meta.get("gateway"), dict) else {}
            )
            alias = str(gateway_cfg.get("model_alias") or "").strip()
            if name in {identifier, alias} or identifier.endswith(f"/{name}"):
                return candidate
    return crud_ai_model.get_default_active_model(db, account_id=str(account.id))


def _tool_name(definition: Any) -> str:
    """Resolve a tool definition's name (function.name or top-level name)."""
    if not isinstance(definition, dict):
        return ""
    function = definition.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    if definition.get("name"):
        return str(definition["name"])
    return ""


def select_target_request(
    events: Sequence[Any], removed_tool_names: set[str]
) -> Optional[dict[str, Any]]:
    """Pick the stored request to replay from a session's gateway events.

    A gateway event's ``payload`` wraps the original request under ``request``.
    Prefer a request that actually carries one of the tools being removed (so
    the replay exercises the candidate), and among the eligible requests pick
    the largest by serialized size. Falls back to the largest request overall.

    Args:
        events: Loaded gateway call events (each with a ``payload`` dict).
        removed_tool_names: Tool names the candidate removes.

    Returns:
        The extracted request dict to replay, or ``None`` when the session has
        no stored request payload.
    """
    best: Optional[dict[str, Any]] = None
    best_score: tuple[int, int] = (-1, -1)
    for event in events:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        request = payload.get("request")
        if not isinstance(request, dict) or not request:
            continue
        tools = request.get("tools") or request.get("functions") or []
        names = {_tool_name(t) for t in tools if isinstance(t, dict)}
        has_removed = 1 if (names & removed_tool_names) else 0
        try:
            size = len(json.dumps(request, default=str))
        except (TypeError, ValueError):
            size = 0
        score = (has_removed, size)
        if score > best_score:
            best_score = score
            best = request
    return best


def _default_events_loader(
    db: Any, *, account: Any, runtime_session_id: str
) -> Sequence[Any]:
    """Load a session's gateway events via the shared context-analysis loader."""
    from preloop.services.context_analysis import load_session_gateway_events

    return load_session_gateway_events(
        db, account=account, runtime_session_id=runtime_session_id
    )


def _default_budget_headroom(db: Any, account_id: Any) -> Optional[float]:
    """Read the account's remaining hard-cap headroom for the pre-call guard."""
    from preloop.services.budget_headroom import remaining_account_budget_headroom

    return remaining_account_budget_headroom(db, account_id)


class GatewayBudgetEnforcer(Protocol):
    """Structural type for the injected gateway budget enforcer.

    Replay never calls the enforcer itself; it forwards it to
    ``OpenAIGatewayService``, whose pre-call budget check invokes exactly one
    method (see ``OpenAIGatewayService._check_budget``). The OSS default is
    ``preloop.api.deps.NoopBudgetEnforcer``; billing plugins override it.
    ``auth_context`` stays ``Any``: its concrete type lives in
    ``preloop.services.model_gateway_auth``, which is imported lazily below
    to keep this module import-light.
    """

    def enforce_or_raise(
        self,
        db: Session,
        auth_context: Any,
        ai_model: AIModel,
        payload: dict[str, Any],
    ) -> Any:
        """Raise when the pending gateway call would breach a hard limit."""
        pass


def _default_gateway_factory(
    db: Session,
    current_user: Any,
    budget_enforcer: Optional[GatewayBudgetEnforcer] = None,
) -> Any:
    """Build a gateway service for replay, with session attribution suppressed.

    Mirrors ``SessionOptimizationService._create_gateway_service``: replay is
    Preloop-driven validation traffic, so it is not attributed to the analyzed
    runtime session.

    Args:
        db: Database session.
        current_user: The authenticated replay requester.
        budget_enforcer: Budget enforcer for the replay traffic. Endpoints
            inject the deployment's enforcer via the ``get_budget_enforcer``
            dependency hook (which billing plugins override); ``None`` leaves
            the gateway's default (no-op) enforcement.
    """
    from preloop.services.model_gateway_auth import (
        NO_BEARER_TOKEN,
        ModelGatewayAuthContext,
    )
    from preloop.services.openai_gateway import OpenAIGatewayService

    # Synthetic context: user is already authenticated; no inbound bearer.
    auth_context = ModelGatewayAuthContext(token=NO_BEARER_TOKEN, user=current_user)
    return OpenAIGatewayService(
        db,
        auth_context,
        budget_enforcer=budget_enforcer,
        skip_runtime_session_resolution=True,
    )


def run_session_replay(
    db: Session,
    *,
    account: Account,
    current_user: Any,
    runtime_session_id: str,
    candidate: dict[str, Any],
    consented: bool,
    n_runs: int = 3,
    suggestion_id: Optional[str] = None,
    budget_enforcer: Optional[GatewayBudgetEnforcer] = None,
    gateway_factory: Optional[Callable[[], Any]] = None,
    events_loader: Optional[Callable[..., Sequence[Any]]] = None,
    ai_model_resolver: Optional[Callable[..., Optional[AIModel]]] = None,
    budget_headroom_fn: Optional[Callable[..., Optional[float]]] = None,
    max_spend: float = REPLAY_MAX_SPEND_USD,
    per_run_estimate: float = REPLAY_PER_RUN_COST_ESTIMATE,
) -> Any:
    """Load, replay, and persist per-user savings verification for a session.

    Args:
        db: Database session.
        account: The owning account.
        current_user: The requesting/consenting user.
        runtime_session_id: Session whose stored request is replayed.
        candidate: ``{"removed_tool_names": [...], "filtered_output_fields":
            {tool: [fields]}}``.
        consented: Must be ``True`` — a replay re-sends the payload upstream and
            spends the user's budget.
        n_runs: Re-execution pairs per side.
        suggestion_id: Originating suggestion id, if any.
        budget_enforcer: Budget enforcer for the replay gateway traffic
            (injected by the endpoint from the ``get_budget_enforcer``
            dependency hook). Ignored when ``gateway_factory`` is provided.
        gateway_factory: Injectable gateway builder (defaults to the live one).
        events_loader: Injectable session-events loader (defaults to the live
            context-analysis loader).
        ai_model_resolver: Injectable payload-model resolver (defaults to
            :func:`resolve_payload_ai_model`), for pricing the replay.
        budget_headroom_fn: Injectable account-headroom reader (defaults to
            :func:`remaining_account_budget_headroom`). Used to cap the pre-call
            guard at the account's remaining hard-cap headroom, so a replay
            never even starts a run that would breach the account budget.
        max_spend: Hard ceiling on replay spend for this run. The effective
            pre-call budget is ``min(max_spend, account headroom)``; the gateway
            additionally enforces all budget policies per request.
        per_run_estimate: FALLBACK pre-call projection per run, used only when
            model pricing cannot price the payload.

    Returns:
        The persisted ``runtime_session_replay_run`` row.

    Raises:
        ConsentRequiredError: If ``consented`` is not ``True``.
    """
    if not consented:
        raise ConsentRequiredError("replay requires explicit user consent")

    from preloop.models.crud import crud_runtime_session_replay_run

    removed = {str(name) for name in (candidate.get("removed_tool_names") or [])}
    filtered = candidate.get("filtered_output_fields") or None
    username = getattr(current_user, "username", None)

    # Cap the pre-call guard at the tighter of the replay ceiling and the
    # account's remaining hard-cap headroom, so a replay never starts a run that
    # would breach the account budget. Fail-open: if the headroom read errors,
    # fall back to the replay ceiling — the gateway remains the authoritative,
    # per-request enforcer of the full policy set, so a failed read here never
    # allows more than ``max_spend``.
    headroom_fn = budget_headroom_fn or _default_budget_headroom
    try:
        headroom = headroom_fn(db, account.id)
    except Exception:
        logger.warning(
            "replay: failed to read account budget headroom; using max_spend",
            exc_info=True,
        )
        headroom = None
    effective_budget = min(max_spend, headroom) if headroom is not None else max_spend

    loader = events_loader or _default_events_loader
    events = loader(db, account=account, runtime_session_id=runtime_session_id)
    target = select_target_request(events, removed)

    if target is None:
        outcome = execute_replay(
            None,
            reexecute=lambda _payload: ReplayRunTokens(total_tokens=0),
            estimate_cost=lambda _payload: per_run_estimate,
            remaining_budget=effective_budget,
        )
    else:
        resolver = ai_model_resolver or resolve_payload_ai_model
        ai_model = resolver(db, account=account, payload=target)
        # A replay re-executes through the resolved model: when that model is
        # a deployment-hosted one (operator-paid compute), the registered
        # analysis-model authorizer may deny (e.g. 402). BYOK models pass.
        authorize_analysis_model(
            db, account=account, ai_model=ai_model, feature="replay_verification"
        )
        factory = gateway_factory or (
            lambda: _default_gateway_factory(db, current_user, budget_enforcer)
        )
        reexecute = build_gateway_reexecute(factory(), ai_model=ai_model)
        outcome = execute_replay(
            target,
            reexecute=reexecute,
            estimate_cost=build_payload_cost_estimator(
                ai_model, fallback_per_run=per_run_estimate
            ),
            remaining_budget=effective_budget,
            removed_tool_names=removed or None,
            filtered_output_fields=filtered,
            n_runs=n_runs,
        )

    if outcome.status in (STATUS_FAILED_USAGE_MISSING, STATUS_FAILED_UPSTREAM):
        logger.error(
            "Replay measurement FAILED for runtime_session_id=%s: run "
            "recorded as %s, spent=%s",
            runtime_session_id,
            outcome.status,
            outcome.cost_spent,
        )

    return persist_replay_run(
        db,
        crud_runtime_session_replay_run,
        account_id=account.id,
        runtime_session_id=runtime_session_id,
        candidate=candidate,
        outcome=outcome,
        n_runs=n_runs,
        consented_by=username,
        requested_by=username,
        suggestion_id=suggestion_id,
    )
