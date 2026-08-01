"""Runtime-session optimization recommendations.

Generates evidence-grounded cost/context optimization suggestions for one
captured runtime session, attaches one-click applyable actions, and applies
them through the existing governance/budget APIs. Deterministic analysis is
always available; the optional LLM-powered pass runs on the account's own
configured model (BYOK) or, where a deployment gates founder-paid compute, on
a built-in hosted model subject to the registered analysis-model authorizer
(see :mod:`preloop.services.optimization_gating`).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import (
    crud_ai_model,
    crud_api_usage,
    crud_budget_policy,
    crud_managed_agent,
    crud_runtime_session,
    crud_runtime_session_optimization_action,
    crud_runtime_session_optimization_result,
    crud_tool_configuration,
)
from preloop.models.schemas.tool_configuration import (
    ToolConfigurationCreate,
    ToolConfigurationUpdate,
)
from preloop.models.models.account import Account
from preloop.models.models.ai_model import AIModel
from preloop.models.models.budget import BudgetPeriod
from preloop.models.models.user import User
from preloop.schemas.gateway_usage import (
    GatewayTokenUsage,
    RuntimeSessionOptimizationActionListResponse,
    RuntimeSessionOptimizationActionSpec,
    RuntimeSessionOptimizationAppliedAction,
    RuntimeSessionOptimizationApplyRequest,
    RuntimeSessionOptimizationRequest,
    RuntimeSessionOptimizationResponse,
    RuntimeSessionOptimizationSuggestion,
    RuntimeSessionSummary,
)
from preloop.services.account_governance_cache import (
    invalidate_account_governance_cache,
)
from preloop.services.builtin_tool_optimizer import (
    BUILTIN_USAGE_WINDOW_DAYS,
    UnusedBuiltinPartition,
    invocation_counts_by_tool,
    partition_unused_tools,
)
from preloop.services.context_analysis import (
    GatewayCallEvent,
    SessionContextProfile,
    build_profile_from_events,
    compute_profile_savings,
    load_session_gateway_events,
)
from preloop.services.context_optimization import (
    CONTEXT_OPTIMIZATION_KEY,
    MIN_TOOL_RESULT_CAP,
)
from preloop.services.subject_governance import (
    SUBJECT_TYPE_MANAGED_AGENTS,
    SUBJECT_TYPES,
    get_subject_governance,
    set_subject_governance,
)
from preloop.services.model_gateway_auth import (
    NO_BEARER_TOKEN,
    ModelGatewayAuthContext,
)
from preloop.services.optimization_gating import authorize_analysis_model
from preloop.services.model_pricing import estimate_ai_model_usage_cost
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.runtime_session_explorer import RuntimeSessionExplorerService
from preloop.services.tool_schema_tokens import estimate_tool_schema_tokens
from preloop.services.tool_usage_stats import ToolUsageStatsService

logger = logging.getLogger(__name__)

# Recent gateway events are enough for optimization pattern detection without
# blowing the analysis prompt budget on very long sessions.
DEFAULT_MAX_OPTIMIZATION_PAYLOADS = 30

# Hard character budget for the optimization prompt (~4k tokens). The model
# only ever sees the profile digest plus pre-truncated offending snippets.
DIGEST_CHAR_BUDGET = 16_000
SNIPPET_CHAR_LIMIT = 400
MAX_DIGEST_SNIPPETS = 8
GATEWAY_EVENT_LOAD_LIMIT = 100

# The gateway tags the optimization call's own ApiUsage row with this purpose
# (via request metadata), which feeds the daily cap and cost analytics.
OPTIMIZATION_USAGE_PURPOSE = "session_optimization"
LLM_SKIPPED_DAILY_CAP = "daily_cap_reached"
# The selected model errored (e.g. incompatible params, unrefreshable creds) or
# returned no usable content; suggestions came from the deterministic analyzer.
LLM_SKIPPED_MODEL_ERROR = "model_error"
LLM_SKIPPED_MODEL_EMPTY = "model_empty_response"

# Server-applicable action types; open_events is resolved client-side.
ACTION_SCOPE_TOOLS = "scope_tools"
ACTION_DISABLE_BUILTIN_TOOLS = "disable_builtin_tools"
ACTION_SET_BUDGET = "set_budget"
ACTION_OPEN_EVENTS = "open_events"
ACTION_ENABLE_COMPRESSION = "enable_compression"
ACTION_CAP_TOOL_RESULTS = "cap_tool_results"
ACTION_MANAGE_OUTPUT_FILTER = "manage_output_filter"
APPLYABLE_ACTION_TYPES = (
    ACTION_SCOPE_TOOLS,
    ACTION_DISABLE_BUILTIN_TOOLS,
    ACTION_SET_BUDGET,
    ACTION_ENABLE_COMPRESSION,
    ACTION_CAP_TOOL_RESULTS,
)

# Default gateway tool-result cap suggested when oversized outputs are found.
DEFAULT_TOOL_RESULT_CAP_CHARS = 8_000

# Budget subject type used for managed-agent scoped budget policies.
BUDGET_SUBJECT_MANAGED_AGENT = "managed_agent"

# Window used to capture the pre-apply baseline for outcome measurement.
BASELINE_WINDOW_DAYS = 7

# Suggested daily budget = session cost * this multiplier (minimum $1).
BUDGET_SUGGESTION_MULTIPLIER = 3.0


class SessionOptimizationService:
    """Generate cost and context optimization suggestions for runtime sessions."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_account_session_optimization_suggestions(
        self,
        *,
        account: Account,
        runtime_session_id: str,
        request: RuntimeSessionOptimizationRequest,
        current_user: User,
        budget_enforcer: Any = None,
    ) -> RuntimeSessionOptimizationResponse:
        """Return deterministic or model-generated optimization suggestions."""
        summary_row = crud_runtime_session.get_account_session_summary(
            self.db,
            account_id=str(account.id),
            runtime_session_id=runtime_session_id,
        )
        if summary_row is None:
            raise HTTPException(status_code=404, detail="Runtime session not found")
        summary = RuntimeSessionExplorerService._summary_row_to_schema(summary_row)
        # Cache-only requests surface previously generated suggestions on panel
        # open without triggering a (potentially slow) generation. A miss is
        # reported explicitly so the UI keeps the "generate" prompt.
        if request.cache_only:
            cached = self._load_latest_cached_response(
                account=account, runtime_session_id=runtime_session_id
            )
            if cached is not None:
                return cached
            return RuntimeSessionOptimizationResponse(
                generated_by="local",
                cache_miss=True,
                generated_at=datetime.now(timezone.utc),
                suggestions=[],
            )
        fast_model = self._resolve_optimization_model(
            account=account, model_id=request.model_id
        )
        scope_hash = self._scope_hash(request, fast_model)
        if not request.regenerate:
            cached = self._load_cached_response(
                account=account,
                runtime_session_id=runtime_session_id,
                scope_hash=scope_hash,
            )
            if cached is not None:
                return cached
        # Gate LLM-powered analysis on the resolved model BEFORE any generation
        # work: deployments that meter founder-paid hosted compute register an
        # authorizer that may reject hosted-model analysis (e.g. with a 402
        # upgrade contract). BYOK models pass untouched, deterministic-only
        # accounts (``fast_model is None``) are never gated, and cached reads
        # above stay free.
        authorize_analysis_model(
            self.db,
            account=account,
            ai_model=fast_model,
            feature="session_optimization",
        )
        gateway_events = load_session_gateway_events(
            self.db,
            account=account,
            runtime_session_id=runtime_session_id,
            event_ids=request.event_ids or None,
            limit=GATEWAY_EVENT_LOAD_LIMIT,
        )
        profile = build_profile_from_events(runtime_session_id, gateway_events)
        suggestions = self._local_optimization_suggestions(
            summary, profile, account=account
        )
        llm_skipped_reason: Optional[str] = None
        if fast_model is not None and self._daily_optimization_cap_reached(account):
            logger.info(
                "Skipping optimization model call: daily cap reached",
                extra={"account_id": str(account.id)},
            )
            llm_skipped_reason = LLM_SKIPPED_DAILY_CAP
            fast_model = None
        if fast_model is not None:
            scoped_payloads = self._scoped_payloads_from_events(gateway_events)
            try:
                generated, token_usage, estimated_cost = self._call_optimization_model(
                    model=fast_model,
                    summary=summary,
                    profile=profile,
                    scoped_payloads=scoped_payloads,
                    request=request,
                    current_user=current_user,
                    budget_enforcer=budget_enforcer,
                    runtime_session_id=runtime_session_id,
                )
                if generated:
                    response = RuntimeSessionOptimizationResponse(
                        generated_by="model",
                        fast_model_name=fast_model.name,
                        model_id=str(fast_model.id),
                        model_name=fast_model.name,
                        token_usage=token_usage,
                        estimated_optimization_cost=estimated_cost,
                        generated_at=datetime.now(timezone.utc),
                        suggestions=generated,
                    )
                    self._finalize_response(
                        response, account=account, summary=summary, profile=profile
                    )
                    self._store_cached_response(
                        account=account,
                        runtime_session_id=runtime_session_id,
                        scope_hash=scope_hash,
                        model_id=str(fast_model.id),
                        response=response,
                    )
                    return response
                # No exception, but the model produced no usable suggestions
                # (e.g. a reasoning model spent its budget before emitting JSON).
                llm_skipped_reason = LLM_SKIPPED_MODEL_EMPTY
            except Exception as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise
                llm_skipped_reason = LLM_SKIPPED_MODEL_ERROR
                # Warning, not info: reaching this path means the configured
                # model produced unusable output (or the call failed), which
                # is a model-quality signal worth monitoring.
                logger.warning(
                    "Falling back to local runtime-session optimizations",
                    exc_info=True,
                )
        response = RuntimeSessionOptimizationResponse(
            generated_by="local",
            fast_model_name=fast_model.name if fast_model else None,
            model_id=str(fast_model.id) if fast_model else None,
            model_name=fast_model.name if fast_model else None,
            generated_at=datetime.now(timezone.utc),
            llm_skipped_reason=llm_skipped_reason,
            suggestions=suggestions,
        )
        self._finalize_response(
            response, account=account, summary=summary, profile=profile
        )
        # Cache the deterministic fallback too. Without this, sessions whose
        # model pass is unavailable (capped, errored, or no default model) are
        # never persisted, so the panel finds no cached result and re-prompts to
        # regenerate on every open.
        self._store_cached_response(
            account=account,
            runtime_session_id=runtime_session_id,
            scope_hash=scope_hash,
            model_id=str(fast_model.id) if fast_model else None,
            response=response,
        )
        return response

    def _finalize_response(
        self,
        response: RuntimeSessionOptimizationResponse,
        *,
        account: Account,
        summary: RuntimeSessionSummary,
        profile: Optional[SessionContextProfile],
    ) -> None:
        """Attach actions, waste score, and profile data to one response.

        Args:
            response: Response to enrich in place.
            account: Owning account (for managed-agent lookups).
            summary: Session summary backing the suggestions.
            profile: Measured context profile, if available.
        """
        response.context_profile = (
            profile.model_dump(exclude_none=True) if profile else None
        )
        response.waste_score = self._compute_waste_score(summary, profile)
        # Baseline totals for the analyzed scope so the UI can show savings as a
        # share of a coherent "before". The scope may be a subset of the whole
        # session, so apportion the session cost by token share rather than
        # showing the full-session cost against a partial-scope saving.
        scope_tokens = 0
        if profile is not None:
            scope_tokens = int(profile.total_prompt_tokens) + int(
                profile.total_completion_tokens
            )
        response.analyzed_scope_total_tokens = scope_tokens
        self._apply_savings_rollup(
            response, summary=summary, profile=profile, scope_tokens=scope_tokens
        )
        session_tokens = (
            int(summary.token_usage.total_tokens or 0) if summary.token_usage else 0
        )
        session_cost = float(summary.estimated_cost or 0.0)
        if scope_tokens and session_tokens:
            share = min(scope_tokens / session_tokens, 1.0)
            response.analyzed_scope_estimated_cost = round(session_cost * share, 6)
        else:
            response.analyzed_scope_estimated_cost = round(session_cost, 6)
        self._attach_suggestion_actions(
            response.suggestions, account=account, summary=summary, profile=profile
        )

    @staticmethod
    def _apply_savings_rollup(
        response: RuntimeSessionOptimizationResponse,
        *,
        summary: RuntimeSessionSummary,
        profile: Optional[SessionContextProfile],
        scope_tokens: int,
    ) -> None:
        """Set the headline savings totals from a deduped, bounded roll-up.

        Suggestions are overlapping lenses on the same measured tokens, so
        summing them double-counts. When a profile is available the total comes
        from :func:`compute_profile_savings`; otherwise the suggestion sum is
        used as a fallback. Either way the result is clamped to the analyzed
        scope, because no optimization can recover more tokens than were
        analyzed. A clamp is logged rather than applied silently.

        Args:
            response: Response to populate in place.
            summary: Stored usage totals, used for the fallback bound and cost.
            profile: Measured context profile, if available.
            scope_tokens: Total tokens in the analyzed scope.
        """
        suggestion_sum = sum(
            suggestion.expected_savings_tokens for suggestion in response.suggestions
        )
        if profile is not None:
            breakdown = compute_profile_savings(profile)
            total_tokens = breakdown.total_tokens
            if breakdown.clamped:
                logger.warning(
                    "Clamped runtime-session savings estimate to analyzed scope: "
                    "raw components exceeded scope (scope=%s tokens, "
                    "suggestion_sum=%s tokens, session=%s)",
                    breakdown.scope_tokens,
                    suggestion_sum,
                    getattr(profile, "session_id", "unknown"),
                )
        else:
            total_tokens = suggestion_sum

        session_tokens = (
            int(summary.token_usage.total_tokens or 0) if summary.token_usage else 0
        )
        # Invariant: estimated savings can never exceed the tokens they were
        # measured against. Fall back to session totals when no scope is known.
        bound = scope_tokens or session_tokens
        if bound and total_tokens > bound:
            logger.warning(
                "Runtime-session savings estimate %s exceeded analyzed scope %s; "
                "clamping to scope",
                total_tokens,
                bound,
            )
            total_tokens = bound

        response.potential_savings_tokens = max(total_tokens, 0)
        session_cost = float(summary.estimated_cost or 0.0)
        cost_per_token = session_cost / session_tokens if session_tokens else 0.0
        response.potential_savings_usd = round(
            response.potential_savings_tokens * cost_per_token, 6
        )

    @staticmethod
    def _compute_waste_score(
        summary: RuntimeSessionSummary,
        profile: Optional[SessionContextProfile],
    ) -> Optional[int]:
        """Score measured waste as a share of prompt-side tokens (0-100).

        Args:
            summary: Stored usage totals for the session.
            profile: Measured context profile.

        Returns:
            Waste score, or ``None`` when no profile was measurable.
        """
        if profile is None or profile.analyzed_event_count == 0:
            return None
        # Use the same deduped roll-up as the headline savings figure so the
        # waste score and the token total can never disagree.
        wasted = compute_profile_savings(profile).total_tokens
        denominator = max(
            profile.total_prompt_tokens,
            (summary.token_usage.prompt_tokens if summary.token_usage else 0) or 0,
            1,
        )
        return min(100, round(100 * wasted / denominator))

    def _attach_suggestion_actions(
        self,
        suggestions: list[RuntimeSessionOptimizationSuggestion],
        *,
        account: Account,
        summary: RuntimeSessionSummary,
        profile: Optional[SessionContextProfile],
    ) -> None:
        """Attach a one-click applyable action to each suggestion in place.

        Suggestions map to subject-scoped governance (``scope_tools``), a
        scoped budget policy (``set_budget``), or a replay deep-link
        (``open_events``) when only evidence is available.

        Args:
            suggestions: Suggestions to enrich.
            account: Owning account for managed-agent lookup.
            summary: Session summary providing the principal scope.
            profile: Measured context profile (for unused tool names).
        """
        agent = crud_managed_agent.get_by_source(
            self.db,
            account_id=str(account.id),
            session_source_type=summary.session_source_type,
            session_source_id=summary.session_source_id,
        )
        # Per-run sessions carry a derived source id of the form
        # "<base>:<run-id>", but the managed_agent row stores the BASE id. When
        # the direct lookup misses and the id is run-scoped, retry with the base
        # id so scope_tools / output-filter actions still resolve an agent.
        if agent is None and ":" in (summary.session_source_id or ""):
            base_source_id = summary.session_source_id.rsplit(":", 1)[0]
            agent = crud_managed_agent.get_by_source(
                self.db,
                account_id=str(account.id),
                session_source_type=summary.session_source_type,
                session_source_id=base_source_id,
            )
        agent_id = str(agent.id) if agent is not None else None
        unused_tools = (
            list(profile.tool_schema_overhead.unused_tool_names)
            if profile and profile.tool_schema_overhead
            else []
        )
        partition = self._partition_unused_for_account(account, profile)
        tier1_builtins = list(partition.tier1_builtin_names) if partition else []
        scope_tools = (
            list(partition.scope_raw_names) if partition is not None else unused_tools
        )
        suggested_budget = max(
            round(summary.estimated_cost * BUDGET_SUGGESTION_MULTIPLIER, 2), 1.0
        )
        oversized_output_tokens = 0
        if profile and profile.tool_bloat and profile.tool_bloat.largest_outputs:
            oversized_output_tokens = max(
                item.estimated_tokens for item in profile.tool_bloat.largest_outputs
            )
        top_oversized_field = (
            profile.tool_bloat.oversized_output_fields[0]
            if profile
            and profile.tool_bloat
            and profile.tool_bloat.oversized_output_fields
            else None
        )
        for suggestion in suggestions:
            if suggestion.action is not None:
                continue
            if suggestion.id == "dedupe-tool-outputs" and agent_id:
                suggestion.action = RuntimeSessionOptimizationActionSpec(
                    type=ACTION_ENABLE_COMPRESSION,
                    params={
                        "subject_type": SUBJECT_TYPE_MANAGED_AGENTS,
                        "subject_id": agent_id,
                        "subject_name": summary.runtime_principal_name
                        or summary.session_source_type,
                        "dedupe_tool_results": True,
                        "strip_noise": True,
                    },
                )
                continue
            if (
                suggestion.id == "trim-context"
                and agent_id
                and oversized_output_tokens * 4 > DEFAULT_TOOL_RESULT_CAP_CHARS
            ):
                suggestion.action = RuntimeSessionOptimizationActionSpec(
                    type=ACTION_CAP_TOOL_RESULTS,
                    params={
                        "subject_type": SUBJECT_TYPE_MANAGED_AGENTS,
                        "subject_id": agent_id,
                        "subject_name": summary.runtime_principal_name
                        or summary.session_source_type,
                        "max_tool_result_chars": max(
                            DEFAULT_TOOL_RESULT_CAP_CHARS, MIN_TOOL_RESULT_CAP
                        ),
                    },
                )
                continue
            if (
                suggestion.id == "filter-tool-output"
                and top_oversized_field is not None
            ):
                suggestion.action = RuntimeSessionOptimizationActionSpec(
                    type=ACTION_MANAGE_OUTPUT_FILTER,
                    params={
                        "server_name": top_oversized_field.server_name,
                        "tool_name": top_oversized_field.tool_name,
                        "suggested_fields": list(top_oversized_field.field_names),
                        "managed_agent_id": agent_id,
                    },
                )
                continue
            if suggestion.id == "disable-builtin-tools" and tier1_builtins:
                suggestion.action = RuntimeSessionOptimizationActionSpec(
                    type=ACTION_DISABLE_BUILTIN_TOOLS,
                    params={"tool_names": tier1_builtins},
                )
                continue
            if suggestion.id == "scope-tools" and agent_id and scope_tools:
                suggestion.action = RuntimeSessionOptimizationActionSpec(
                    type=ACTION_SCOPE_TOOLS,
                    params={
                        "subject_type": SUBJECT_TYPE_MANAGED_AGENTS,
                        "subject_id": agent_id,
                        "subject_name": summary.runtime_principal_name
                        or summary.session_source_type,
                        "tool_names": scope_tools,
                    },
                )
            elif suggestion.id == "budget-guardrail" and agent_id:
                suggestion.action = RuntimeSessionOptimizationActionSpec(
                    type=ACTION_SET_BUDGET,
                    params={
                        "subject_type": BUDGET_SUBJECT_MANAGED_AGENT,
                        "subject_id": agent_id,
                        "subject_name": summary.runtime_principal_name
                        or summary.session_source_type,
                        "period": "daily",
                        "hard_limit_usd": suggested_budget,
                    },
                )
            else:
                # Model-generated suggestions use the model's own ids/titles, so
                # they never match the deterministic id checks above. Infer an
                # applyable action from the suggestion text + measured profile so
                # their buttons work like the deterministic ones; fall back to a
                # replay deep-link when only evidence is available.
                inferred = self._infer_model_suggestion_action(
                    suggestion,
                    agent_id=agent_id,
                    unused_tools=scope_tools,
                    tier1_builtins=tier1_builtins,
                    top_oversized_field=top_oversized_field,
                    summary=summary,
                    suggested_budget=suggested_budget,
                )
                if inferred is not None:
                    suggestion.action = inferred
                elif suggestion.evidence_event_ids:
                    suggestion.action = RuntimeSessionOptimizationActionSpec(
                        type=ACTION_OPEN_EVENTS,
                        params={"event_ids": suggestion.evidence_event_ids},
                    )

    def _infer_model_suggestion_action(
        self,
        suggestion: RuntimeSessionOptimizationSuggestion,
        *,
        agent_id: Optional[str],
        unused_tools: list[str],
        tier1_builtins: list[str],
        top_oversized_field: Optional[Any],
        summary: RuntimeSessionSummary,
        suggested_budget: float,
    ) -> Optional[RuntimeSessionOptimizationActionSpec]:
        """Infer an applyable action for a model-authored suggestion by keyword.

        Model suggestions carry the model's own ids, so they miss the id-based
        action mapping. This matches the suggestion's title/description against
        the measured profile to attach the same concrete action a matching
        deterministic suggestion would carry.

        Args:
            suggestion: The model-authored suggestion.
            agent_id: Resolved managed-agent id, if any.
            unused_tools: Advertised-but-unused tool names left for scope_tools
                after carving out tier-1 builtins.
            tier1_builtins: Bare builtin names with zero account-wide
                invocations (account-wide disable candidates).
            top_oversized_field: Bulkiest droppable tool-output field, if any.
            summary: Session summary (for subject naming).
            suggested_budget: Suggested daily hard budget in USD.

        Returns:
            An action spec, or ``None`` when nothing confidently matches.
        """
        text = f"{suggestion.title} {suggestion.description}".lower()
        subject_name = summary.runtime_principal_name or summary.session_source_type
        # Prefix-stability / caching suggestions often mention "tool schemas" as
        # part of the cached prefix; they must NOT be treated as tool-removal.
        # Leave them to the evidence deep-link (open_events).
        is_caching = any(
            kw in text for kw in ("cache", "caching", "prefix", "stabiliz")
        )
        # Trim/filter an oversized tool-output field. Require the word "field"
        # so tool-*definition* suggestions ("trim unused tools") don't match the
        # output-field filter — they belong to scope_tools below.
        if top_oversized_field is not None and "field" in text:
            return RuntimeSessionOptimizationActionSpec(
                type=ACTION_MANAGE_OUTPUT_FILTER,
                params={
                    "server_name": top_oversized_field.server_name,
                    "tool_name": top_oversized_field.tool_name,
                    "suggested_fields": list(top_oversized_field.field_names),
                    "managed_agent_id": agent_id,
                },
            )
        # Account-wide disable of unused Preloop builtins when tier-1 evidence
        # exists and the suggestion text targets builtins / Preloop tools.
        targets_builtins = any(kw in text for kw in ("builtin", "built-in", "preloop"))
        if (
            not is_caching
            and tier1_builtins
            and targets_builtins
            and "tool" in text
            and any(
                kw in text
                for kw in ("unused", "advertised", "disable", "remove", "never")
            )
        ):
            return RuntimeSessionOptimizationActionSpec(
                type=ACTION_DISABLE_BUILTIN_TOOLS,
                params={"tool_names": tier1_builtins},
            )
        # Disable advertised-but-unused tools (never for caching suggestions).
        if (
            not is_caching
            and agent_id
            and unused_tools
            and "tool" in text
            and any(
                kw in text
                for kw in (
                    "unused",
                    "advertised",
                    "disable",
                    "remove",
                    "scope",
                    "never",
                )
            )
        ):
            return RuntimeSessionOptimizationActionSpec(
                type=ACTION_SCOPE_TOOLS,
                params={
                    "subject_type": SUBJECT_TYPE_MANAGED_AGENTS,
                    "subject_id": agent_id,
                    "subject_name": subject_name,
                    "tool_names": unused_tools,
                },
            )
        # Cap a daily budget.
        if agent_id and any(
            kw in text for kw in ("budget", "spend cap", "hard limit", "cost limit")
        ):
            return RuntimeSessionOptimizationActionSpec(
                type=ACTION_SET_BUDGET,
                params={
                    "subject_type": BUDGET_SUBJECT_MANAGED_AGENT,
                    "subject_id": agent_id,
                    "subject_name": subject_name,
                    "period": "daily",
                    "hard_limit_usd": suggested_budget,
                },
            )
        return None

    @staticmethod
    def _scope_hash(
        request: RuntimeSessionOptimizationRequest, model: Optional[AIModel]
    ) -> str:
        """Stable hash of the request scope and generation model.

        Args:
            request: Optimization request with scope controls.
            model: Resolved generation model, if any.

        Returns:
            Hex digest identifying one cacheable scope.
        """
        scope = {
            "event_ids": sorted(request.event_ids or []),
            "source_kinds": sorted(request.source_kinds or []),
            "from_index": request.from_index,
            "to_index": request.to_index,
            "model_id": str(model.id) if model else None,
        }
        return hashlib.sha256(
            json.dumps(scope, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _daily_optimization_cap_reached(self, account: Account) -> bool:
        """Check whether today's optimization model spend exceeds the cap."""
        cap = max(float(settings.billing_session_optimization_daily_cap_usd), 0.0)
        if cap <= 0:
            return False
        day_start = datetime.combine(
            datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc
        )
        spend = crud_api_usage.get_gateway_spend(
            self.db,
            account_id=str(account.id),
            start=day_start,
            purpose=OPTIMIZATION_USAGE_PURPOSE,
        )
        return spend >= cap

    def _load_cached_response(
        self,
        *,
        account: Account,
        runtime_session_id: str,
        scope_hash: str,
    ) -> Optional[RuntimeSessionOptimizationResponse]:
        """Return a cached optimization response when the cache table is available."""
        try:
            cached = crud_runtime_session_optimization_result.get_by_scope(
                self.db,
                account_id=account.id,
                runtime_session_id=runtime_session_id,
                scope_hash=scope_hash,
            )
        except SQLAlchemyError:
            logger.warning(
                "Optimization cache lookup failed; continuing without cache",
                exc_info=True,
            )
            return None
        if cached is None:
            return None
        response = RuntimeSessionOptimizationResponse.model_validate(cached.response)
        response.from_cache = True
        return response

    def _load_latest_cached_response(
        self,
        *,
        account: Account,
        runtime_session_id: str,
    ) -> Optional[RuntimeSessionOptimizationResponse]:
        """Return the most recent cached result for a session, any scope.

        Used by the cache-only panel-open load so previously generated
        suggestions appear without re-running generation, even if the exact
        request scope differs from when they were generated.

        Args:
            account: Owning account.
            runtime_session_id: Runtime session to look up.

        Returns:
            The latest cached response, or ``None`` when none is stored.
        """
        try:
            rows = crud_runtime_session_optimization_result.list_for_sessions(
                self.db,
                account_id=account.id,
                runtime_session_ids=[runtime_session_id],
            )
        except SQLAlchemyError:
            logger.warning(
                "Optimization latest-cache lookup failed; continuing",
                exc_info=True,
            )
            return None
        if not rows or not isinstance(rows[0].response, dict):
            return None
        response = RuntimeSessionOptimizationResponse.model_validate(rows[0].response)
        response.from_cache = True
        return response

    def _store_cached_response(
        self,
        *,
        account: Account,
        runtime_session_id: str,
        scope_hash: str,
        model_id: Optional[str],
        response: RuntimeSessionOptimizationResponse,
    ) -> None:
        """Persist a generated response so panel re-opens are free."""
        try:
            crud_runtime_session_optimization_result.upsert(
                self.db,
                account_id=account.id,
                runtime_session_id=runtime_session_id,
                scope_hash=scope_hash,
                model_id=model_id,
                response=json.loads(response.model_dump_json()),
            )
        except Exception:
            logger.warning(
                "Failed to cache session optimization response", exc_info=True
            )

    def _builtin_catalog_names(self) -> set[str]:
        """Return bare names from the BUILTIN_TOOLS catalog."""
        from preloop.api.endpoints.tools import BUILTIN_TOOLS

        return {
            str(tool["name"])
            for tool in BUILTIN_TOOLS
            if isinstance(tool, dict) and tool.get("name")
        }

    def _builtin_catalog_by_name(self) -> dict[str, dict[str, Any]]:
        """Return BUILTIN_TOOLS keyed by bare name."""
        from preloop.api.endpoints.tools import BUILTIN_TOOLS

        return {
            str(tool["name"]): tool
            for tool in BUILTIN_TOOLS
            if isinstance(tool, dict) and tool.get("name")
        }

    def _partition_unused_for_account(
        self,
        account: Optional[Account],
        profile: Optional[SessionContextProfile],
    ) -> Optional[UnusedBuiltinPartition]:
        """Tier unused tools using account-wide invocation stats.

        Args:
            account: Owning account; when missing, no partition is produced.
            profile: Session context profile with unused tool names.

        Returns:
            Partition, or ``None`` when there is nothing to split.
        """
        if account is None or profile is None or profile.tool_schema_overhead is None:
            return None
        unused = list(profile.tool_schema_overhead.unused_tool_names)
        if not unused:
            return None
        unused_tokens = dict(profile.tool_schema_overhead.unused_tool_tokens or {})
        # Backfill tokens when older cached profiles omit the per-tool map.
        unused_total = profile.tool_schema_overhead.unused_schema_tokens_estimate
        if not unused_tokens and unused_total:
            # Fall back to equal split only for action attachment; suggestion
            # builders always see fresh profiles with unused_tool_tokens.
            per = unused_total // max(len(unused), 1)
            unused_tokens = {name: per for name in unused}
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=BUILTIN_USAGE_WINDOW_DAYS)
            usage_rows = ToolUsageStatsService(self.db).get_account_usage_by_tool(
                account_id=str(account.id),
                start_date=start,
                end_date=end,
            )
            counts = invocation_counts_by_tool(usage_rows)
        except Exception:
            logger.warning(
                "Failed to load account tool usage for builtin disable tiering; "
                "skipping account-wide disable recommendation",
                exc_info=True,
            )
            # Without corroboration, keep everything on the agent-scoped path.
            return UnusedBuiltinPartition(
                tier1_raw_names=[],
                tier1_builtin_names=[],
                scope_raw_names=unused,
                tier1_session_tokens=0,
                scope_session_tokens=int(
                    profile.tool_schema_overhead.unused_schema_tokens_estimate or 0
                ),
            )
        return partition_unused_tools(
            unused,
            builtin_names=self._builtin_catalog_names(),
            unused_tool_tokens=unused_tokens,
            invocation_counts=counts,
        )

    def _catalog_schema_tokens_estimate(
        self, tool_name: str, *, account: Optional[Account]
    ) -> int:
        """Estimate one-request schema tokens for a builtin via PR #140 helper.

        Args:
            tool_name: Bare builtin name.
            account: Optional account for justification_mode lookup.

        Returns:
            Estimated schema tokens including justification injection when set.
        """
        catalog = self._builtin_catalog_by_name()
        meta = catalog.get(tool_name)
        if meta is None:
            return 0
        justification_mode: Optional[str] = None
        if account is not None:
            existing = crud_tool_configuration.get_by_tool_name_and_source(
                self.db,
                account_id=str(account.id),
                tool_name=tool_name,
                tool_source="builtin",
            )
            if existing is not None:
                justification_mode = existing.justification_mode
        return estimate_tool_schema_tokens(
            name=tool_name,
            description=str(meta.get("description") or ""),
            schema=meta.get("schema") if isinstance(meta.get("schema"), dict) else None,
            justification_mode=justification_mode,
        )

    def _local_optimization_suggestions(
        self,
        summary: RuntimeSessionSummary,
        profile: Optional[SessionContextProfile] = None,
        *,
        account: Optional[Account] = None,
    ) -> list[RuntimeSessionOptimizationSuggestion]:
        """Build deterministic suggestions grounded in measured context waste.

        Args:
            summary: Stored usage totals for the session.
            profile: Evidence-grounded context profile; savings estimates come
                from its measured token counts instead of fixed multipliers.
            account: Owning account; used to corroborate account-wide builtin
                disable recommendations against 30-day tool usage stats.

        Returns:
            Suggestions ordered by expected token savings.
        """
        suggestions: list[RuntimeSessionOptimizationSuggestion] = []
        token_usage = summary.token_usage
        total_tokens = max(
            int(token_usage.total_tokens or 0) if token_usage is not None else 0,
            1,
        )
        prompt_tokens = (
            int(token_usage.prompt_tokens or 0) if token_usage is not None else 0
        )
        cost_per_token = float(summary.estimated_cost or 0.0) / total_tokens
        tool_bloat = profile.tool_bloat if profile else None
        cache_profile = profile.cache_profile if profile else None
        retry_profile = profile.retry_profile if profile else None
        schema_overhead = profile.tool_schema_overhead if profile else None
        partition = self._partition_unused_for_account(account, profile)

        duplicate_tokens = tool_bloat.duplicate_output_tokens if tool_bloat else 0
        compressible_tokens = (
            max(tool_bloat.compressible_tokens_estimate - duplicate_tokens, 0)
            if tool_bloat
            else 0
        )
        if duplicate_tokens > 0 and tool_bloat is not None:
            suggestions.append(
                RuntimeSessionOptimizationSuggestion(
                    id="dedupe-tool-outputs",
                    title="Deduplicate repeated tool outputs",
                    description=(
                        "Identical tool outputs appear multiple times in the "
                        "request context (e.g. the same file read twice). "
                        "Reuse prior results instead of re-fetching them."
                    ),
                    expected_savings_tokens=duplicate_tokens,
                    expected_savings_usd=round(duplicate_tokens * cost_per_token, 6),
                    confidence="high",
                    action_label="Inspect duplicated outputs",
                    evidence=[f"{duplicate_tokens} duplicated tool-output tokens"],
                    evidence_event_ids=tool_bloat.duplicate_event_ids,
                )
            )
        if prompt_tokens / total_tokens > 0.75 or compressible_tokens > 0:
            trim_evidence = [
                f"{prompt_tokens} prompt tokens",
                f"{round((prompt_tokens / total_tokens) * 100)}% prompt-side",
            ]
            trim_event_ids: list[str] = []
            if compressible_tokens > 0 and tool_bloat is not None:
                trim_evidence.append(
                    f"{compressible_tokens} compressible tool-output tokens"
                )
                trim_event_ids = sorted(
                    {item.event_id for item in tool_bloat.largest_outputs}
                )
            # Name the single biggest concrete offender so the suggestion is
            # actionable ("the X tool returned N tokens") instead of generic
            # advice to "review context."
            top_output = (
                tool_bloat.largest_outputs[0]
                if tool_bloat and tool_bloat.largest_outputs
                else None
            )
            if top_output is not None and top_output.estimated_tokens > 0:
                trim_title = f"Cap '{top_output.tool_name}' output and trim context"
                trim_description = (
                    f"The '{top_output.tool_name}' tool returned "
                    f"{top_output.estimated_tokens:,} tokens in a single call — "
                    "large retrieved-context payloads like this dominate the "
                    "prompt cost. Cap or summarize big tool results before they "
                    "enter the context, and trim static system/skill/memory text."
                )
            else:
                trim_title = "Trim prompt context"
                trim_description = (
                    "Most of this session's tokens were prompt-side. Review "
                    "system instructions, tool schemas, skills, memory, and "
                    "retrieved context before the next run."
                )
            suggestions.append(
                RuntimeSessionOptimizationSuggestion(
                    id="trim-context",
                    title=trim_title,
                    description=trim_description,
                    expected_savings_tokens=compressible_tokens,
                    expected_savings_usd=round(compressible_tokens * cost_per_token, 6),
                    confidence="high" if compressible_tokens else "medium",
                    action_label="Review context segments",
                    evidence=trim_evidence,
                    evidence_event_ids=trim_event_ids,
                )
            )
        oversized_fields = tool_bloat.oversized_output_fields if tool_bloat else []
        if oversized_fields:
            top_field = oversized_fields[0]
            field_label = ", ".join(top_field.field_names[:3])
            tool_label = (
                f"{top_field.server_name}/{top_field.tool_name}"
                if top_field.server_name
                else top_field.tool_name
            )
            suggestions.append(
                RuntimeSessionOptimizationSuggestion(
                    id="filter-tool-output",
                    title=f"Filter unused fields from {top_field.tool_name} output",
                    description=(
                        f"The '{tool_label}' tool returns bulky field(s) "
                        f"({field_label}) worth ~"
                        f"{top_field.estimated_tokens:,} tokens that are carried "
                        "into the prompt on every turn. If the agent never uses "
                        "them, add an output filter to drop these fields before "
                        "they enter the context."
                    ),
                    expected_savings_tokens=top_field.estimated_tokens,
                    expected_savings_usd=round(
                        top_field.estimated_tokens * cost_per_token, 6
                    ),
                    confidence="medium",
                    action_label="Filter tool output",
                    evidence=[
                        f"Tool: {tool_label}",
                        "Bulky fields: " + ", ".join(top_field.field_names[:5]),
                        f"~{top_field.estimated_tokens:,} tokens in these fields",
                    ],
                )
            )
        if schema_overhead and schema_overhead.unused_schema_tokens_estimate > 0:
            # Carve unused Preloop builtins with zero account-wide invocations
            # into a separate account-wide disable suggestion so the same
            # schema tokens are never counted twice (#146).
            tier1_names = partition.tier1_builtin_names if partition else []
            tier1_tokens = partition.tier1_session_tokens if partition else 0
            scope_names = (
                partition.scope_raw_names
                if partition is not None
                else list(schema_overhead.unused_tool_names)
            )
            scope_tokens = (
                partition.scope_session_tokens
                if partition is not None
                else schema_overhead.unused_schema_tokens_estimate
            )
            if tier1_names and tier1_tokens > 0:
                evidence = [
                    (
                        f"Account-wide window: last {BUILTIN_USAGE_WINDOW_DAYS} "
                        "days; invocation_count = 0 for each listed builtin"
                    ),
                ]
                for bare_name in tier1_names[:8]:
                    catalog_tokens = self._catalog_schema_tokens_estimate(
                        bare_name, account=account
                    )
                    evidence.append(
                        (
                            f"{bare_name}: invocation_count = 0; "
                            f"schema_tokens_estimate = {catalog_tokens}"
                        )
                    )
                suggestions.append(
                    RuntimeSessionOptimizationSuggestion(
                        id="disable-builtin-tools",
                        title="Disable unused Preloop builtin tools",
                        description=(
                            f"{len(tier1_names)} Preloop builtin tool(s) were "
                            "advertised this session but have zero invocations "
                            f"account-wide over the last "
                            f"{BUILTIN_USAGE_WINDOW_DAYS} days, yet their "
                            "schemas are re-sent on every request, costing ~"
                            f"{tier1_tokens:,} tokens across "
                            f"{max(schema_overhead.resend_count, 1)} request(s) "
                            f"({', '.join(tier1_names[:4])}). Disable them "
                            "account-wide so every agent stops paying the "
                            "schema tax; re-enable anytime on the Tools page."
                        ),
                        # NOT multiplied by resend_count: already resend-inclusive.
                        expected_savings_tokens=tier1_tokens,
                        expected_savings_usd=round(tier1_tokens * cost_per_token, 6),
                        confidence="high",
                        action_label="Disable builtins account-wide",
                        evidence=evidence,
                    )
                )
            if scope_names and scope_tokens > 0:
                suggestions.append(
                    RuntimeSessionOptimizationSuggestion(
                        id="scope-tools",
                        title="Scope down advertised tools",
                        description=(
                            f"{len(scope_names)} of "
                            f"{schema_overhead.advertised_tools} advertised tools "
                            "were never called this session yet their schemas are "
                            "re-sent on every request, costing ~"
                            f"{scope_tokens:,} tokens "
                            f"across {max(schema_overhead.resend_count, 1)} request(s)"
                            + (
                                " (" + ", ".join(scope_names[:4]) + ")"
                                if scope_names
                                else ""
                            )
                            + ". Restrict tool visibility for this agent."
                        ),
                        # NOT multiplied by resend_count: analyze_tool_schema_overhead
                        # already accumulates each advertised schema once per request
                        # that carried it, so this estimate is resend-inclusive.
                        # Multiplying again made the figure quadratic in request count
                        # and pushed reported savings above 100% of analyzed tokens.
                        expected_savings_tokens=scope_tokens,
                        expected_savings_usd=round(scope_tokens * cost_per_token, 6),
                        confidence="high",
                        action_label="Scope tool access",
                        evidence=[
                            (
                                f"{len(scope_names)} of "
                                f"{schema_overhead.advertised_tools} advertised "
                                "tools never invoked"
                            ),
                            "Unused: " + ", ".join(scope_names[:5]),
                        ],
                    )
                )
        if cache_profile and cache_profile.cache_breaking_events:
            breaking = cache_profile.cache_breaking_events
            wasted_prefix_tokens = cache_profile.avg_repeated_prefix_tokens * len(
                breaking
            )
            suggestions.append(
                RuntimeSessionOptimizationSuggestion(
                    id="stabilize-prefix",
                    title="Stabilize the request prefix for cache hits",
                    description=(
                        "Requests in this session mutate the shared message "
                        "prefix, which prevents provider prompt caching from "
                        "applying to the repeated context."
                    ),
                    expected_savings_tokens=wasted_prefix_tokens,
                    expected_savings_usd=round(
                        wasted_prefix_tokens * cost_per_token, 6
                    ),
                    confidence="medium",
                    action_label="Inspect cache-breaking requests",
                    evidence=[
                        f"{len(breaking)} cache-breaking requests",
                        "Reasons: "
                        + ", ".join(sorted({event.reason_hint for event in breaking})),
                    ],
                    evidence_event_ids=[event.event_id for event in breaking],
                )
            )
        if cache_profile and cache_profile.idle_expiry_events:
            idle_events = cache_profile.idle_expiry_events
            idle_tokens = cache_profile.measured_idle_expiry_tokens
            # Write-vs-read differential from catalog prices x ApiUsage
            # cache_creation_tokens. Never invent a USD figure from session
            # average cost when catalog prices are missing.
            idle_cost = round(cache_profile.measured_idle_expiry_extra_cost_usd, 6)
            priced_events = sum(
                1 for event in idle_events if event.measured_extra_cost_usd is not None
            )
            suggestions.append(
                RuntimeSessionOptimizationSuggestion(
                    id="reduce-idle-cache-expiry",
                    title="Avoid idle prompt-cache expiry",
                    description=(
                        "This session paused long enough for the provider "
                        "prompt cache to expire, then re-paid cache WRITE on "
                        "an unchanged prefix. Keep the session warmer than "
                        "the provider TTL, or use a longer cache TTL where "
                        "the provider supports it."
                    ),
                    expected_savings_tokens=idle_tokens,
                    expected_savings_usd=idle_cost,
                    confidence="high",
                    action_label="Inspect idle cache expiries",
                    evidence=[
                        (
                            f"{len(idle_events)} idle cache "
                            f"expir{'y' if len(idle_events) == 1 else 'ies'}"
                        ),
                        f"{idle_tokens} tokens re-written after TTL lapse",
                        (
                            f"measured extra cost ${idle_cost:.4f} "
                            f"({priced_events}/{len(idle_events)} expiries priced)"
                            if priced_events
                            else "extra cost unavailable (no catalog cache prices)"
                        ),
                    ],
                    evidence_event_ids=[event.event_id for event in idle_events],
                )
            )
        if summary.failed_requests or (retry_profile and retry_profile.failed_requests):
            wasted_tokens = retry_profile.wasted_tokens if retry_profile else 0
            wasted_cost = retry_profile.wasted_cost_estimate if retry_profile else 0.0
            suggestions.append(
                RuntimeSessionOptimizationSuggestion(
                    id="fix-failures",
                    title="Fix failed requests before optimizing",
                    description=(
                        "Failed or denied gateway calls usually create retry "
                        "spend. Resolve the failure path before tuning context."
                    ),
                    expected_savings_tokens=wasted_tokens,
                    expected_savings_usd=round(
                        wasted_cost or wasted_tokens * cost_per_token, 6
                    ),
                    confidence="high",
                    action_label="Inspect failed requests",
                    evidence=[f"{summary.failed_requests} failed requests"]
                    + (
                        [f"{wasted_tokens} tokens spent on failures and retries"]
                        if wasted_tokens
                        else []
                    ),
                    evidence_event_ids=(
                        retry_profile.failure_event_ids if retry_profile else []
                    ),
                )
            )
        if not suggestions:
            suggestions.append(
                RuntimeSessionOptimizationSuggestion(
                    id="budget-guardrail",
                    title="Add a scoped budget guardrail",
                    description=(
                        "No obvious waste pattern was detected. A scoped budget "
                        "still protects future sessions from unexpected spend spikes."
                    ),
                    expected_savings_tokens=0,
                    expected_savings_usd=0.0,
                    confidence="medium",
                    action_label="Review budget policy",
                    evidence=[f"Current spend: ${summary.estimated_cost:.4f}"],
                )
            )
        suggestions.sort(
            key=lambda suggestion: suggestion.expected_savings_tokens, reverse=True
        )
        return suggestions

    def _resolve_optimization_model(
        self, *, account: Account, model_id: Optional[str]
    ) -> Optional[AIModel]:
        """Resolve the model used for the internal optimization LLM call.

        An explicit ``model_id`` is honored verbatim; otherwise the account's
        default active model is used. Reasoning models (the common default) are
        fine here as long as the token budget is generous enough for the hidden
        reasoning pass to complete — see the optimization call's ``max_tokens``.

        Args:
            account: Owning account.
            model_id: Optional explicit model id from the request.

        Returns:
            The resolved model, or ``None`` when the account has no usable
            default model.

        Raises:
            HTTPException: When an explicit ``model_id`` is invalid or not
                visible to the account.
        """
        if model_id:
            try:
                model = crud_ai_model.get(self.db, id=UUID(str(model_id)))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid model_id") from exc
            if model is None or (
                model.account_id is not None
                and str(model.account_id) != str(account.id)
            ):
                raise HTTPException(status_code=404, detail="AI model not found")
            return model
        return crud_ai_model.get_default_active_model(
            self.db, account_id=str(account.id)
        )

    @staticmethod
    def _scoped_payloads_from_events(
        events: list[GatewayCallEvent],
        max_payloads: int = DEFAULT_MAX_OPTIMIZATION_PAYLOADS,
    ) -> list[dict[str, Any]]:
        """Return recent gateway payloads from preloaded session events."""
        payloads = [
            event.payload for event in events if isinstance(event.payload, dict)
        ]
        bounded_limit = max(1, max_payloads)
        return payloads[-bounded_limit:]

    def _create_gateway_service(
        self,
        *,
        current_user: User,
        budget_enforcer: Any,
        runtime_session_id: str,
    ) -> OpenAIGatewayService:
        """Build a gateway service for the internal optimization call.

        The call is deliberately NOT attributed to the analyzed runtime session:
        it is Preloop's own meta-analysis traffic, not the agent's, so counting
        it against the session would inflate the session's tokens/cost/request
        metrics and clutter its transcript. Usage is still recorded at the
        account level (via ``purpose="session_optimization"``) for billing.
        The ``runtime_session_id`` arg is retained for signature compatibility.
        """
        auth_context = ModelGatewayAuthContext(token=NO_BEARER_TOKEN, user=current_user)
        gateway = OpenAIGatewayService(
            self.db,
            auth_context,
            budget_enforcer=budget_enforcer,
            skip_runtime_session_resolution=True,
        )
        return gateway

    def _call_optimization_model(
        self,
        *,
        model: AIModel,
        summary: RuntimeSessionSummary,
        profile: Optional[SessionContextProfile],
        scoped_payloads: list[dict[str, Any]],
        request: RuntimeSessionOptimizationRequest,
        current_user: User,
        budget_enforcer: Any,
        runtime_session_id: str,
    ) -> tuple[
        list[RuntimeSessionOptimizationSuggestion],
        GatewayTokenUsage,
        float,
    ]:
        context = self._build_optimization_digest(
            summary=summary,
            profile=profile,
            scoped_payloads=scoped_payloads,
            request=request,
        )
        runtime = resolve_ai_model_runtime(model)
        model_alias = (
            runtime.model_gateway_model_alias
            or f"{model.provider_name}/{model.model_identifier}"
        )
        gateway_payload: dict[str, Any] = {
            "model": model_alias,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Generate cost and reliability optimization suggestions "
                        "for a captured agent session. Return only JSON with key "
                        "suggestions, an array of objects with id, title, "
                        "description, expected_savings_tokens, expected_savings_usd, "
                        "confidence, action_label, evidence, evidence_event_ids. "
                        "Use only event ids present in the provided context "
                        "profile. Be specific and do not invent savings "
                        "unsupported by the provided usage data."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False),
                },
            ],
            "temperature": 0.1,
            # Budget for both a full suggestions array AND a reasoning model's
            # hidden reasoning pass (e.g. deepseek, the common account default).
            # Too tight and reasoning consumes the whole budget, leaving empty
            # content that silently degrades to deterministic-only suggestions.
            # The cap is generous but not costly: models stop at end-of-output,
            # so actual completion tokens stay ~1k even with this ceiling.
            "max_tokens": 16000,
            "response_format": {"type": "json_object"},
            "metadata": {
                "purpose": "session_optimization",
                "runtime_session_id": runtime_session_id,
            },
        }
        gateway = self._create_gateway_service(
            current_user=current_user,
            budget_enforcer=budget_enforcer,
            runtime_session_id=runtime_session_id,
        )
        try:
            response_payload = gateway.create_chat_completion(gateway_payload)
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            # Some providers reject response_format; retry once without it and
            # rely on the fenced-JSON fallback parsing instead.
            gateway_payload.pop("response_format", None)
            response_payload = gateway.create_chat_completion(gateway_payload)
        suggestions, token_usage, estimated_cost = (
            self._parse_optimization_model_response(model, response_payload)
        )
        suggestions = self._validate_model_suggestions(suggestions, profile)
        if not suggestions:
            raise ValueError("All model suggestions failed validation")
        return suggestions, token_usage, estimated_cost

    def _build_optimization_digest(
        self,
        *,
        summary: RuntimeSessionSummary,
        profile: Optional[SessionContextProfile],
        scoped_payloads: list[dict[str, Any]],
        request: RuntimeSessionOptimizationRequest,
    ) -> dict[str, Any]:
        """Build a budget-bounded digest; the model never sees raw payloads.

        Args:
            summary: Stored usage totals for the session.
            profile: Measured context profile (the primary model input).
            scoped_payloads: Scoped gateway payloads used only to extract a
                handful of pre-truncated offending snippets.
            request: Original optimization request (for scope echo).

        Returns:
            Digest dict whose serialized size stays within
            :data:`DIGEST_CHAR_BUDGET`; snippets are trimmed first, the
            profile is never dropped.
        """
        session_title = (
            summary.summary
            or summary.session_reference
            or summary.runtime_principal_name
            or summary.session_source_type
        )
        digest: dict[str, Any] = {
            "session": {
                "title": session_title,
                "requests": summary.total_requests,
                "failed_requests": summary.failed_requests,
                "tokens": summary.token_usage.model_dump(),
                "estimated_cost": summary.estimated_cost,
            },
            "scope": {
                "event_ids": (request.event_ids or [])[:100],
                "source_kinds": (request.source_kinds or [])[:10],
                "from_index": request.from_index,
                "to_index": request.to_index,
            },
            "context_profile": (
                profile.model_dump(exclude_none=True) if profile else None
            ),
            "snippets": self._collect_offending_snippets(scoped_payloads),
        }
        while (
            len(json.dumps(digest, ensure_ascii=False, default=str))
            > DIGEST_CHAR_BUDGET
            and digest["snippets"]
        ):
            digest["snippets"] = digest["snippets"][:-1]
        return digest

    def _collect_offending_snippets(
        self, scoped_payloads: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Pick the most useful pre-truncated content snippets for the model.

        Failure details first, then the largest request messages, each capped
        at :data:`SNIPPET_CHAR_LIMIT` characters and deduplicated.
        """
        snippets: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(kind: str, text: str) -> None:
            trimmed = text.strip()[:SNIPPET_CHAR_LIMIT]
            if not trimmed or trimmed in seen:
                return
            seen.add(trimmed)
            snippets.append({"kind": kind, "text": trimmed})

        for payload in scoped_payloads:
            error_detail = payload.get("error_detail")
            if isinstance(error_detail, str) and error_detail.strip():
                add("error_detail", error_detail)
        sized_messages: list[tuple[int, str]] = []
        for payload in scoped_payloads:
            for message in RuntimeSessionExplorerService._extract_request_messages(
                payload
            ):
                text = message.get("text") or ""
                sized_messages.append((len(text), f"{message.get('role')}: {text}"))
        sized_messages.sort(key=lambda item: item[0], reverse=True)
        for _, text in sized_messages:
            if len(snippets) >= MAX_DIGEST_SNIPPETS:
                break
            add("large_message", text)
        return snippets[:MAX_DIGEST_SNIPPETS]

    def _validate_model_suggestions(
        self,
        suggestions: list[RuntimeSessionOptimizationSuggestion],
        profile: Optional[SessionContextProfile],
    ) -> list[RuntimeSessionOptimizationSuggestion]:
        """Clamp savings claims to measured waste and drop bogus evidence ids.

        Args:
            suggestions: Parsed model suggestions.
            profile: Measured context profile used as the upper bound.

        Returns:
            Validated suggestions; claims above the measured waste budget are
            clamped and their confidence downgraded to "low".
        """
        if profile is None:
            return suggestions
        known_event_ids = {
            event_id for segment in profile.segments for event_id in segment.event_ids
        }
        if profile.retry_profile:
            known_event_ids.update(profile.retry_profile.failure_event_ids)
        waste_budget = profile.total_prompt_tokens
        if profile.tool_bloat:
            waste_budget = max(
                waste_budget, profile.tool_bloat.compressible_tokens_estimate
            )
        validated: list[RuntimeSessionOptimizationSuggestion] = []
        for suggestion in suggestions:
            update: dict[str, Any] = {}
            if suggestion.expected_savings_tokens > waste_budget:
                update["expected_savings_tokens"] = waste_budget
                update["confidence"] = "low"
            if suggestion.evidence_event_ids:
                valid_ids = [
                    event_id
                    for event_id in suggestion.evidence_event_ids
                    if event_id in known_event_ids
                ]
                if valid_ids != suggestion.evidence_event_ids:
                    update["evidence_event_ids"] = valid_ids
            validated.append(
                suggestion.model_copy(update=update) if update else suggestion
            )
        return validated

    @staticmethod
    def _as_str_list(value: Any, max_chars: int) -> list[str]:
        """Coerce a model field to a clean list of strings.

        Models sometimes return ``evidence`` as a single string rather than a
        list; iterating that string would split it into characters. This
        normalizes a string to a one-element list and filters non-string list
        items, truncating each to ``max_chars``.

        Args:
            value: Raw field value from the model response.
            max_chars: Per-item truncation length.

        Returns:
            A list of non-empty, truncated strings.
        """
        if isinstance(value, str):
            return [value[:max_chars]] if value.strip() else []
        if isinstance(value, list):
            return [
                str(item)[:max_chars]
                for item in value
                if isinstance(item, str) and item.strip()
            ]
        return []

    def _parse_optimization_model_response(
        self,
        model: AIModel,
        response_payload: dict[str, Any],
    ) -> tuple[
        list[RuntimeSessionOptimizationSuggestion],
        GatewayTokenUsage,
        float,
    ]:
        choices = response_payload.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        raw = (message.get("content") or "{}").strip()
        if raw.startswith("```"):
            fence_line, _, rest = raw.partition("\n")
            if rest:
                raw = rest
            else:
                # Single-line fenced payload (e.g. '```json{...}'): there is
                # no fence line to drop, so strip the backticks and optional
                # language tag glued to the JSON instead.
                raw = fence_line.lstrip("`").removeprefix("json")
            raw = raw.rsplit("```", 1)[0]
        parsed = json.loads(raw)
        raw_suggestions = (
            parsed.get("suggestions") if isinstance(parsed, dict) else None
        )
        if not isinstance(raw_suggestions, list):
            raise ValueError("Optimization model returned invalid suggestions")
        suggestions = [
            RuntimeSessionOptimizationSuggestion(
                id=str(item.get("id") or f"model-suggestion-{index}")[:80],
                title=str(item.get("title") or "Optimization suggestion")[:160],
                description=str(item.get("description") or "")[:1000],
                expected_savings_tokens=int(item.get("expected_savings_tokens") or 0),
                expected_savings_usd=float(item.get("expected_savings_usd") or 0.0),
                confidence=str(item.get("confidence") or "medium"),
                action_label=str(item.get("action_label") or "Review suggestion")[:80],
                evidence=self._as_str_list(item.get("evidence"), 240)[:6],
                evidence_event_ids=self._as_str_list(
                    item.get("evidence_event_ids"), 64
                )[:10],
            )
            for index, item in enumerate(raw_suggestions)
            if isinstance(item, dict)
        ][:6]
        if not suggestions:
            raise ValueError("Optimization model returned no suggestions")
        usage_data = response_payload.get("usage") or {}
        if not isinstance(usage_data, dict):
            usage_data = {}
        prompt_tokens = int(
            usage_data.get("prompt_tokens") or usage_data.get("input_tokens") or 0
        )
        completion_tokens = int(
            usage_data.get("completion_tokens") or usage_data.get("output_tokens") or 0
        )
        total_tokens = int(
            usage_data.get("total_tokens") or prompt_tokens + completion_tokens
        )
        token_usage = GatewayTokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        estimated_cost = (
            estimate_ai_model_usage_cost(
                model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                usage_details=usage_data,
            )
            or 0.0
        )
        return suggestions, token_usage, estimated_cost

    def apply_account_session_optimization_action(
        self,
        *,
        account: Account,
        runtime_session_id: str,
        request: RuntimeSessionOptimizationApplyRequest,
        current_user: User,
    ) -> RuntimeSessionOptimizationAppliedAction:
        """Apply one suggestion action against existing governance/budget APIs.

        Args:
            account: Owning account.
            runtime_session_id: Session the action is applied from.
            request: Suggestion id plus the action contract to apply.
            current_user: Applying user, recorded for the history tab.

        Returns:
            The persisted applied-action record.

        Raises:
            HTTPException: When the session is missing, the action type is
                not server-applicable, or parameters are invalid.
        """
        summary_row = crud_runtime_session.get_account_session_summary(
            self.db,
            account_id=str(account.id),
            runtime_session_id=runtime_session_id,
        )
        if summary_row is None:
            raise HTTPException(status_code=404, detail="Runtime session not found")
        summary = RuntimeSessionExplorerService._summary_row_to_schema(summary_row)
        action = request.action
        if action.type not in APPLYABLE_ACTION_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(f"Action type '{action.type}' cannot be applied server-side"),
            )
        if crud_runtime_session_optimization_action.exists_for_suggestion(
            self.db,
            account_id=account.id,
            runtime_session_id=runtime_session_id,
            suggestion_id=request.suggestion_id,
            action_type=action.type,
        ):
            raise HTTPException(
                status_code=409,
                detail="This suggestion was already applied for this session",
            )
        if action.type == ACTION_SCOPE_TOOLS:
            result = self._apply_scope_tools(account=account, params=action.params)
        elif action.type == ACTION_DISABLE_BUILTIN_TOOLS:
            result = self._apply_disable_builtin_tools(
                account=account, params=action.params
            )
        elif action.type in (ACTION_ENABLE_COMPRESSION, ACTION_CAP_TOOL_RESULTS):
            result = self._apply_context_optimization_settings(
                account=account, params=action.params
            )
        else:
            result = self._apply_set_budget(account=account, params=action.params)
        baseline = self._principal_baseline(account=account, summary=summary)
        row = crud_runtime_session_optimization_action.create_applied(
            self.db,
            account_id=account.id,
            runtime_session_id=runtime_session_id,
            suggestion_id=request.suggestion_id,
            suggestion_title=request.suggestion_title,
            action_type=action.type,
            params=action.params,
            applied_by=current_user.username,
            runtime_principal_id=summary.runtime_principal_id,
            baseline=baseline,
            result=result,
        )
        return self._applied_action_to_schema(row, account=account)

    def list_account_session_optimization_actions(
        self,
        *,
        account: Account,
        runtime_session_id: str,
    ) -> RuntimeSessionOptimizationActionListResponse:
        """Return applied actions for one session with measured outcomes.

        Args:
            account: Owning account.
            runtime_session_id: Runtime session id.

        Returns:
            Applied actions ordered latest-first, each with a measured
            outcome when post-apply traffic exists for the same principal.
        """
        rows = crud_runtime_session_optimization_action.list_for_session(
            self.db,
            account_id=account.id,
            runtime_session_id=runtime_session_id,
        )
        return RuntimeSessionOptimizationActionListResponse(
            items=[self._applied_action_to_schema(row, account=account) for row in rows]
        )

    def _apply_scope_tools(
        self, *, account: Account, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Disable the given tools via subject-scoped governance overrides.

        Args:
            account: Account whose governance store is updated.
            params: ``subject_type``, ``subject_id``, and ``tool_names``.

        Returns:
            Result payload describing the disabled tools.

        Raises:
            HTTPException: On invalid subject or empty tool list.
        """
        subject_type = str(params.get("subject_type") or "")
        subject_id = str(params.get("subject_id") or "")
        tool_names = [
            str(name).strip()
            for name in (params.get("tool_names") or [])
            if str(name).strip()
        ]
        if subject_type not in SUBJECT_TYPES or not subject_id:
            raise HTTPException(
                status_code=400, detail="Invalid governance subject for scope_tools"
            )
        if not tool_names:
            raise HTTPException(
                status_code=400, detail="scope_tools requires tool_names"
            )
        if subject_type == SUBJECT_TYPE_MANAGED_AGENTS:
            agent = crud_managed_agent.get_for_account(
                self.db, account_id=str(account.id), agent_id=subject_id
            )
            if agent is None:
                raise HTTPException(status_code=404, detail="Managed agent not found")
        config = get_subject_governance(
            account.meta_data or {},
            subject_type=subject_type,
            subject_id=subject_id,
        )
        overrides = dict(config.get("tool_enabled_overrides") or {})
        for tool_name in tool_names:
            overrides[tool_name] = False
        config["tool_enabled_overrides"] = overrides
        account.meta_data = set_subject_governance(
            account.meta_data or {},
            subject_type=subject_type,
            subject_id=subject_id,
            config=config,
        )
        self.db.add(account)
        self.db.commit()
        invalidate_account_governance_cache(str(account.id))
        return {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "disabled_tools": tool_names,
        }

    def _apply_disable_builtin_tools(
        self, *, account: Account, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Disable Preloop builtins account-wide via ToolConfiguration rows.

        Upserts ``tool_source="builtin"`` rows with ``is_enabled=False``. Never
        touches rows with any other ``tool_source`` (including ``agent`` from
        PR #132). Idempotent for already-disabled rows.

        Args:
            account: Account whose tool configurations are updated.
            params: ``tool_names`` list of bare builtin names.

        Returns:
            Result payload listing the disabled tool names (revert via Tools
            page re-enable).

        Raises:
            HTTPException: On empty list or unknown (non-builtin) tool names.
        """
        tool_names = [
            str(name).strip()
            for name in (params.get("tool_names") or [])
            if str(name).strip()
        ]
        if not tool_names:
            raise HTTPException(
                status_code=400,
                detail="disable_builtin_tools requires tool_names",
            )
        builtin_names = self._builtin_catalog_names()
        unknown = sorted({name for name in tool_names if name not in builtin_names})
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    "disable_builtin_tools only accepts Preloop builtin names; "
                    f"unknown: {', '.join(unknown)}"
                ),
            )
        # Deduplicate while preserving order.
        ordered_unique: list[str] = []
        seen: set[str] = set()
        for name in tool_names:
            if name in seen:
                continue
            seen.add(name)
            ordered_unique.append(name)

        disabled: list[str] = []
        for tool_name in ordered_unique:
            existing = crud_tool_configuration.get_by_tool_name_and_source(
                self.db,
                account_id=str(account.id),
                tool_name=tool_name,
                tool_source="builtin",
            )
            if existing is not None:
                # Strict keying on tool_source="builtin" already; never flip
                # agent-approval or MCP rows.
                if existing.is_enabled:
                    update_in = ToolConfigurationUpdate(is_enabled=False)  # type: ignore[call-arg]
                    crud_tool_configuration.update(
                        self.db,
                        db_obj=existing,
                        config_in=update_in,
                    )
                disabled.append(tool_name)
                continue
            create_in = ToolConfigurationCreate(
                tool_name=tool_name,
                tool_source="builtin",
                account_id=str(account.id),
                mcp_server_id=None,
                is_enabled=False,
            )  # type: ignore[call-arg]
            crud_tool_configuration.create(self.db, config_in=create_in)
            disabled.append(tool_name)
        return {
            "disabled_tools": disabled,
            "tool_source": "builtin",
            "revert_hint": (
                "Re-enable these tools from the Tools page (toggle is_enabled back on)."
            ),
        }

    def _apply_context_optimization_settings(
        self, *, account: Account, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Enable gateway context-optimization transforms for one subject.

        Handles both ``enable_compression`` (dedupe + noise stripping) and
        ``cap_tool_results`` (tool-result size cap); whichever keys are
        present in ``params`` are merged into the subject's settings.

        Args:
            account: Account whose governance store is updated.
            params: ``subject_type``, ``subject_id``, plus any of
                ``dedupe_tool_results``, ``strip_noise``,
                ``max_tool_result_chars``.

        Returns:
            Result payload with the effective settings.

        Raises:
            HTTPException: On invalid subject or empty settings.
        """
        subject_type = str(params.get("subject_type") or "")
        subject_id = str(params.get("subject_id") or "")
        if subject_type not in SUBJECT_TYPES or not subject_id:
            raise HTTPException(
                status_code=400,
                detail="Invalid governance subject for context optimization",
            )
        if subject_type == SUBJECT_TYPE_MANAGED_AGENTS:
            agent = crud_managed_agent.get_for_account(
                self.db, account_id=str(account.id), agent_id=subject_id
            )
            if agent is None:
                raise HTTPException(status_code=404, detail="Managed agent not found")
        updates: dict[str, Any] = {}
        for flag in ("dedupe_tool_results", "strip_noise"):
            if isinstance(params.get(flag), bool):
                updates[flag] = params[flag]
        if params.get("max_tool_result_chars") is not None:
            try:
                cap = int(params["max_tool_result_chars"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid tool-result cap"
                ) from exc
            if cap < MIN_TOOL_RESULT_CAP:
                raise HTTPException(
                    status_code=400,
                    detail=f"Tool-result cap must be at least {MIN_TOOL_RESULT_CAP}",
                )
            updates["max_tool_result_chars"] = cap
        if not updates:
            raise HTTPException(
                status_code=400, detail="No context optimization settings provided"
            )
        config = get_subject_governance(
            account.meta_data or {},
            subject_type=subject_type,
            subject_id=subject_id,
        )
        settings_dict = dict(config.get(CONTEXT_OPTIMIZATION_KEY) or {})
        settings_dict.update(updates)
        config[CONTEXT_OPTIMIZATION_KEY] = settings_dict
        account.meta_data = set_subject_governance(
            account.meta_data or {},
            subject_type=subject_type,
            subject_id=subject_id,
            config=config,
        )
        self.db.add(account)
        self.db.commit()
        invalidate_account_governance_cache(str(account.id))
        return {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "context_optimization": settings_dict,
        }

    def _apply_set_budget(
        self, *, account: Account, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Create or update a scoped budget policy.

        Args:
            account: Owning account.
            params: ``subject_type``, ``subject_id``, ``period``,
                ``hard_limit_usd``, and optional ``model_alias``.

        Returns:
            Result payload with the budget policy id.

        Raises:
            HTTPException: On invalid period, subject, or limit.
        """
        subject_type = str(params.get("subject_type") or "")
        subject_id_raw = params.get("subject_id")
        model_alias = params.get("model_alias") or None
        try:
            period = BudgetPeriod(str(params.get("period") or "daily"))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid budget period"
            ) from exc
        try:
            hard_limit = float(params.get("hard_limit_usd") or 0.0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid budget limit") from exc
        if hard_limit <= 0:
            raise HTTPException(status_code=400, detail="Budget limit must be positive")
        if not subject_type:
            raise HTTPException(status_code=400, detail="Missing budget subject")
        subject_id: Optional[UUID] = None
        if subject_id_raw:
            try:
                subject_id = UUID(str(subject_id_raw))
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid budget subject id"
                ) from exc
        if subject_type == BUDGET_SUBJECT_MANAGED_AGENT and subject_id is not None:
            agent = crud_managed_agent.get_for_account(
                self.db, account_id=str(account.id), agent_id=str(subject_id)
            )
            if agent is None:
                raise HTTPException(status_code=404, detail="Managed agent not found")
        existing = [
            policy
            for policy in crud_budget_policy.get_policies_for_subject(
                self.db,
                account_id=account.id,
                subject_type=subject_type,
                subject_id=subject_id,
            )
            if policy.period == period and policy.model_alias == model_alias
        ]
        if existing:
            policy = existing[0]
            policy.hard_limit_usd = hard_limit
            self.db.add(policy)
            self.db.commit()
            self.db.refresh(policy)
        else:
            policy = crud_budget_policy.create(
                self.db,
                obj_in={
                    "account_id": account.id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "model_alias": model_alias,
                    "period": period,
                    "hard_limit_usd": hard_limit,
                },
            )
        return {
            "budget_policy_id": str(policy.id),
            "subject_type": subject_type,
            "subject_id": str(subject_id) if subject_id else None,
            "period": period.value,
            "hard_limit_usd": hard_limit,
        }

    def _principal_baseline(
        self, *, account: Account, summary: RuntimeSessionSummary
    ) -> Optional[dict[str, Any]]:
        """Capture pre-apply usage averages for the session's principal.

        Args:
            account: Owning account.
            summary: Session summary providing the principal id.

        Returns:
            Baseline averages over the last week, or ``None`` when the
            session has no runtime principal.
        """
        if not summary.runtime_principal_id:
            return None
        now = datetime.now(timezone.utc)
        baseline = crud_api_usage.get_runtime_principal_gateway_averages(
            self.db,
            account_id=str(account.id),
            runtime_principal_id=summary.runtime_principal_id,
            start=now - timedelta(days=BASELINE_WINDOW_DAYS),
            end=now,
        )
        baseline["window_days"] = BASELINE_WINDOW_DAYS
        baseline["captured_at"] = now.isoformat()
        return baseline

    def _applied_action_to_schema(
        self, row: Any, *, account: Account
    ) -> RuntimeSessionOptimizationAppliedAction:
        """Convert one stored action row, computing its measured outcome.

        Args:
            row: Stored ``RuntimeSessionOptimizationAction`` row.
            account: Owning account.

        Returns:
            API schema with ``outcome`` populated when post-apply requests
            exist for the same runtime principal.
        """
        outcome: Optional[dict[str, Any]] = None
        baseline = row.baseline if isinstance(row.baseline, dict) else None
        if row.runtime_principal_id:
            measured = crud_api_usage.get_runtime_principal_gateway_averages(
                self.db,
                account_id=str(row.account_id),
                runtime_principal_id=row.runtime_principal_id,
                start=row.created_at,
                exclude_runtime_session_id=str(row.runtime_session_id),
            )
            if measured["requests"] > 0:
                outcome = dict(measured)
                if baseline:
                    base_tokens = float(
                        baseline.get("avg_total_tokens_per_request") or 0.0
                    )
                    base_cost = float(baseline.get("avg_cost_per_request") or 0.0)
                    if base_tokens > 0:
                        outcome["tokens_per_request_change_pct"] = round(
                            100
                            * (measured["avg_total_tokens_per_request"] - base_tokens)
                            / base_tokens,
                            1,
                        )
                    if base_cost > 0:
                        outcome["cost_per_request_change_pct"] = round(
                            100
                            * (measured["avg_cost_per_request"] - base_cost)
                            / base_cost,
                            1,
                        )
        return RuntimeSessionOptimizationAppliedAction(
            id=str(row.id),
            runtime_session_id=str(row.runtime_session_id),
            suggestion_id=row.suggestion_id,
            suggestion_title=row.suggestion_title,
            action_type=row.action_type,
            params=row.params or {},
            status=row.status,
            applied_by=row.applied_by,
            applied_at=row.created_at,
            result=row.result or {},
            baseline=baseline,
            outcome=outcome,
        )
