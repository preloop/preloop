"""A late KUBECONFIG must never select the operator's default context."""

from unittest.mock import AsyncMock

import pytest

from scripts.tests.kubernetes_guard import disposable_client


@pytest.mark.asyncio
async def test_explicit_config_after_import_is_passed_to_loader(monkeypatch) -> None:
    client = AsyncMock()
    client.configuration.host = "https://127.0.0.1:12345"
    loader = AsyncMock(return_value=client)
    monkeypatch.setattr(
        "scripts.tests.kubernetes_guard.config.new_client_from_config", loader
    )
    monkeypatch.setenv("KUBECONFIG", "/operator/config-must-not-be-used")
    result = await disposable_client(
        config_file="/tmp/disposable-config",
        context="kind-disposable",
        expected_host="https://127.0.0.1:12345",
    )
    assert result is client
    loader.assert_awaited_once_with(
        config_file="/tmp/disposable-config", context="kind-disposable"
    )


@pytest.mark.asyncio
async def test_actual_remote_client_rejected_before_mutation(monkeypatch) -> None:
    client = AsyncMock()
    client.configuration.host = "https://cluster.example.com:6443"
    loader = AsyncMock(return_value=client)
    monkeypatch.setattr(
        "scripts.tests.kubernetes_guard.config.new_client_from_config", loader
    )
    with pytest.raises(ValueError, match="does not match"):
        await disposable_client(
            config_file="/tmp/disposable",
            context="kind-disposable",
            expected_host="https://127.0.0.1:12345",
        )
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_expected_host_rejected_without_loading(monkeypatch) -> None:
    loader = AsyncMock()
    monkeypatch.setattr(
        "scripts.tests.kubernetes_guard.config.new_client_from_config", loader
    )
    with pytest.raises(ValueError, match="loopback"):
        await disposable_client(
            config_file="/tmp/disposable",
            context="kind-disposable",
            expected_host="https://cluster.example.com:6443",
        )
    loader.assert_not_awaited()
