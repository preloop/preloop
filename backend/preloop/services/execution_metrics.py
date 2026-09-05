"""Service for calculating flow execution metrics."""

import logging
import re
from typing import Dict

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.model_pricing import estimate_ai_model_usage_cost

logger = logging.getLogger(__name__)

#: ``flow_execution.estimated_cost`` is ``Numeric(10, 4)``; quantize writes so
#: comparisons against the stored value are stable.
_ROLLUP_DECIMALS = 4


def sync_execution_cost_rollup(db: Session, execution_id: str) -> bool:
    """Recompute ``flow_execution.estimated_cost`` from attributed usage rows.

    The rollup is written once when the execution finishes, but most gateway
    usage rows are priced *later*: the live price lookup and the repricing
    backfill fill in ``api_usage.estimated_cost`` after the fact (issue #209).
    This helper re-derives the stored rollup with the same attribution rule
    the metrics endpoint uses (``action_type='model_gateway'`` rows whose
    ``flow_execution_id`` matches, replay-validation traffic excluded) so the
    per-run number a user sees equals the sum of the usage rows behind it.

    When no attributable row could be priced, the rollup is set to ``NULL``
    ("unknown"), never a placeholder ``0.0`` that would read as "free" —
    matching the metrics endpoint's semantics.

    Executions without any attributed gateway rows are left untouched: their
    rollup may come from the legacy log-parsing path, which this helper has
    no basis to overwrite.

    Args:
        db: Database session.
        execution_id: The flow execution whose rollup to refresh.

    Returns:
        True when the execution has attributed gateway usage and its rollup
        was recomputed (even if the value was already in sync); False when
        the execution does not exist or has no attributed gateway rows.
    """
    from preloop.models.crud import crud_api_usage, crud_flow_execution

    execution = crud_flow_execution.get(db, id=execution_id)
    if execution is None:
        return False

    usage = crud_api_usage.get_gateway_usage_for_execution(db, execution_id)
    if not usage["api_requests"]:
        return False

    raw_cost = usage["estimated_cost"]
    new_cost = None if raw_cost is None else round(float(raw_cost), _ROLLUP_DECIMALS)
    old_cost = (
        None if execution.estimated_cost is None else float(execution.estimated_cost)
    )
    if old_cost != new_cost:
        logger.info(
            "Syncing cost rollup for execution %s: %s -> %s",
            execution_id,
            old_cost,
            new_cost,
        )
        execution.estimated_cost = new_cost
        db.add(execution)
        db.flush()
    return True


class ExecutionMetricsService:
    """Calculate metrics for flow executions including token usage and costs."""

    def __init__(self, db: Session):
        self.db = db

    def get_execution_metrics(self, execution_id: str) -> Dict:
        """Get comprehensive metrics for a flow execution.

        Args:
            execution_id: UUID of the flow execution

        Returns:
            Dictionary with:
            - tool_calls: Number of MCP tool calls
            - api_requests: Number of API requests made
            - token_usage: Token usage from codex logs
            - estimated_cost: Estimated cost in USD, or None when no usage
              could be priced (never a placeholder 0.0)
            - has_pricing: Whether any request could be priced
            - cost_is_partial: Whether the cost excludes unpriced requests
            - unpriced_requests: Number of requests that could not be priced
            - unpriced_tokens: Token volume behind the unpriced requests
        """
        from preloop.models.crud import crud_flow_execution

        execution = crud_flow_execution.get(self.db, id=execution_id)

        if not execution:
            raise ValueError(f"Execution {execution_id} not found")

        # Parse logs for tool calls
        tool_calls = self._count_tool_calls(execution)

        # Query API usage for this execution
        gateway_usage = self._get_gateway_usage(execution)
        api_requests = gateway_usage["api_requests"]

        cost_is_partial = False
        unpriced_requests = 0
        unpriced_tokens = 0
        if api_requests > 0:
            token_usage = gateway_usage["token_usage"]
            estimated_cost = gateway_usage["estimated_cost"]
            has_pricing = gateway_usage["has_pricing"]
            cost_is_partial = bool(gateway_usage.get("cost_is_partial"))
            unpriced_requests = int(gateway_usage.get("unpriced_requests") or 0)
            unpriced_tokens = int(gateway_usage.get("unpriced_tokens") or 0)
        else:
            # Fall back to legacy log parsing when the execution did not use
            # explicit gateway attribution.
            token_usage = self._parse_token_usage(execution)
            estimated_cost, has_pricing = self._calculate_cost(execution, token_usage)
            if not has_pricing and token_usage.get("total_tokens"):
                # Tokens were spent but no price resolved: report the volume
                # rather than a $0.00 that the user would read as "free".
                estimated_cost = None
                unpriced_tokens = int(token_usage.get("total_tokens") or 0)
                unpriced_requests = 1

        return {
            "tool_calls": tool_calls,
            "api_requests": api_requests,
            "token_usage": token_usage,
            "estimated_cost": estimated_cost,
            "has_pricing": has_pricing,
            "cost_is_partial": cost_is_partial,
            "unpriced_requests": unpriced_requests,
            "unpriced_tokens": unpriced_tokens,
        }

    def _get_gateway_usage(self, execution: models.FlowExecution) -> Dict:
        """Return explicit gateway usage totals for an execution when available."""
        from preloop.models.crud import crud_api_usage

        return crud_api_usage.get_gateway_usage_for_execution(self.db, execution.id)

    def _count_tool_calls(self, execution: models.FlowExecution) -> int:
        """Count tool calls from execution logs.

        Args:
            execution: FlowExecution model

        Returns:
            Number of tool calls
        """
        count = 0

        # Count from mcp_usage_logs if available
        if execution.mcp_usage_logs and isinstance(execution.mcp_usage_logs, list):
            count += len(execution.mcp_usage_logs)

        # Count from normalized log_entries (new table) if available
        if execution.log_entries:
            for entry in execution.log_entries:
                if entry.log_type in ["tool_call", "mcp_call"]:
                    count += 1
        elif execution.execution_logs and isinstance(execution.execution_logs, list):
            # Legacy fallback: count from JSONB execution_logs
            for log in execution.execution_logs:
                if isinstance(log, dict) and log.get("type") in [
                    "tool_call",
                    "mcp_call",
                ]:
                    count += 1

        return count

    def _parse_token_usage(self, execution: models.FlowExecution) -> Dict[str, int]:
        """Parse token usage from codex output logs.

        Looks for pattern: "tokens used\n{number}"

        Args:
            execution: FlowExecution model

        Returns:
            Dictionary with total_tokens, input_tokens, output_tokens
        """
        token_usage = {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0}

        # Regex pattern for token usage (supports comma-separated thousands)
        # Pattern: "tokens used" followed by newline and number with optional commas
        pattern = r"tokens used[:\s]*\n\s*(\d{1,3}(?:,\d{3})*)"

        logs_text = ""

        # Prefer normalized log_entries table; fall back to legacy JSONB
        if execution.log_entries:
            for entry in execution.log_entries:
                if entry.message:
                    logs_text += entry.message + "\n"
                if entry.metadata_ and isinstance(entry.metadata_, dict):
                    for key in ["content", "message", "line", "stdout", "stderr"]:
                        if key in entry.metadata_:
                            logs_text += str(entry.metadata_[key]) + "\n"
        elif execution.execution_logs and isinstance(execution.execution_logs, list):
            for log in execution.execution_logs:
                if isinstance(log, dict):
                    if "payload" in log and isinstance(log["payload"], dict):
                        payload = log["payload"]
                        for key in ["content", "message", "line", "stdout", "stderr"]:
                            if key in payload:
                                logs_text += str(payload[key]) + "\n"

        if not logs_text:
            return token_usage

        # Find all token usage mentions
        matches = re.findall(pattern, logs_text, re.IGNORECASE | re.MULTILINE)

        if matches:
            # Sum all token usages found (remove commas first)
            total = sum(int(match.replace(",", "")) for match in matches)
            token_usage["total_tokens"] = total

            logger.info(
                f"Found {len(matches)} token usage entries in execution {execution.id}, "
                f"total: {total} tokens"
            )

        return token_usage

    def _count_api_requests(self, execution: models.FlowExecution) -> int:
        """Count API requests made during execution timeframe.

        Uses the execution's start_time and end_time to filter ApiUsage records
        by the user who owns the flow. Prefer explicit flow_execution_id
        attribution when gateway request records are available.

        Args:
            execution: FlowExecution model

        Returns:
            Number of API requests
        """
        from preloop.models.crud import crud_api_usage

        count = crud_api_usage.count_by_execution_timeframe(self.db, execution)

        logger.info(
            f"Found {count} API requests for execution {execution.id} "
            f"between {execution.start_time} and {execution.end_time or 'now'}"
        )

        return count

    def _calculate_cost(
        self, execution: models.FlowExecution, token_usage: Dict[str, int]
    ) -> tuple[float, bool]:
        """Calculate estimated cost based on token usage and model pricing.

        Args:
            execution: FlowExecution model
            token_usage: Dictionary with token counts

        Returns:
            Tuple of (estimated_cost, has_pricing_configured)
            - estimated_cost: Cost in USD (0.0 if no pricing configured)
            - has_pricing_configured: True if pricing was found in AI model metadata
        """
        total_cost = 0.0
        has_pricing = False
        total_tokens = token_usage.get("total_tokens", 0)

        if total_tokens == 0:
            return (0.0, False)

        # Get the flow and AI model
        from preloop.models.crud import crud_flow

        flow = crud_flow.get(self.db, id=execution.flow_id)

        if not flow or not flow.ai_model_id:
            # No pricing available - return 0 cost
            return (0.0, False)

        from preloop.models.crud import crud_ai_model

        ai_model = crud_ai_model.get(self.db, id=flow.ai_model_id)

        if not ai_model:
            return (0.0, False)

        from preloop.services.pricing_overrides import resolve_pricing_override

        pricing_override = None
        if ai_model.account_id:
            pricing_override = resolve_pricing_override(
                self.db,
                account_id=ai_model.account_id,
                ai_model=ai_model,
            )
        resolved_cost = estimate_ai_model_usage_cost(
            ai_model,
            prompt_tokens=token_usage.get("input_tokens", 0),
            completion_tokens=token_usage.get("output_tokens", 0),
            total_tokens=total_tokens,
            pricing_override=pricing_override,
        )
        if resolved_cost is not None:
            has_pricing = True
            total_cost = float(resolved_cost)

        if has_pricing:
            logger.info(
                f"Calculated cost for execution {execution.id}: "
                f"${total_cost:.4f} ({total_tokens} tokens)"
            )
        else:
            logger.info(
                f"No pricing configured for execution {execution.id} "
                f"({total_tokens} tokens)"
            )

        return (round(total_cost, 4), has_pricing)
