"""Price recovery after a preflight denial for a model without a price (#801)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _gateway_service() -> Any:
    from preloop.services.model_gateway_auth import ModelGatewayAuthContext
    from preloop.services.openai_gateway import OpenAIGatewayService

    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    service = OpenAIGatewayService(MagicMock(), auth_context)
    service.budget_enforcer = object()
    return service


@pytest.mark.parametrize(
    ("reason", "expect_lookup"),
    [
        ("pricing_required_for_budget_enforcement", True),
        ("free_hosted_model_budget_exceeded", False),
    ],
)
def test_pricing_required_denial_schedules_a_price_lookup(
    reason: str, expect_lookup: bool
) -> None:
    """A preflight denial for a missing price must still start recovery.

    The denied request is recorded with zero tokens, and the on-miss lookup
    only fires for unpriced rows that carry tokens, so before #801 a model
    denied for lacking a price was never looked up and every retry was
    denied the same way, with no log line.
    """
    service = _gateway_service()
    ai_model = SimpleNamespace(id="model-801")
    denial = SimpleNamespace(hard_limit_exceeded=True, enforcement_reason=reason)

    with (
        patch.object(service, "_check_per_execution_limits"),
        patch(
            "preloop.services.openai_gateway.ModelGatewayBudgetService"
        ) as budget_service,
        patch("preloop.services.openai_gateway.schedule_price_lookup") as schedule,
    ):
        budget_service.return_value.preflight_check.return_value = denial
        assert service._check_budget(ai_model, {}) is denial

    if expect_lookup:
        schedule.assert_called_once_with(ai_model_id="model-801")
    else:
        schedule.assert_not_called()
