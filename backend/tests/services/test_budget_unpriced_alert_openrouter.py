"""The budget pre-check must not page a pricing gap for provider-priced routes.

An OpenRouter model has no catalog price before the request, but its usage is
priced from the per-request cost OpenRouter returns. A pre-request alert
("Tokens on triggering request: 0") is not a pricing gap there; the recording
path pages only when a request with tokens stays unpriced.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.model_gateway_budget_enforcer import ModelGatewayBudgetEnforcer


def _model(provider: str, endpoint: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        provider_name=provider,
        api_endpoint=endpoint,
        model_identifier="example/model",
        meta_data={},
    )


@pytest.mark.parametrize(
    ("ai_model", "accounting", "expected"),
    [
        (_model("openrouter"), "true", False),
        (_model("openrouter"), "false", True),
        (_model("openai"), "true", True),
    ],
)
def test_budget_precheck_pages_only_without_provider_cost(
    monkeypatch: pytest.MonkeyPatch,
    ai_model: SimpleNamespace,
    accounting: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("OPENROUTER_USAGE_ACCOUNTING", accounting)
    with patch("preloop.services.unpriced_model_alert.notify_unpriced_model") as notify:
        ModelGatewayBudgetEnforcer()._alert_unpriced_model(
            MagicMock(),
            account_id="00000000-0000-0000-0000-000000000001",
            ai_model=ai_model,
            model_alias="example/model",
            provider=ai_model.provider_name,
        )
    assert notify.called is expected
