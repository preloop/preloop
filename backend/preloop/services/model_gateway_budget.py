"""Budget checks for model gateway requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_usage,
    crud_flow,
)
from preloop.models.crud.plan import subscription as crud_subscription
from preloop.models.models.ai_model import AIModel
from preloop.models.models.flow import Flow
from preloop.services.model_allowlist import (
    allowlist_permits_model,
    format_model_not_allowed_detail,
    normalize_allowed_models,
    requested_model_label,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_pricing import estimate_ai_model_usage_cost
from preloop.services.model_runtime_resolver import gateway_model_alias_candidates
from preloop.services.subject_governance import (
    build_subject_context_from_api_key,
    get_subject_governance,
    subject_scope_chain,
)


def is_built_in_hosted_model(ai_model: AIModel) -> bool:
    """True when a model is a deployment-provided (operator-paid) hosted model.

    Built-in hosted models are marked ``hosted`` in their metadata, or are
    system-wide (no owning account) gateway-enabled models. Account-owned
    models running on the user's own provider keys (BYOK) are never hosted.

    Args:
        ai_model: The model to classify.

    Returns:
        Whether the model's compute is paid by the deployment operator.
    """
    meta_data = ai_model.meta_data if isinstance(ai_model.meta_data, dict) else {}
    if bool(meta_data.get("hosted")):
        return True
    gateway_config = (
        meta_data.get("gateway") if isinstance(meta_data.get("gateway"), dict) else {}
    )
    return ai_model.account_id is None and bool(gateway_config.get("enabled"))


def _default_estimated_output_tokens() -> int:
    return max(1, int(settings.billing_budget_default_estimated_output_tokens))


def _chars_per_token() -> float:
    return max(1.0, float(settings.billing_budget_chars_per_token))


# Back-compat alias for tests/imports that still reference the constant.
DEFAULT_ESTIMATED_OUTPUT_TOKENS = 1024


@dataclass
class BudgetCheckResult:
    """Outcome of a gateway budget check."""

    account_limit_usd: Optional[float]
    account_soft_limit_usd: Optional[float]
    account_current_spend_usd: float
    account_estimated_total_usd: Optional[float]
    flow_limit_usd: Optional[float]
    flow_soft_limit_usd: Optional[float]
    flow_current_spend_usd: float
    flow_estimated_total_usd: Optional[float]
    estimated_request_cost_usd: Optional[float]
    trial_hosted_model_limit_usd: Optional[float]
    trial_hosted_model_current_spend_usd: Optional[float]
    trial_hosted_model_estimated_total_usd: Optional[float]
    hard_limit_exceeded: bool
    soft_limit_exceeded: bool
    enforcement_reason: Optional[str]
    pricing_available: bool
    reset_at: Optional[datetime] = None
    # Populated for ``subject_model_not_allowed`` so renderers can name the
    # requested model and the allowlist that denied it.
    requested_model: Optional[str] = None
    allowed_models: Optional[list[str]] = None


class ModelGatewayBudgetService:
    """Budget checks and reconciliation for model gateway requests."""

    def __init__(self, db: Session, auth_context: ModelGatewayAuthContext) -> None:
        self.db = db
        self.auth_context = auth_context

    def preflight_check(
        self, ai_model: AIModel, payload: Dict[str, Any]
    ) -> BudgetCheckResult:
        """Check whether a gateway request can proceed within configured budgets."""
        account = crud_account.get(self.db, id=self.auth_context.user.account_id)
        subject_context = (
            build_subject_context_from_api_key(self.auth_context.api_key)
            if self.auth_context.api_key
            else {}
        )

        estimated_request_cost = self._estimate_request_cost(
            ai_model,
            payload,
            pricing_override=self._pricing_override_for_request(ai_model, payload),
        )
        pricing_available = estimated_request_cost is not None

        hard_limit_exceeded = False
        soft_limit_exceeded = False
        enforcement_reason = None
        reset_at = None
        trial_hosted_model_limit_usd = None
        trial_hosted_model_current_spend_usd = None
        trial_hosted_model_estimated_total_usd = None
        governed_model_spellings = self._governed_model_spellings(ai_model, payload)
        denied_allowed_models: Optional[list[str]] = None

        # 1. Check subject allowed models. Every scope in the chain (API key,
        # then managed agent) must permit the resolved model; an entry may be
        # a gateway alias, an AIModel id, or an AIModel display name (the
        # console persisted display names historically). Fail closed: an
        # allowlist that names neither the resolved model nor any of its
        # spellings denies the request, including the degenerate case where
        # the request names no model at all.
        if account is not None:
            for subject_type, subject_id in subject_scope_chain(subject_context):
                config = get_subject_governance(
                    account.meta_data or {},
                    subject_type=subject_type,
                    subject_id=subject_id,
                )
                allowed_models = normalize_allowed_models(
                    config.get("allowed_models")
                    if isinstance(config.get("allowed_models"), list)
                    else None
                )
                if not allowed_models:
                    continue
                if not allowlist_permits_model(
                    allowed_models,
                    ai_model,
                    requested_spellings=governed_model_spellings,
                ):
                    hard_limit_exceeded = True
                    enforcement_reason = "subject_model_not_allowed"
                    denied_allowed_models = allowed_models
                    break

        # 2. Check trial mode limitation
        subscription = crud_subscription.get_active_for_account(
            self.db, account_id=str(self.auth_context.user.account_id)
        )
        if (
            subscription
            and subscription.status == "trialing"
            and self._is_built_in_hosted_model(ai_model)
        ):
            trial_hosted_model_limit_usd = max(
                float(settings.billing_trial_hosted_model_hard_cap_usd), 0.0
            )
            trial_hosted_model_current_spend_usd = self._get_trial_hosted_model_spend(
                account_id=str(self.auth_context.user.account_id),
                start=subscription.current_period_start,
                end=subscription.current_period_end or datetime.now(timezone.utc),
            )
            trial_hosted_model_estimated_total_usd = (
                trial_hosted_model_current_spend_usd + estimated_request_cost
                if estimated_request_cost is not None
                else None
            )
            if not pricing_available:
                hard_limit_exceeded = True
                enforcement_reason = "pricing_required_for_budget_enforcement"
            elif (
                trial_hosted_model_estimated_total_usd is not None
                and trial_hosted_model_estimated_total_usd
                > trial_hosted_model_limit_usd
            ):
                hard_limit_exceeded = True
                enforcement_reason = "trial_hosted_model_budget_exceeded"
        elif (
            subscription is None
            and settings.billing_enforce_entitlements
            and self._is_built_in_hosted_model(ai_model)
        ):
            # 3. Free-tier hosted-model cap (T2 paywall move). Card-free
            # accounts have no subscription at all, so the trial branch above
            # never fires for them — without this branch, built-in hosted
            # models are founder-paid and unmetered. No billing period exists
            # either, so spend is windowed to the current calendar month.
            trial_hosted_model_limit_usd = max(
                float(settings.billing_free_hosted_model_hard_cap_usd), 0.0
            )
            now = datetime.now(timezone.utc)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            trial_hosted_model_current_spend_usd = self._get_trial_hosted_model_spend(
                account_id=str(self.auth_context.user.account_id),
                start=month_start,
                end=now,
            )
            trial_hosted_model_estimated_total_usd = (
                trial_hosted_model_current_spend_usd + estimated_request_cost
                if estimated_request_cost is not None
                else None
            )
            if not pricing_available:
                hard_limit_exceeded = True
                enforcement_reason = "pricing_required_for_budget_enforcement"
            elif (
                trial_hosted_model_estimated_total_usd is not None
                and trial_hosted_model_estimated_total_usd
                > trial_hosted_model_limit_usd
            ):
                hard_limit_exceeded = True
                enforcement_reason = "free_hosted_model_budget_exceeded"

        return BudgetCheckResult(
            account_limit_usd=None,
            account_soft_limit_usd=None,
            account_current_spend_usd=0.0,
            account_estimated_total_usd=None,
            flow_limit_usd=None,
            flow_soft_limit_usd=None,
            flow_current_spend_usd=0.0,
            flow_estimated_total_usd=None,
            estimated_request_cost_usd=estimated_request_cost,
            trial_hosted_model_limit_usd=trial_hosted_model_limit_usd,
            trial_hosted_model_current_spend_usd=trial_hosted_model_current_spend_usd,
            trial_hosted_model_estimated_total_usd=trial_hosted_model_estimated_total_usd,
            hard_limit_exceeded=hard_limit_exceeded,
            soft_limit_exceeded=soft_limit_exceeded,
            enforcement_reason=enforcement_reason,
            pricing_available=pricing_available,
            reset_at=reset_at,
            requested_model=requested_model_label(ai_model, payload.get("model")),
            allowed_models=denied_allowed_models,
        )

    def enforce_or_raise(
        self, ai_model: AIModel, payload: Dict[str, Any]
    ) -> BudgetCheckResult:
        """Run the preflight check and raise if a hard limit is exceeded."""
        result = self.preflight_check(ai_model, payload)
        if result.hard_limit_exceeded:
            detail = "Model gateway budget exceeded"
            if result.enforcement_reason == "subject_model_not_allowed":
                detail = format_model_not_allowed_detail(
                    result.requested_model or "unknown", result.allowed_models
                )
            elif result.enforcement_reason == "account_budget_exceeded":
                detail = "Model gateway budget exceeded: account monthly limit reached"
            elif result.enforcement_reason == "flow_budget_exceeded":
                detail = "Model gateway budget exceeded: flow monthly limit reached"
            elif result.enforcement_reason == "user_model_budget_exceeded":
                detail = "Model gateway budget exceeded: user model limit reached"
            elif result.enforcement_reason == "api_key_model_budget_exceeded":
                detail = "Model gateway budget exceeded: key model limit reached"
            elif result.enforcement_reason == "account_model_budget_exceeded":
                detail = "Model gateway budget exceeded: account model limit reached"
            elif result.enforcement_reason == "pricing_required_for_budget_enforcement":
                detail = "Model gateway budget exceeded: pricing unavailable for requested model"
            elif result.enforcement_reason == "trial_hosted_model_budget_exceeded":
                detail = "Preloop trial limit for hosted model reached. Please configure your own OpenAI/Anthropic API key."
            elif result.enforcement_reason == "free_hosted_model_budget_exceeded":
                detail = (
                    "Preloop free-tier limit for hosted models reached. "
                    "Configure your own OpenAI/Anthropic API key or upgrade "
                    "your plan."
                )

            if result.reset_at:
                detail += f", try again at {result.reset_at.isoformat()}"

            headers = {}
            if result.reset_at:
                retry_after = max(
                    int((result.reset_at - datetime.now(timezone.utc)).total_seconds()),
                    1,
                )
                headers["Retry-After"] = str(retry_after)

            raise HTTPException(status_code=403, detail=detail, headers=headers)
        return result

    @staticmethod
    def _governed_model_spellings(
        ai_model: AIModel, payload: Dict[str, Any]
    ) -> set[str]:
        """Return the allowlist keys that identify this request's model.

        Governance keys off the *resolved* model, not the raw wire string, so
        that every spelling the gateway resolver accepts for one model is
        governed as one model. The raw request string is kept as an extra
        candidate so allowlists written against historical spellings keep
        matching.

        Args:
            ai_model: The model the gateway already resolved the request to.
            payload: The gateway request body.

        Returns:
            Spellings to test against the subject's ``allowed_models``; empty
            only when the model carries no identifier at all.
        """
        spellings = gateway_model_alias_candidates(ai_model)
        raw_requested = payload.get("model")
        if isinstance(raw_requested, str) and raw_requested.strip():
            spellings.add(raw_requested.strip())
        return spellings

    def _get_gateway_spend(
        self,
        *,
        account_id: str,
        start: datetime,
        flow_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        model_alias: Optional[str] = None,
    ) -> float:
        return crud_api_usage.get_gateway_spend(
            self.db,
            account_id=account_id,
            start=start,
            flow_id=flow_id,
            api_key_id=api_key_id,
            runtime_principal_id=runtime_principal_id,
            model_alias=model_alias,
        )

    def _get_trial_hosted_model_spend(
        self, *, account_id: str, start: datetime, end: datetime
    ) -> float:
        hosted_model_ids = {
            str(model.id)
            for model in crud_ai_model.get_all_for_account(
                self.db, account_id=account_id
            )
            if self._is_built_in_hosted_model(model)
        }
        if not hosted_model_ids:
            return 0.0

        # Filter to hosted models in SQL and take every matching row: this is a
        # SUM feeding a hard spend cap, and the query orders by request count,
        # so any row limit would silently drop low-volume, high-cost models
        # from the total and under-report spend against the cap.
        usage_rows = crud_api_usage.get_gateway_usage_by_model(
            self.db,
            account_id=account_id,
            start_date=start,
            end_date=end,
            ai_model_ids=sorted(hosted_model_ids),
            limit=None,
        )
        return float(sum(float(row.get("estimated_cost") or 0.0) for row in usage_rows))

    @staticmethod
    def _is_built_in_hosted_model(ai_model: AIModel) -> bool:
        return is_built_in_hosted_model(ai_model)

    @staticmethod
    def _normalize_budget_config(
        config: Optional[Dict[str, Any]],
    ) -> Dict[str, Optional[float]]:
        config = config or {}
        monthly_limit = config.get("monthly_usd_limit")
        soft_limit = config.get("soft_limit_usd")
        if (
            soft_limit is None
            and monthly_limit is not None
            and config.get("soft_limit_ratio") is not None
        ):
            soft_limit = float(monthly_limit) * float(config["soft_limit_ratio"])
        return {
            "monthly_usd_limit": float(monthly_limit)
            if monthly_limit is not None
            else None,
            "soft_limit_usd": float(soft_limit) if soft_limit is not None else None,
        }

    @staticmethod
    def _current_period_start() -> datetime:
        now = datetime.now(timezone.utc)
        return datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    def _get_flow(self, flow_id: Optional[str]) -> Optional[Flow]:
        if not flow_id:
            return None
        return crud_flow.get(
            self.db,
            id=flow_id,
            account_id=self.auth_context.user.account_id,
        )

    def _estimate_request_cost(
        self,
        ai_model: AIModel,
        payload: Dict[str, Any],
        pricing_override: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        if pricing_override is None and self._is_subscription_credentialed(ai_model):
            # Subscription-covered upstream (OAuth): no marginal API charge,
            # matching the $0 the recording path will persist.
            return 0.0
        estimated_input_tokens = ModelGatewayBudgetService._count_input_tokens(
            ai_model, payload
        )
        estimated_output_tokens = int(
            payload.get("max_completion_tokens")
            or payload.get("max_output_tokens")
            or payload.get("max_tokens")
            or _default_estimated_output_tokens()
        )
        return estimate_ai_model_usage_cost(
            ai_model,
            prompt_tokens=estimated_input_tokens,
            completion_tokens=estimated_output_tokens,
            total_tokens=estimated_input_tokens + estimated_output_tokens,
            pricing_override=pricing_override,
        )

    @staticmethod
    def _is_subscription_credentialed(ai_model: AIModel) -> bool:
        """True when the model bills against an OAuth subscription."""
        from preloop.services.secret_service import (
            ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
            OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
        )

        return ai_model.credential_type in {
            ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
            OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
        }

    def _pricing_override_for_request(
        self, ai_model: AIModel, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Return account pricing override for preflight cost estimates."""
        from preloop.services.pricing_overrides import resolve_pricing_override

        raw_model = payload.get("model")
        requested_alias = None
        if isinstance(raw_model, str):
            # Bound and sanitize client-supplied model names before lookup /
            # logging paths touch them.
            cleaned = raw_model.strip()[:128]
            if cleaned and all(ord(ch) >= 32 for ch in cleaned):
                requested_alias = cleaned

        return resolve_pricing_override(
            self.db,
            account_id=self.auth_context.user.account_id,
            ai_model=ai_model,
            requested_alias=requested_alias,
        )

    @staticmethod
    def _count_input_tokens(ai_model: AIModel, payload: Dict[str, Any]) -> int:
        """Estimate preflight input tokens for budget checks.

        Uses the cheap chars/4 heuristic rather than ``litellm.token_counter``
        so every gateway request does not pay tokenizer overhead on the hot
        path. Post-response recording still uses accurate provider usage.

        Args:
            ai_model: Model the request targets (unused; kept for call-site
                compatibility with earlier tokenizer-based estimation).
            payload: The gateway request body.

        Returns:
            Estimated input token count (0 when the payload has no text).
        """
        del ai_model  # reserved for future tokenizer selection
        return ModelGatewayBudgetService._estimate_input_tokens(payload)

    @staticmethod
    def _estimate_input_tokens(payload: Dict[str, Any]) -> int:
        text_parts = []
        if isinstance(payload.get("messages"), list):
            for message in payload["messages"]:
                if isinstance(message, dict):
                    text_parts.append(
                        ModelGatewayBudgetService._content_to_text(
                            message.get("content", "")
                        )
                    )
        if payload.get("instructions"):
            text_parts.append(str(payload["instructions"]))
        raw_input = payload.get("input")
        if isinstance(raw_input, str):
            text_parts.append(raw_input)
        elif isinstance(raw_input, list):
            for item in raw_input:
                if isinstance(item, dict):
                    text_parts.append(
                        ModelGatewayBudgetService._content_to_text(
                            item.get("content", "")
                        )
                    )
        total_chars = sum(len(part) for part in text_parts if part)
        chars_per_token = _chars_per_token()
        return max(1, math.ceil(total_chars / chars_per_token)) if total_chars else 0

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {
                    "input_text",
                    "text",
                    "output_text",
                }:
                    texts.append(item.get("text", ""))
            return "\n".join(filter(None, texts))
        return str(content)
