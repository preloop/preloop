"""Unit tests for the instance registration / telemetry service."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from preloop.services import instance_service
from preloop.services.instance_service import (
    VERSION_CHECK_URL,
    is_telemetry_disabled,
    send_version_check,
    stop_version_checker,
)


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Reset env vars and module globals that leak across telemetry tests."""
    monkeypatch.delenv("PRELOOP_DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("DISABLE_VERSION_CHECK", raising=False)
    instance_service._current_instance = None
    instance_service._version_check_task = None
    yield
    instance_service._current_instance = None
    instance_service._version_check_task = None


def _make_instance(**overrides):
    data = dict(
        instance_uuid=uuid.uuid4(),
        version="1.2.3",
        edition="oss",
        metadata_={"key": "value"},
    )
    data.update(overrides)
    return SimpleNamespace(**data)


# --- is_telemetry_disabled -------------------------------------------------


def test_telemetry_enabled_by_default():
    assert is_telemetry_disabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "Yes"])
def test_telemetry_disabled_via_preloop_env(monkeypatch, value):
    monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", value)
    assert is_telemetry_disabled() is True


def test_telemetry_disabled_via_legacy_env(monkeypatch):
    monkeypatch.setenv("DISABLE_VERSION_CHECK", "1")
    assert is_telemetry_disabled() is True


def test_telemetry_not_disabled_for_other_values(monkeypatch):
    monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "no")
    assert is_telemetry_disabled() is False


# --- send_version_check ----------------------------------------------------


@pytest.mark.asyncio
async def test_send_version_check_skips_when_disabled(monkeypatch):
    monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "true")
    instance = _make_instance()
    # If telemetry is disabled, no HTTP client should be constructed.
    with patch.object(instance_service.httpx, "AsyncClient") as client_cls:
        result = await send_version_check(instance)
    assert result is False
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_send_version_check_success_posts_expected_payload():
    instance = _make_instance(version="9.9.9", edition="enterprise")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"update_available": False}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch.object(instance_service.httpx, "AsyncClient", return_value=mock_client):
        result = await send_version_check(instance)

    assert result is True
    mock_client.post.assert_awaited_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == VERSION_CHECK_URL
    payload = kwargs["json"]
    assert payload["instance_uuid"] == str(instance.instance_uuid)
    assert payload["version"] == "9.9.9"
    assert payload["edition"] == "enterprise"
    assert payload["metadata"] == {"key": "value"}


@pytest.mark.asyncio
async def test_send_version_check_handles_update_available():
    instance = _make_instance()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "update_available": True,
        "current_version": "2.0.0",
    }
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch.object(instance_service.httpx, "AsyncClient", return_value=mock_client):
        result = await send_version_check(instance)
    assert result is True


@pytest.mark.asyncio
async def test_send_version_check_non_200_returns_false():
    instance = _make_instance()
    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.text = "unavailable"
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch.object(instance_service.httpx, "AsyncClient", return_value=mock_client):
        result = await send_version_check(instance)
    assert result is False


@pytest.mark.asyncio
async def test_send_version_check_handles_timeout():
    instance = _make_instance()
    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.TimeoutException("slow")
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch.object(instance_service.httpx, "AsyncClient", return_value=mock_client):
        result = await send_version_check(instance)
    assert result is False


@pytest.mark.asyncio
async def test_send_version_check_handles_generic_error():
    instance = _make_instance()
    mock_client = AsyncMock()
    mock_client.post.side_effect = ValueError("boom")
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch.object(instance_service.httpx, "AsyncClient", return_value=mock_client):
        result = await send_version_check(instance)
    assert result is False


@pytest.mark.asyncio
async def test_send_version_check_defaults_metadata_to_empty(monkeypatch):
    instance = _make_instance(metadata_=None)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch.object(instance_service.httpx, "AsyncClient", return_value=mock_client):
        await send_version_check(instance)
    payload = mock_client.post.call_args.kwargs["json"]
    assert payload["metadata"] == {}


# --- stop_version_checker --------------------------------------------------


def test_stop_version_checker_noop_when_not_running():
    instance_service._version_check_task = None
    # Should not raise when there is no running task.
    stop_version_checker()
    assert instance_service._version_check_task is None


def test_stop_version_checker_cancels_running_task():
    fake_task = MagicMock()
    instance_service._version_check_task = fake_task
    stop_version_checker()
    fake_task.cancel.assert_called_once()
    assert instance_service._version_check_task is None


# --- get_or_create_instance ------------------------------------------------


def test_get_or_create_instance_returns_none_on_db_failure():
    # When the DB session factory raises, the service must swallow and return None.
    with patch(
        "preloop.models.db.session.get_db_session",
        side_effect=RuntimeError("no db"),
    ):
        result = instance_service.get_or_create_instance()
    assert result is None
