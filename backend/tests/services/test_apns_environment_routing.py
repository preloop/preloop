"""APNs environment routing: sandbox tokens from Xcode builds, production
tokens from App Store and TestFlight builds."""

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest
from pydantic import ValidationError

import preloop.services.push_notifications as push_notifications
from preloop.models.models import ApprovalRequest, ApprovalWorkflow
from preloop.models.models.notification_preferences import NotificationPreferences
from preloop.schemas.notification_preferences import MobileDeviceRegistration
from preloop.services import push_proxy
from preloop.services.approval_service import ApprovalService

ENV = {
    "APNS_TEAM_ID": "TESTTEAM12",
    "APNS_KEY_ID": "TESTKEY123",
    "APNS_AUTH_KEY_PATH": "/fake/path.p8",
}


@pytest.fixture(autouse=True)
def reset_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without cached APNs clients."""
    monkeypatch.setattr(push_notifications, "_apns_services", {})
    monkeypatch.delenv("APNS_USE_SANDBOX", raising=False)


class TestEnvironmentHelpers:
    """normalize/default/resolve helpers."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("sandbox", "sandbox"),
            ("development", "sandbox"),
            (" Production ", "production"),
            ("", None),
            (None, None),
            ("staging", None),
        ],
    )
    def test_normalize(self, value: Optional[str], expected: Optional[str]) -> None:
        """Entitlement spelling 'development' maps to sandbox."""
        assert push_notifications.normalize_apns_environment(value) == expected

    def test_default_is_production(self) -> None:
        """Unlabeled tokens go to production by default."""
        assert push_notifications.default_apns_environment() == "production"

    def test_use_sandbox_keeps_old_meaning_for_unlabeled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """APNS_USE_SANDBOX=true sends unlabeled tokens to sandbox only."""
        monkeypatch.setenv("APNS_USE_SANDBOX", "true")
        assert push_notifications.resolve_apns_environment(None) == "sandbox"
        assert push_notifications.resolve_apns_environment("production") == (
            "production"
        )


class TestTwoClients:
    """get_apns_service keeps one client per environment."""

    def _get(self, environment: Optional[str]) -> Any:
        with (
            patch.dict("os.environ", ENV, clear=False),
            patch("builtins.open", mock_open(read_data="fake_key")),
        ):
            return push_notifications.get_apns_service(environment)

    def test_clients_use_matching_hosts(self) -> None:
        """Sandbox and production clients hit different APNs hosts."""
        sandbox = self._get("sandbox")
        production = self._get("production")
        assert sandbox.apns_server == "https://api.sandbox.push.apple.com"
        assert production.apns_server == "https://api.push.apple.com"
        assert sandbox is not production

    def test_default_client_is_production_and_cached(self) -> None:
        """No environment means the default (production) client, reused."""
        default = self._get(None)
        assert default is self._get("production")

    def test_select_reuses_default_for_matching_label(self) -> None:
        """Tokens labeled with the default environment reuse its client."""
        default = MagicMock()
        assert push_notifications.select_apns_service(default, None) is default
        assert push_notifications.select_apns_service(default, "production") is (
            default
        )

    def test_select_switches_for_other_label(self) -> None:
        """A sandbox label on a production server gets the sandbox client."""
        default = MagicMock()
        sandbox = MagicMock()
        with patch.object(
            push_notifications, "get_apns_service", return_value=sandbox
        ) as getter:
            chosen = push_notifications.select_apns_service(default, "sandbox")
        assert chosen is sandbox
        getter.assert_called_once_with("sandbox")

    def test_select_without_apns_returns_none(self) -> None:
        """No APNs config stays None so callers fall back to the proxy."""
        assert push_notifications.select_apns_service(None, "sandbox") is None


class TestStoredEnvironment:
    """The model and schema keep the reported environment."""

    def test_add_device_token_stores_ios_environment(self) -> None:
        """iOS entries carry apns_environment; Android entries never do."""
        prefs = NotificationPreferences(mobile_device_tokens=[])
        prefs.add_device_token("ios", "a" * 64, apns_environment="sandbox")
        prefs.add_device_token("android", "fcm-1", apns_environment="sandbox")
        by_platform = {d["platform"]: d for d in prefs.mobile_device_tokens}
        assert by_platform["ios"]["apns_environment"] == "sandbox"
        assert "apns_environment" not in by_platform["android"]
        assert prefs.get_device_entries("ios") == [("a" * 64, "sandbox")]
        assert prefs.get_device_entries("android") == [("fcm-1", None)]

    def test_unlabeled_token_has_no_environment(self) -> None:
        """Older app builds register without a label."""
        prefs = NotificationPreferences(mobile_device_tokens=[])
        prefs.add_device_token("ios", "b" * 64)
        assert "apns_environment" not in prefs.mobile_device_tokens[0]
        assert prefs.get_device_entries("ios") == [("b" * 64, None)]

    def test_schema_accepts_development_alias(self) -> None:
        """The entitlement value 'development' is stored as sandbox."""
        reg = MobileDeviceRegistration(
            platform="ios", token="a" * 64, apns_environment="development"
        )
        assert reg.apns_environment == "sandbox"

    def test_schema_rejects_unknown_environment(self) -> None:
        """Typos are refused instead of silently routed to production."""
        with pytest.raises(ValidationError):
            MobileDeviceRegistration(
                platform="ios", token="a" * 64, apns_environment="staging"
            )

    def test_schema_field_is_optional(self) -> None:
        """Older app builds that send no label still register."""
        reg = MobileDeviceRegistration(platform="ios", token="a" * 64)
        assert reg.apns_environment is None


@pytest.mark.asyncio
class TestProxyPayload:
    """The push proxy forwards the environment to the hosted instance."""

    async def _send(self, **kwargs: Any) -> Dict[str, Any]:
        response = MagicMock(status_code=200)
        response.json.return_value = {"success": True}
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__.return_value = client
        with (
            patch.object(push_proxy, "PUSH_PROXY_URL", "https://proxy.test/push"),
            patch.object(push_proxy, "PUSH_PROXY_API_KEY", "k"),
            patch("httpx.AsyncClient", return_value=client),
        ):
            await push_proxy.send_push_via_proxy(title="t", body="b", **kwargs)
        return client.post.call_args.kwargs["json"]

    async def test_ios_payload_carries_environment(self) -> None:
        """Labeled iOS tokens forward apns_environment."""
        payload = await self._send(
            platform="ios", device_token="a" * 64, apns_environment="sandbox"
        )
        assert payload["apns_environment"] == "sandbox"

    async def test_unlabeled_and_android_payloads_omit_it(self) -> None:
        """Unknown environment and Android tokens send no field."""
        ios = await self._send(platform="ios", device_token="a" * 64)
        android = await self._send(
            platform="android", device_token="f", apns_environment="sandbox"
        )
        assert "apns_environment" not in ios
        assert "apns_environment" not in android


def _approval_fixtures() -> tuple[ApprovalRequest, ApprovalWorkflow]:
    workflow = MagicMock(spec=ApprovalWorkflow)
    workflow.id = uuid.uuid4()
    request = MagicMock(spec=ApprovalRequest)
    request.id = uuid.uuid4()
    request.tool_name = "create_issue"
    request.tool_args = {}
    request.agent_reasoning = "r"
    request.summary = None
    request.rule_context = None
    request.managed_agent_name = None
    request.runtime_session_id = None
    request.expires_at = datetime.utcnow() + timedelta(minutes=5)
    request.priority = "medium"
    return request, workflow


async def _run_send(
    ios_tokens: List[tuple], production: Any, sandbox: Any
) -> tuple[Dict[str, Any], List[Any]]:
    """Run _send_push_notification with fixed tokens; return result + prunes."""
    db = AsyncMock()
    service = ApprovalService(db, "https://app.test")
    request, workflow = _approval_fixtures()
    user_id = ios_tokens[0][0]
    calls: List[Any] = []

    def run_in_executor(_executor: Any, func: Any) -> "asyncio.Future[Any]":
        future: "asyncio.Future[Any]" = asyncio.Future()
        if not calls:
            calls.append("gather")
            future.set_result(([user_id], ios_tokens, []))
        else:
            calls.append(func)
            future.set_result(None)
        return future

    def get_service(environment: Optional[str] = None) -> Any:
        return sandbox if environment == "sandbox" else production

    loop = MagicMock()
    loop.run_in_executor = run_in_executor
    with (
        patch.object(push_notifications, "get_apns_service", side_effect=get_service),
        patch("asyncio.get_event_loop", return_value=loop),
        patch(
            "preloop.services.approval_service.get_task_publisher",
            AsyncMock(return_value=None),
        ),
    ):
        result = await service._send_push_notification(request, workflow)
    return result, calls[1:]


@pytest.mark.asyncio
class TestApprovalRouting:
    """Approval pushes go to the client that matches each token."""

    async def test_sandbox_token_uses_sandbox_client(self) -> None:
        """A dev-build token reaches sandbox APNs, not production."""
        production = MagicMock()
        production.send_notification = AsyncMock(return_value=(True, 200, None))
        sandbox = MagicMock()
        sandbox.send_notification = AsyncMock(return_value=(True, 200, None))
        uid = uuid.uuid4()
        result, _ = await _run_send(
            [(uid, "a" * 64, "sandbox"), (uid, "b" * 64, None)], production, sandbox
        )
        assert result["sent"] == 2
        assert sandbox.send_notification.await_args.kwargs["device_token"] == ("a" * 64)
        assert production.send_notification.await_args.kwargs["device_token"] == (
            "b" * 64
        )

    async def test_labeled_bad_device_token_is_kept(self) -> None:
        """BadDeviceToken on a labeled token logs and does not prune."""
        production = MagicMock()
        production.send_notification = AsyncMock(
            return_value=(False, 400, "BadDeviceToken")
        )
        uid = uuid.uuid4()
        result, prune_calls = await _run_send(
            [(uid, "a" * 64, "production")], production, MagicMock()
        )
        assert result["failed"] == 1
        assert prune_calls == []

    async def test_unlabeled_bad_device_token_is_still_pruned(self) -> None:
        """Old builds keep the existing prune behaviour."""
        production = MagicMock()
        production.send_notification = AsyncMock(
            return_value=(False, 400, "BadDeviceToken")
        )
        uid = uuid.uuid4()
        _, prune_calls = await _run_send([(uid, "a" * 64, None)], production, None)
        assert len(prune_calls) == 1

    async def test_labeled_unregistered_token_is_pruned(self) -> None:
        """410 Unregistered means the app was removed; prune even if labeled."""
        sandbox = MagicMock()
        sandbox.send_notification = AsyncMock(return_value=(False, 410, "Unregistered"))
        uid = uuid.uuid4()
        _, prune_calls = await _run_send(
            [(uid, "a" * 64, "sandbox")], MagicMock(), sandbox
        )
        assert len(prune_calls) == 1
