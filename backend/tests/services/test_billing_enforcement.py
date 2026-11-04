"""Core stays compatible with absent and older billing plugins."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from preloop.config import settings
from preloop.services import billing_enforcement as policy
from preloop.services.execution_concurrency import (
    ACCOUNT_CAP_KEY,
    account_running_cap,
    default_account_cap,
)
from preloop.services.hosted_spend_guard import guard_unmetered_hosted_call


@pytest.mark.parametrize(
    "env,override,expected",
    [
        (False, True, False),
        (True, False, False),
        (True, True, True),
        (True, None, True),
    ],
)
def test_optional_plugin_and_env_precedence(monkeypatch, env, override, expected):
    monkeypatch.setattr(settings, "billing_enforce_entitlements", env)
    resolver = Mock(return_value=override) if override is not None else None
    monkeypatch.setitem(
        policy.get_plugin_manager()._services, "entitlement_enforcement", resolver
    )
    assert policy.entitlements_enforced("account") is expected
    if not env:
        resolver.assert_not_called()


def test_exempt_account_bypasses_hosted_guard_and_projected_concurrency(monkeypatch):
    manager = policy.get_plugin_manager()
    resolver = Mock(return_value=False)
    meter = Mock()
    monkeypatch.setitem(manager._services, "entitlement_enforcement", resolver)
    monkeypatch.setitem(manager._services, "hosted_spend", meter)
    guard_unmetered_hosted_call(object(), account_id="account")
    meter.guard_unmetered.assert_not_called()
    account = SimpleNamespace(id="account", meta_data={ACCOUNT_CAP_KEY: 1})
    assert account_running_cap(account) == default_account_cap()
    assert account.meta_data[ACCOUNT_CAP_KEY] == 1


@pytest.mark.parametrize("allowance", [1, 20])
def test_exemption_never_lowers_a_higher_operator_allowance(monkeypatch, allowance):
    monkeypatch.setattr(settings, "flow_execution_max_running_per_account", 5)
    monkeypatch.setattr(settings, "billing_enforce_entitlements", True)
    monkeypatch.setitem(
        policy.get_plugin_manager()._services,
        "entitlement_enforcement",
        lambda account_id: False,
    )
    account = SimpleNamespace(id="account", meta_data={ACCOUNT_CAP_KEY: allowance})
    assert account_running_cap(account) == max(5, allowance)


def test_plugin_failure_retains_environment_and_existing_concurrency(
    monkeypatch, caplog
):
    monkeypatch.setattr(settings, "billing_enforce_entitlements", True)
    resolver = Mock(side_effect=RuntimeError("plugin unavailable"))
    monkeypatch.setitem(
        policy.get_plugin_manager()._services, "entitlement_enforcement", resolver
    )
    assert policy.entitlements_enforced("account") is True
    assert (
        account_running_cap(
            SimpleNamespace(id="account", meta_data={ACCOUNT_CAP_KEY: 2})
        )
        == 2
    )
    assert "entitlement" in caplog.text.lower()


def test_concurrency_env_off_wins_and_missing_account_never_calls_plugin(monkeypatch):
    monkeypatch.setattr(settings, "billing_enforce_entitlements", False)
    resolver = Mock(return_value=True)
    monkeypatch.setitem(
        policy.get_plugin_manager()._services, "entitlement_enforcement", resolver
    )
    assert (
        account_running_cap(
            SimpleNamespace(id="account", meta_data={ACCOUNT_CAP_KEY: 1})
        )
        == default_account_cap()
    )
    assert account_running_cap(None) == default_account_cap()
    assert account_running_cap(SimpleNamespace(meta_data={ACCOUNT_CAP_KEY: 1})) == 1
    resolver.assert_not_called()


def test_absent_billing_preserves_standalone_operator_concurrency(monkeypatch):
    monkeypatch.setattr(settings, "billing_enforce_entitlements", False)
    monkeypatch.setitem(
        policy.get_plugin_manager()._services, "entitlement_enforcement", None
    )
    assert (
        account_running_cap(
            SimpleNamespace(id="account", meta_data={ACCOUNT_CAP_KEY: 1})
        )
        == 1
    )
