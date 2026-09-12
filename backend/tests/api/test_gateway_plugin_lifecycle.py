"""Dedicated gateways must install request governance without API workers."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from preloop.api.app import create_app, lifespan
from preloop.api.deps import get_budget_enforcer
from preloop.plugins.base import Plugin, PluginManager, PluginMetadata


class GatewayPlugin(Plugin):
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata("test-governance", "1", "tests", "Request governance")

    def get_dependencies(self) -> dict[Any, Any]:
        return {get_budget_enforcer: self.enforcer}

    def enforcer(self) -> str:
        return "governed"

    def get_routers(self) -> list[Any]:
        raise AssertionError("A gateway must not register API-only routers")

    async def on_startup(self) -> None:
        raise AssertionError("A gateway must not start API workers")

    async def on_shutdown(self) -> None:
        raise AssertionError("A gateway must not stop API workers")

    async def on_gateway_startup(self) -> None:
        self.calls.append("start")
        if self.fail:
            raise RuntimeError("required gateway governance unavailable")

    async def on_gateway_shutdown(self) -> None:
        self.calls.append("stop")


def configure(
    monkeypatch: pytest.MonkeyPatch, plugin: GatewayPlugin | None
) -> PluginManager:
    manager = PluginManager()
    if plugin is not None:
        manager.register_plugin(plugin)
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "gateway")
    monkeypatch.setattr("preloop.plugins.get_plugin_manager", lambda: manager)
    monkeypatch.setattr("preloop.api.app.connect_nats", AsyncMock())
    monkeypatch.setattr("preloop.api.app.close_nats", AsyncMock())
    return manager


def test_gateway_installs_plugin_dependencies_without_api_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = GatewayPlugin()
    configure(monkeypatch, plugin)
    app = create_app()
    assert app.dependency_overrides[get_budget_enforcer]() == "governed"


def test_oss_gateway_keeps_noop_without_enterprise_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(monkeypatch, None)
    app = create_app()
    assert get_budget_enforcer not in app.dependency_overrides


@pytest.mark.asyncio
async def test_gateway_runs_only_gateway_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = GatewayPlugin()
    configure(monkeypatch, plugin)
    monkeypatch.setenv("TESTING", "false")
    async with lifespan(create_app()):
        assert plugin.calls == ["start"]
    assert plugin.calls == ["start", "stop"]


@pytest.mark.asyncio
async def test_gateway_refuses_startup_when_required_hook_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = GatewayPlugin(fail=True)
    configure(monkeypatch, plugin)
    monkeypatch.setenv("TESTING", "false")
    with pytest.raises(RuntimeError, match="governance unavailable"):
        async with lifespan(create_app()):
            pytest.fail("An ungoverned gateway must not accept traffic")
