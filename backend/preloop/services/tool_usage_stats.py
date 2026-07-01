"""Account-wide tool usage and schema-injection cost aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_ai_model,
    crud_api_usage,
    crud_runtime_session_activity,
)
from preloop.models.models.ai_model import AIModel
from preloop.models.models.api_usage import ApiUsage
from preloop.schemas.gateway_usage import GatewayToolUsageByAgent, GatewayUsageByTool
from preloop.services.model_pricing import estimate_ai_model_usage_cost


def _coerce_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(result, 0)


def _tools_meta(row: ApiUsage) -> Optional[list[dict[str, Any]]]:
    meta = row.meta_data
    if not isinstance(meta, dict):
        return None
    tools_meta = meta.get("tools_meta")
    if not isinstance(tools_meta, list):
        return None
    return [item for item in tools_meta if isinstance(item, dict)]


def _per_prompt_token_price(
    row: ApiUsage, model: Optional[AIModel]
) -> tuple[float, bool]:
    if model is not None:
        unit_cost = estimate_ai_model_usage_cost(
            model,
            prompt_tokens=1,
            completion_tokens=0,
            total_tokens=1,
        )
        if unit_cost is not None and unit_cost > 0:
            return float(unit_cost), True
    prompt_tokens = _coerce_int(row.prompt_tokens)
    estimated_cost = float(row.estimated_cost or 0.0)
    if prompt_tokens > 0 and estimated_cost > 0:
        return estimated_cost / prompt_tokens, True
    return 0.0, True


@dataclass
class _SchemaCostAggregate:
    tool_name: str
    tool_source: str = "payload"
    schema_injections: int = 0
    schema_tokens_total: int = 0
    estimated_schema_cost: float = 0.0
    agent_costs: dict[str, float] = field(default_factory=dict)
    agent_names: dict[str, str] = field(default_factory=dict)
    agent_types: dict[str, str] = field(default_factory=dict)


class ToolUsageStatsService:
    """Build account-wide tool usage summaries for cost and tools views."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_account_usage_by_tool(
        self,
        *,
        account_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[GatewayUsageByTool]:
        """Return merged invocation counts and schema-injection cost per tool."""
        end = end_date or datetime.now(timezone.utc)
        start = start_date or datetime.fromtimestamp(0, tz=timezone.utc)

        invocation_rows = crud_runtime_session_activity.get_tool_summary_for_account(
            self.db,
            account_id=account_id,
            start_date=start,
            end_date=end,
            limit=limit,
        )
        agent_rows = (
            crud_runtime_session_activity.get_tool_invocations_by_agent_for_account(
                self.db,
                account_id=account_id,
                start_date=start,
                end_date=end,
            )
        )
        schema_costs = self._aggregate_schema_costs(
            account_id=account_id,
            start=start,
            end=end,
        )

        merged: dict[str, dict[str, Any]] = {}
        for row in invocation_rows:
            tool_name = row["tool_name"]
            if not tool_name:
                continue
            merged[tool_name] = {
                "tool_name": tool_name,
                "server_name": row.get("server_name"),
                "invocation_count": row.get("call_count", 0),
                "successful_invocations": row.get("successful_calls", 0),
                "failed_invocations": row.get("failed_calls", 0),
                "last_activity_at": row.get("last_activity_at"),
                "schema_injections": 0,
                "schema_tokens_total": 0,
                "estimated_schema_cost": 0.0,
                "usage_by_agent": [],
            }

        for row in agent_rows:
            tool_name = row["tool_name"]
            if not tool_name:
                continue
            entry = merged.setdefault(
                tool_name,
                {
                    "tool_name": tool_name,
                    "server_name": None,
                    "invocation_count": 0,
                    "successful_invocations": 0,
                    "failed_invocations": 0,
                    "last_activity_at": None,
                    "schema_injections": 0,
                    "schema_tokens_total": 0,
                    "estimated_schema_cost": 0.0,
                    "usage_by_agent": [],
                },
            )
            principal_id = row.get("runtime_principal_id")
            principal_type = row.get("runtime_principal_type")
            agent_id = principal_id if principal_type == "managed_agent" else None
            schema_cost = schema_costs.get(tool_name)
            agent_schema_cost = 0.0
            if schema_cost and principal_id:
                agent_schema_cost = schema_cost.agent_costs.get(principal_id, 0.0)
            entry["usage_by_agent"].append(
                GatewayToolUsageByAgent(
                    runtime_principal_type=principal_type,
                    runtime_principal_id=principal_id,
                    runtime_principal_name=row.get("runtime_principal_name"),
                    agent_id=agent_id,
                    invocation_count=row.get("call_count", 0),
                    estimated_schema_cost=round(agent_schema_cost, 6),
                )
            )

        for tool_name, schema in schema_costs.items():
            entry = merged.setdefault(
                tool_name,
                {
                    "tool_name": tool_name,
                    "server_name": None,
                    "invocation_count": 0,
                    "successful_invocations": 0,
                    "failed_invocations": 0,
                    "last_activity_at": None,
                    "schema_injections": 0,
                    "schema_tokens_total": 0,
                    "estimated_schema_cost": 0.0,
                    "usage_by_agent": [],
                },
            )
            entry["schema_injections"] = schema.schema_injections
            entry["schema_tokens_total"] = schema.schema_tokens_total
            entry["estimated_schema_cost"] = round(schema.estimated_schema_cost, 6)

        results: list[GatewayUsageByTool] = []
        for entry in merged.values():
            invocations = int(entry["invocation_count"] or 0)
            schema_cost = float(entry["estimated_schema_cost"] or 0.0)
            avg_cost = schema_cost / invocations if invocations > 0 else 0.0
            entry["usage_by_agent"].sort(
                key=lambda item: item.invocation_count, reverse=True
            )
            results.append(
                GatewayUsageByTool(
                    tool_name=entry["tool_name"],
                    server_name=entry.get("server_name"),
                    invocation_count=invocations,
                    successful_invocations=int(entry["successful_invocations"] or 0),
                    failed_invocations=int(entry["failed_invocations"] or 0),
                    schema_injections=int(entry["schema_injections"] or 0),
                    schema_tokens_total=int(entry["schema_tokens_total"] or 0),
                    estimated_schema_cost=schema_cost,
                    avg_cost_per_invocation=round(avg_cost, 6),
                    last_activity_at=entry.get("last_activity_at"),
                    usage_by_agent=entry["usage_by_agent"],
                )
            )

        results.sort(
            key=lambda item: (item.estimated_schema_cost, item.invocation_count),
            reverse=True,
        )
        return results[:limit]

    def _aggregate_schema_costs(
        self,
        *,
        account_id: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, _SchemaCostAggregate]:
        rows = crud_api_usage.list_gateway_rows_in_window(
            self.db,
            account_id=account_id,
            start=start,
            end=end,
        )
        model_cache: dict[str, Optional[AIModel]] = {}
        aggregates: dict[str, _SchemaCostAggregate] = {}

        for row in rows:
            tools_meta = _tools_meta(row)
            if tools_meta is None:
                continue
            model_id = str(row.ai_model_id) if row.ai_model_id is not None else None
            if model_id is not None and model_id not in model_cache:
                model_cache[model_id] = crud_ai_model.get(self.db, id=model_id)
            model = model_cache.get(model_id) if model_id is not None else None
            price_per_token, _ = _per_prompt_token_price(row, model)
            principal_id = (
                str(row.runtime_principal_id)
                if row.runtime_principal_id is not None
                else None
            )
            principal_name = row.runtime_principal_name
            principal_type = row.runtime_principal_type

            for tool in tools_meta:
                name = tool.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                schema_tokens = _coerce_int(tool.get("schema_tokens_estimate"))
                source = str(tool.get("source") or "payload")
                tool_cost = schema_tokens * price_per_token
                agg = aggregates.get(name)
                if agg is None:
                    agg = _SchemaCostAggregate(tool_name=name, tool_source=source)
                    aggregates[name] = agg
                agg.schema_injections += 1
                agg.schema_tokens_total += schema_tokens
                agg.estimated_schema_cost += tool_cost
                if principal_id:
                    agg.agent_costs[principal_id] = (
                        agg.agent_costs.get(principal_id, 0.0) + tool_cost
                    )
                    if principal_name:
                        agg.agent_names[principal_id] = principal_name
                    if principal_type:
                        agg.agent_types[principal_id] = principal_type

        return aggregates
