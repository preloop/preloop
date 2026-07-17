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
    monkeypatch.delenv("PRELOOP_HOSTED", raising=False)
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


# --- version comparison ----------------------------------------------------


class TestIsNewerVersion:
    def test_strictly_newer(self):
        assert instance_service.is_newer_version("1.0.0", "0.11.1") is True
        assert instance_service.is_newer_version("0.11.2", "0.11.1") is True

    def test_equal_is_not_newer(self):
        assert instance_service.is_newer_version("0.11.1", "0.11.1") is False

    def test_older_is_not_newer(self):
        assert instance_service.is_newer_version("0.10.0", "0.11.1") is False

    def test_v_prefix_and_padding(self):
        assert instance_service.is_newer_version("v1.0", "0.11.1") is True
        assert instance_service.is_newer_version("1.0", "1.0.0") is False

    def test_unparseable_candidate_is_not_newer(self):
        assert instance_service.is_newer_version("banana", "0.11.1") is False
        assert instance_service.is_newer_version("", "0.11.1") is False
        assert instance_service.is_newer_version(None, "0.11.1") is False

    def test_prerelease_suffix_ignored(self):
        assert instance_service.is_newer_version("1.0.0-rc1", "0.11.1") is True


# --- hosted kill switch -----------------------------------------------------


class TestHostedInstance:
    @pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE"])
    def test_hosted_env_truthy(self, monkeypatch, value):
        monkeypatch.setenv("PRELOOP_HOSTED", value)
        assert instance_service.is_hosted_instance() is True

    def test_not_hosted_by_default(self):
        assert instance_service.is_hosted_instance() is False

    def test_status_empty_when_hosted(self, monkeypatch):
        """Hosted preloop.ai must never report an update to its own console."""
        monkeypatch.setenv("PRELOOP_HOSTED", "1")
        status = instance_service.get_local_update_status()
        assert status["update_available"] is False
        assert status["latest_version"] is None

    def test_persist_skipped_when_hosted(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_HOSTED", "1")
        with patch(
            "preloop.models.db.session.get_db_session",
            side_effect=AssertionError("must not touch the DB when hosted"),
        ):
            instance_service._persist_version_check_result(
                {"update_available": True, "current_version": "99.0.0"}
            )


# --- update status recompute ------------------------------------------------


def _mock_db_with_row(row):
    db = MagicMock()
    db.query.return_value.first.return_value = row
    return db


class TestGetLocalUpdateStatus:
    def _status_with_persisted(self, monkeypatch, version_check):
        row = SimpleNamespace(metadata_={"version_check": version_check})
        db = _mock_db_with_row(row)
        with patch(
            "preloop.models.db.session.get_db_session",
            return_value=iter([db]),
        ):
            return instance_service.get_local_update_status()

    def test_recomputes_stale_persisted_boolean(self, monkeypatch):
        """A persisted update_available=True with a non-newer latest is healed."""
        monkeypatch.setattr(instance_service, "SERVER_VERSION", "1.0.0")
        status = self._status_with_persisted(
            monkeypatch,
            {"update_available": True, "latest_version": "1.0.0"},
        )
        assert status["update_available"] is False

    def test_reports_update_for_strictly_newer(self, monkeypatch):
        monkeypatch.setattr(instance_service, "SERVER_VERSION", "0.11.1")
        status = self._status_with_persisted(
            monkeypatch,
            {"update_available": True, "latest_version": "0.12.0"},
        )
        assert status["update_available"] is True
        assert status["latest_version"] == "0.12.0"

    def test_unparseable_latest_is_not_an_update(self, monkeypatch):
        monkeypatch.setattr(instance_service, "SERVER_VERSION", "0.11.1")
        status = self._status_with_persisted(
            monkeypatch,
            {"update_available": True, "latest_version": "not-a-version"},
        )
        assert status["update_available"] is False


class TestPersistVersionCheckResult:
    def test_does_not_persist_update_for_non_newer_version(self, monkeypatch):
        """Self-heal: current_version <= SERVER_VERSION is never an update."""
        monkeypatch.setattr(instance_service, "SERVER_VERSION", "1.0.0")
        row = SimpleNamespace(metadata_={})
        db = _mock_db_with_row(row)
        with patch(
            "preloop.models.db.session.get_db_session",
            return_value=iter([db]),
        ):
            instance_service._persist_version_check_result(
                {"update_available": True, "current_version": "1.0.0"}
            )
        assert row.metadata_["version_check"]["update_available"] is False
        assert row.metadata_["version_check"]["latest_version"] == "1.0.0"

    def test_persists_update_for_newer_version(self, monkeypatch):
        monkeypatch.setattr(instance_service, "SERVER_VERSION", "0.11.1")
        row = SimpleNamespace(metadata_={})
        db = _mock_db_with_row(row)
        with patch(
            "preloop.models.db.session.get_db_session",
            return_value=iter([db]),
        ):
            instance_service._persist_version_check_result(
                {"update_available": True, "current_version": "0.12.0"}
            )
        assert row.metadata_["version_check"]["update_available"] is True


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
