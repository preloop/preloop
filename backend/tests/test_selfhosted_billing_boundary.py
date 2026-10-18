"""Deployment boundaries for optional plugin flags and hosted model caps."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.config import settings
from preloop.plugins.base import Plugin, PluginManager, PluginMetadata
from preloop.services.model_gateway_budget import ModelGatewayBudgetService


class DeploymentPlugin(Plugin):
    """A deployment can explicitly disable a frontend capability."""

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="deployment", version="1", author="test", description="test"
        )

    def get_features(self) -> dict[str, bool]:
        return {"billing": False, "cost": True}


def test_explicit_false_plugin_features_survive_serialization() -> None:
    """A disabled feature is preserved for API clients as false."""
    manager = PluginManager()
    manager.register_plugin(DeploymentPlugin())
    assert manager.get_enabled_features()["features"] == {
        "user_management": True,
        "team_management": True,
        "basic_roles": True,
        "billing": False,
        "cost": True,
    }


@pytest.mark.parametrize("trial", [True, False])
@pytest.mark.parametrize("hosted", [True, False])
def test_hosted_caps_only_apply_on_hosted_instances(
    monkeypatch: pytest.MonkeyPatch, trial: bool, hosted: bool
) -> None:
    """Free and legacy trial subscriptions cannot impose a self-hosted cap."""
    monkeypatch.setenv("PRELOOP_HOSTED", "true" if hosted else "false")
    monkeypatch.setattr(settings, "billing_enforce_entitlements", True)
    subscription = (
        SimpleNamespace(current_period_start=None, current_period_end=None)
        if trial
        else None
    )
    context = SimpleNamespace(account_id="account", api_key=None)
    model = SimpleNamespace(id="model", name="model", model_identifier="model")
    service = ModelGatewayBudgetService(MagicMock(), context)
    with (
        patch("preloop.models.crud.crud_account.get", return_value=None),
        patch(
            "preloop.models.crud.plan.subscription.get_active_for_account",
            return_value=subscription,
        ),
        patch(
            "preloop.models.crud.resource_share.crud_resource_share.shared_agent_governance",
            return_value={},
        ),
        patch("preloop.services.model_gateway_budget.is_live_trial", return_value=True),
        patch.object(service, "_pricing_override_for_request", return_value=None),
        patch.object(service, "_estimate_request_cost", return_value=100.0),
        patch.object(service, "_governed_model_spellings", return_value=["model"]),
        patch.object(service, "_is_built_in_hosted_model", return_value=True),
        patch.object(
            service, "_get_trial_hosted_model_spend", return_value=100.0
        ) as spend,
    ):
        result = service.preflight_check(model, {"model": "model"})
    assert result.hard_limit_exceeded is hosted
    if not hosted:
        spend.assert_not_called()
        assert result.trial_hosted_model_limit_usd is None
