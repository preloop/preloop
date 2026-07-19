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
    monkeypatch.delenv("PRELOOP_INSTALL_STARTED_AT", raising=False)
    monkeypatch.delenv("PRELOOP_INSTALL_COMPLETED_AT", raising=False)
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
    # Marker already emitted: this test checks the steady-state payload.
    instance = _make_instance(
        version="9.9.9",
        edition="enterprise",
        metadata_={
            "key": "value",
            instance_service.INSTALL_COMPLETED_EMITTED_KEY: "2026-01-01T00:00:00+00:00",
        },
    )

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
    assert payload["metadata"] == {
        "key": "value",
        instance_service.INSTALL_COMPLETED_EMITTED_KEY: "2026-01-01T00:00:00+00:00",
    }


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
    """None metadata becomes a dict; a fresh instance carries only the marker."""
    instance = _make_instance(metadata_=None)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with (
        patch.object(instance_service.httpx, "AsyncClient", return_value=mock_client),
        patch(
            "preloop.models.db.session.get_db_session",
            side_effect=lambda: iter([MagicMock()]),
        ),
    ):
        await send_version_check(instance)
    payload = mock_client.post.call_args.kwargs["json"]
    assert payload["metadata"] == {"install_completed": True}


# --- install_completed funnel marker ----------------------------------------


def _mock_success_client(response_json=None):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = response_json or {}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    return mock_client


class TestInstallCompletedMarker:
    """The one-time install_completed marker on the version-check channel."""

    @pytest.mark.asyncio
    async def test_sent_on_first_successful_checkin_and_flag_persists(self):
        instance = _make_instance(metadata_={})
        row = SimpleNamespace(metadata_={})
        db = _mock_db_with_row(row)
        mock_client = _mock_success_client()

        with (
            patch.object(
                instance_service.httpx, "AsyncClient", return_value=mock_client
            ),
            patch(
                "preloop.models.db.session.get_db_session",
                side_effect=lambda: iter([db]),
            ),
        ):
            assert await send_version_check(instance) is True

        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["metadata"]["install_completed"] is True
        # Exactly-once guard persisted in the Instance row AND in-memory.
        key = instance_service.INSTALL_COMPLETED_EMITTED_KEY
        assert key in row.metadata_
        assert key in instance.metadata_

    @pytest.mark.asyncio
    async def test_sent_exactly_once_across_consecutive_checks(self):
        instance = _make_instance(metadata_={})
        row = SimpleNamespace(metadata_={})
        db = _mock_db_with_row(row)
        mock_client = _mock_success_client()

        with (
            patch.object(
                instance_service.httpx, "AsyncClient", return_value=mock_client
            ),
            patch(
                "preloop.models.db.session.get_db_session",
                side_effect=lambda: iter([db]),
            ),
        ):
            await send_version_check(instance)
            await send_version_check(instance)

        first_payload = mock_client.post.call_args_list[0].kwargs["json"]
        second_payload = mock_client.post.call_args_list[1].kwargs["json"]
        assert first_payload["metadata"]["install_completed"] is True
        assert "install_completed" not in second_payload["metadata"]

    @pytest.mark.asyncio
    async def test_not_marked_emitted_when_post_fails(self):
        """A failed delivery must retry the marker on the next check-in."""
        instance = _make_instance(metadata_={})
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = "unavailable"
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with patch.object(
            instance_service.httpx, "AsyncClient", return_value=mock_client
        ):
            assert await send_version_check(instance) is False

        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["metadata"]["install_completed"] is True
        # Not emitted: the flag must not persist on failure.
        assert instance_service.INSTALL_COMPLETED_EMITTED_KEY not in instance.metadata_

    def test_suppressed_when_telemetry_disabled(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "true")
        instance = _make_instance(metadata_={})
        assert instance_service.should_emit_install_completed(instance) is False

    @pytest.mark.asyncio
    async def test_suppressed_when_hosted(self, monkeypatch):
        """Hosted preloop.ai must never report its own install as a funnel hit."""
        monkeypatch.setenv("PRELOOP_HOSTED", "1")
        instance = _make_instance(metadata_={})
        mock_client = _mock_success_client()

        with patch.object(
            instance_service.httpx, "AsyncClient", return_value=mock_client
        ):
            await send_version_check(instance)

        payload = mock_client.post.call_args.kwargs["json"]
        assert "install_completed" not in payload["metadata"]
        assert instance_service.INSTALL_COMPLETED_EMITTED_KEY not in instance.metadata_

    @pytest.mark.asyncio
    async def test_installer_timestamps_included_when_stamped(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_INSTALL_STARTED_AT", "2026-07-18T10:00:00Z")
        monkeypatch.setenv("PRELOOP_INSTALL_COMPLETED_AT", "2026-07-18T10:04:30Z")
        instance = _make_instance(metadata_={})
        row = SimpleNamespace(metadata_={})
        db = _mock_db_with_row(row)
        mock_client = _mock_success_client()

        with (
            patch.object(
                instance_service.httpx, "AsyncClient", return_value=mock_client
            ),
            patch(
                "preloop.models.db.session.get_db_session",
                side_effect=lambda: iter([db]),
            ),
        ):
            await send_version_check(instance)

        metadata = mock_client.post.call_args.kwargs["json"]["metadata"]
        assert metadata["install_started_at"] == "2026-07-18T10:00:00Z"
        assert metadata["install_completed_at"] == "2026-07-18T10:04:30Z"

    @pytest.mark.asyncio
    async def test_installer_timestamps_never_synthesized(self):
        """No .env stamps -> no timestamp fields, not even empty ones."""
        instance = _make_instance(metadata_={})
        row = SimpleNamespace(metadata_={})
        db = _mock_db_with_row(row)
        mock_client = _mock_success_client()

        with (
            patch.object(
                instance_service.httpx, "AsyncClient", return_value=mock_client
            ),
            patch(
                "preloop.models.db.session.get_db_session",
                side_effect=lambda: iter([db]),
            ),
        ):
            await send_version_check(instance)

        metadata = mock_client.post.call_args.kwargs["json"]["metadata"]
        assert "install_started_at" not in metadata
        assert "install_completed_at" not in metadata


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
