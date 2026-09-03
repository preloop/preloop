"""Reporting service for model gateway usage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_usage, crud_gateway_usage_search_document
from preloop.models.models.account import Account
from preloop.models.models.ai_model import AIModel
from preloop.models.models.flow import Flow
from preloop.schemas.ai_model import AIModelGatewayUsageSummaryResponse
from preloop.schemas.gateway_usage import (
    AccountGatewayUsageSearchResponse,
    AccountGatewayUsageSummaryResponse,
    AccountRateLimitReportResponse,
    RateLimitByModel,
    RateLimitBySession,
    RateLimitSnapshotItem,
    RateLimitTotals,
    FlowGatewayUsageSummaryResponse,
    GatewayBudgetSummary,
    GatewayTokenUsage,
    GatewayUsageByDay,
    GatewayUsageByExecution,
    GatewayUsageByFlow,
    GatewayUsageByModel,
    GatewayUsageSearchResultItem,
    GatewayUsageBySession,
)
from preloop.services.tool_usage_stats import ToolUsageStatsService

#: Window every usage report falls back to when the caller names no dates.
DEFAULT_USAGE_WINDOW_DAYS = 30


def normalize_usage_period(
    start_date: Optional[datetime], end_date: Optional[datetime]
) -> tuple[datetime, datetime]:
    """Return the reporting window, defaulting to the last 30 days in UTC.

    Shared so every usage answer for the same page covers the same period:
    the batch overview and the per-model detail summary must not disagree
    about what "this window" means.

    Args:
        start_date: Requested lower bound, naive values read as UTC.
        end_date: Requested upper bound, naive values read as UTC.

    Returns:
        Timezone-aware ``(start_date, end_date)``.
    """
    if end_date is None:
        end_date = datetime.now(timezone.utc)
    elif end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)

    if start_date is None:
        start_date = end_date - timedelta(days=DEFAULT_USAGE_WINDOW_DAYS)
    elif start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=timezone.utc)

    return start_date, end_date


class ModelGatewayUsageService:
    """Build product-facing summaries from gateway usage facts."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_account_summary(
        self,
        *,
        account: Account,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        runtime_principal_id: Optional[str] = None,
        include_breakdown: bool = True,
        exclude_retries: bool = False,
    ) -> AccountGatewayUsageSummaryResponse:
        start_date, end_date = self._normalize_period(start_date, end_date)
        totals = crud_api_usage.get_gateway_usage_summary(
            self.db,
            account_id=str(account.id),
            start_date=start_date,
            end_date=end_date,
            runtime_principal_id=runtime_principal_id,
            exclude_retries=exclude_retries,
        )
        if include_breakdown:
            usage_by_model = crud_api_usage.get_gateway_usage_by_model(
                self.db,
                account_id=str(account.id),
                start_date=start_date,
                end_date=end_date,
                runtime_principal_id=runtime_principal_id,
            )
            usage_by_flow = crud_api_usage.get_gateway_usage_by_flow(
                self.db,
                account_id=str(account.id),
                start_date=start_date,
                end_date=end_date,
                runtime_principal_id=runtime_principal_id,
            )
            usage_by_session = crud_api_usage.get_gateway_usage_by_session(
                self.db,
                account_id=str(account.id),
                start_date=start_date,
                end_date=end_date,
                runtime_principal_id=runtime_principal_id,
                # Cover all agents' sessions for the cost-view breakdown tabs;
                # the default of 20 truncates to the most-recent sessions and
                # drops agents whose traffic isn't in that window.
                limit=250,
            )
            usage_by_tool = ToolUsageStatsService(self.db).get_account_usage_by_tool(
                account_id=str(account.id),
                start_date=start_date,
                end_date=end_date,
            )
            requests_by_day = crud_api_usage.get_gateway_usage_timeseries(
                self.db,
                account_id=str(account.id),
                start_date=start_date,
                end_date=end_date,
                runtime_principal_id=runtime_principal_id,
            )
        else:
            usage_by_model = []
            usage_by_flow = []
            usage_by_session = []
            usage_by_tool = []
            requests_by_day = []

        budget_cfg = self._normalize_budget_config(
            (account.meta_data or {}).get("model_gateway_budget")
        )
        budget = GatewayBudgetSummary(
            monthly_limit_usd=budget_cfg["monthly_usd_limit"],
            soft_limit_usd=budget_cfg["soft_limit_usd"],
            current_spend_usd=totals["estimated_cost"],
            soft_limit_exceeded=(
                budget_cfg["soft_limit_usd"] is not None
                and totals["estimated_cost"] > budget_cfg["soft_limit_usd"]
            ),
            hard_limit_exceeded=(
                budget_cfg["monthly_usd_limit"] is not None
                and totals["estimated_cost"] > budget_cfg["monthly_usd_limit"]
            ),
        )

        return AccountGatewayUsageSummaryResponse(
            period_start=start_date,
            period_end=end_date,
            total_requests=totals["request_count"],
            successful_requests=totals["success_count"],
            failed_requests=totals["error_count"],
            token_usage=GatewayTokenUsage(
                prompt_tokens=totals["prompt_tokens"],
                completion_tokens=totals["completion_tokens"],
                total_tokens=totals["total_tokens"],
            ),
            estimated_cost=totals["estimated_cost"],
            unpriced_requests=totals.get("unpriced_requests", 0),
            unpriced_tokens=totals.get("unpriced_tokens", 0),
            budget=budget,
            requests_by_day=[GatewayUsageByDay(**row) for row in requests_by_day],
            usage_by_model=[self._model_row_to_schema(row) for row in usage_by_model],
            usage_by_flow=[self._flow_row_to_schema(row) for row in usage_by_flow],
            usage_by_session=[
                self._session_row_to_schema(row) for row in usage_by_session
            ],
            usage_by_tool=usage_by_tool,
        )

    def get_flow_summary(
        self,
        *,
        account: Account,
        flow: Flow,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> FlowGatewayUsageSummaryResponse:
        start_date, end_date = self._normalize_period(start_date, end_date)
        totals = crud_api_usage.get_gateway_usage_summary(
            self.db,
            account_id=str(account.id),
            flow_id=str(flow.id),
            start_date=start_date,
            end_date=end_date,
        )
        usage_by_model = crud_api_usage.get_gateway_usage_by_model(
            self.db,
            account_id=str(account.id),
            flow_id=str(flow.id),
            start_date=start_date,
            end_date=end_date,
        )
        usage_by_execution = crud_api_usage.get_gateway_usage_by_execution(
            self.db,
            account_id=str(account.id),
            flow_id=str(flow.id),
            start_date=start_date,
            end_date=end_date,
        )
        budget_cfg = self._normalize_budget_config(
            (flow.agent_config or {}).get("model_gateway_budget")
        )
        budget = GatewayBudgetSummary(
            monthly_limit_usd=budget_cfg["monthly_usd_limit"],
            soft_limit_usd=budget_cfg["soft_limit_usd"],
            current_spend_usd=totals["estimated_cost"],
            soft_limit_exceeded=(
                budget_cfg["soft_limit_usd"] is not None
                and totals["estimated_cost"] > budget_cfg["soft_limit_usd"]
            ),
            hard_limit_exceeded=(
                budget_cfg["monthly_usd_limit"] is not None
                and totals["estimated_cost"] > budget_cfg["monthly_usd_limit"]
            ),
        )

        return FlowGatewayUsageSummaryResponse(
            flow_id=str(flow.id),
            flow_name=flow.name,
            period_start=start_date,
            period_end=end_date,
            total_requests=totals["request_count"],
            successful_requests=totals["success_count"],
            failed_requests=totals["error_count"],
            token_usage=GatewayTokenUsage(
                prompt_tokens=totals["prompt_tokens"],
                completion_tokens=totals["completion_tokens"],
                total_tokens=totals["total_tokens"],
            ),
            estimated_cost=totals["estimated_cost"],
            budget=budget,
            usage_by_model=[self._model_row_to_schema(row) for row in usage_by_model],
            usage_by_execution=[
                GatewayUsageByExecution(
                    flow_execution_id=row["flow_execution_id"],
                    request_count=row["request_count"],
                    token_usage=GatewayTokenUsage(
                        prompt_tokens=row["prompt_tokens"],
                        completion_tokens=row["completion_tokens"],
                        total_tokens=row["total_tokens"],
                    ),
                    estimated_cost=row["estimated_cost"],
                    last_request_at=row["last_request_at"],
                )
                for row in usage_by_execution
            ],
        )

    def get_ai_model_summary(
        self,
        *,
        ai_model: AIModel,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> AIModelGatewayUsageSummaryResponse:
        """Return gateway usage totals for one durable AI model."""
        start_date, end_date = self._normalize_period(start_date, end_date)
        totals = crud_api_usage.get_gateway_usage_summary(
            self.db,
            account_id=str(ai_model.account_id),
            ai_model_id=str(ai_model.id),
            start_date=start_date,
            end_date=end_date,
        )
        usage_by_session = crud_api_usage.get_gateway_usage_by_session(
            self.db,
            account_id=str(ai_model.account_id),
            ai_model_id=str(ai_model.id),
            start_date=start_date,
            end_date=end_date,
        )
        requests_by_day = crud_api_usage.get_gateway_usage_timeseries(
            self.db,
            account_id=str(ai_model.account_id),
            ai_model_id=str(ai_model.id),
            start_date=start_date,
            end_date=end_date,
        )
        return AIModelGatewayUsageSummaryResponse(
            ai_model_id=str(ai_model.id),
            model_name=ai_model.name,
            provider_name=ai_model.provider_name,
            model_identifier=ai_model.model_identifier,
            period_start=start_date,
            period_end=end_date,
            total_requests=totals["request_count"],
            successful_requests=totals["success_count"],
            failed_requests=totals["error_count"],
            token_usage=GatewayTokenUsage(
                prompt_tokens=totals["prompt_tokens"],
                completion_tokens=totals["completion_tokens"],
                total_tokens=totals["total_tokens"],
            ),
            estimated_cost=totals["estimated_cost"],
            requests_by_day=[GatewayUsageByDay(**row) for row in requests_by_day],
            usage_by_session=[
                self._session_row_to_schema(row) for row in usage_by_session
            ],
        )

    def get_api_key_summary(
        self,
        *,
        api_key: Any,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Any:
        """Return gateway usage totals for one API key."""
        start_date, end_date = self._normalize_period(start_date, end_date)
        totals = crud_api_usage.get_gateway_usage_summary(
            self.db,
            account_id=str(api_key.account_id),
            api_key_id=str(api_key.id),
            start_date=start_date,
            end_date=end_date,
        )
        usage_by_model = crud_api_usage.get_gateway_usage_by_model(
            self.db,
            account_id=str(api_key.account_id),
            api_key_id=str(api_key.id),
            start_date=start_date,
            end_date=end_date,
        )
        usage_by_session = crud_api_usage.get_gateway_usage_by_session(
            self.db,
            account_id=str(api_key.account_id),
            api_key_id=str(api_key.id),
            start_date=start_date,
            end_date=end_date,
        )
        requests_by_day = crud_api_usage.get_gateway_usage_timeseries(
            self.db,
            account_id=str(api_key.account_id),
            api_key_id=str(api_key.id),
            start_date=start_date,
            end_date=end_date,
        )

        from preloop.schemas.gateway_usage import ApiKeyGatewayUsageSummaryResponse

        return ApiKeyGatewayUsageSummaryResponse(
            api_key_id=str(api_key.id),
            period_start=start_date,
            period_end=end_date,
            total_requests=totals["request_count"],
            successful_requests=totals["success_count"],
            failed_requests=totals["error_count"],
            token_usage=GatewayTokenUsage(
                prompt_tokens=totals["prompt_tokens"],
                completion_tokens=totals["completion_tokens"],
                total_tokens=totals["total_tokens"],
            ),
            estimated_cost=totals["estimated_cost"],
            requests_by_day=[GatewayUsageByDay(**row) for row in requests_by_day],
            usage_by_model=[self._model_row_to_schema(row) for row in usage_by_model],
            usage_by_session=[
                self._session_row_to_schema(row) for row in usage_by_session
            ],
        )

    def search_account_interactions(
        self,
        *,
        account: Account,
        query: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        ai_model_id: Optional[str] = None,
        provider_name: Optional[str] = None,
        model_alias: Optional[str] = None,
        flow_id: Optional[str] = None,
        runtime_session_id: Optional[str] = None,
        runtime_principal_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        session_source_type: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> AccountGatewayUsageSearchResponse:
        """Search or list indexed gateway interactions for one account."""
        start_date, end_date = self._normalize_period(start_date, end_date)
        results = crud_gateway_usage_search_document.search_account_documents(
            self.db,
            account_id=str(account.id),
            start_date=start_date,
            end_date=end_date,
            query=query,
            ai_model_id=ai_model_id,
            provider_name=provider_name,
            model_alias=model_alias,
            flow_id=flow_id,
            runtime_session_id=runtime_session_id,
            runtime_principal_id=runtime_principal_id,
            api_key_id=api_key_id,
            session_source_type=session_source_type,
            limit=limit,
            offset=offset,
        )
        return AccountGatewayUsageSearchResponse(
            period_start=start_date,
            period_end=end_date,
            query=query,
            total=results["total"],
            limit=limit,
            offset=offset,
            items=[self._search_row_to_schema(item) for item in results["items"]],
        )

    def get_account_rate_limit_report(
        self,
        *,
        account: Account,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        runtime_principal_id: Optional[str] = None,
    ) -> AccountRateLimitReportResponse:
        """Build the account rate-limit telemetry and headroom report (#136).

        Aggregates observed upstream 429s (count, provider-advised blocked
        time, subtype split, per-model and per-session breakdowns) over the
        window, plus the most recent rate-limit header snapshot per
        provider/model as observed on real upstream responses. Snapshots are
        not window-filtered: the latest observation is the headroom signal
        regardless of when it happened, and it carries its own timestamp.

        Args:
            account: The account whose gateway traffic is reported.
            start_date: Inclusive window start; defaults to 30 days back.
            end_date: Exclusive window end; defaults to now.
            runtime_principal_id: Restrict the 429 aggregation to one
                principal (snapshots stay account-wide).

        Returns:
            The report response.
        """
        start_date, end_date = self._normalize_period(start_date, end_date)
        summary = crud_api_usage.get_rate_limit_summary(
            self.db,
            account_id=str(account.id),
            start_date=start_date,
            end_date=end_date,
            runtime_principal_id=runtime_principal_id,
        )
        snapshots = crud_api_usage.get_latest_rate_limit_snapshots(
            self.db,
            account_id=str(account.id),
        )
        return AccountRateLimitReportResponse(
            period_start=start_date,
            period_end=end_date,
            totals=RateLimitTotals(**summary["totals"]),
            by_model=[RateLimitByModel(**row) for row in summary["by_model"]],
            by_session=[RateLimitBySession(**row) for row in summary["by_session"]],
            latest_snapshots=[RateLimitSnapshotItem(**row) for row in snapshots],
        )

    @staticmethod
    def _normalize_period(
        start_date: Optional[datetime], end_date: Optional[datetime]
    ) -> tuple[datetime, datetime]:
        return normalize_usage_period(start_date, end_date)

    @staticmethod
    def _normalize_budget_config(config):
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
    def _model_row_to_schema(row) -> GatewayUsageByModel:
        return GatewayUsageByModel(
            ai_model_id=row["ai_model_id"],
            model_alias=row["model_alias"],
            provider_name=row["provider_name"],
            request_count=row["request_count"],
            token_usage=GatewayTokenUsage(
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                total_tokens=row["total_tokens"],
            ),
            estimated_cost=row["estimated_cost"],
            unpriced_request_count=row.get("unpriced_request_count") or 0,
            zero_priced_request_count=row.get("zero_priced_request_count") or 0,
            failed_request_count=row.get("failed_request_count") or 0,
            last_request_at=row.get("last_request_at"),
        )

    @staticmethod
    def _flow_row_to_schema(row) -> GatewayUsageByFlow:
        return GatewayUsageByFlow(
            flow_id=row["flow_id"],
            flow_name=row["flow_name"],
            request_count=row["request_count"],
            token_usage=GatewayTokenUsage(
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                total_tokens=row["total_tokens"],
            ),
            estimated_cost=row["estimated_cost"],
        )

    @staticmethod
    def _session_row_to_schema(row) -> GatewayUsageBySession:
        return GatewayUsageBySession(
            ai_model_id=row["ai_model_id"],
            runtime_session_id=row["runtime_session_id"],
            session_source_type=row["session_source_type"],
            session_source_id=row["session_source_id"],
            title=row.get("title"),
            session_summary=row.get("session_summary"),
            session_summary_updated_at=row.get("session_summary_updated_at"),
            runtime_principal_type=row.get("runtime_principal_type"),
            runtime_principal_id=row.get("runtime_principal_id"),
            runtime_principal_name=row.get("runtime_principal_name"),
            agent_id=row.get("agent_id"),
            agent_name=row.get("agent_name"),
            flow_execution_id=row["flow_execution_id"],
            flow_id=row["flow_id"],
            flow_name=row["flow_name"],
            session_reference=row["session_reference"],
            model_alias=row["model_alias"],
            provider_name=row["provider_name"],
            request_count=row["request_count"],
            token_usage=GatewayTokenUsage(
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                total_tokens=row["total_tokens"],
            ),
            estimated_cost=row["estimated_cost"],
            last_request_at=row["last_request_at"],
        )

    @staticmethod
    def _search_row_to_schema(item) -> GatewayUsageSearchResultItem:
        return GatewayUsageSearchResultItem(
            api_usage_id=item["api_usage_id"],
            ai_model_id=item["ai_model_id"],
            timestamp=item["timestamp"],
            status_code=item["status_code"],
            outcome=item["outcome"],
            endpoint=item["endpoint"],
            method=item["method"],
            provider_name=item["provider_name"],
            model_alias=item["model_alias"],
            flow_id=item["flow_id"],
            flow_name=item["flow_name"],
            flow_execution_id=item["flow_execution_id"],
            runtime_session_id=item["runtime_session_id"],
            session_source_type=item["session_source_type"],
            session_source_id=item["session_source_id"],
            session_reference=item["session_reference"],
            runtime_principal_type=item["runtime_principal_type"],
            runtime_principal_id=item["runtime_principal_id"],
            runtime_principal_name=item["runtime_principal_name"],
            auth_subject_type=item["auth_subject_type"],
            api_key_id=item["api_key_id"],
            api_key_name=item["api_key_name"],
            estimated_cost=item["estimated_cost"],
            token_usage=GatewayTokenUsage(
                prompt_tokens=item["prompt_tokens"],
                completion_tokens=item["completion_tokens"],
                total_tokens=item["total_tokens"],
            ),
            excerpt=item["excerpt"],
            meta_data=item["meta_data"],
        )
