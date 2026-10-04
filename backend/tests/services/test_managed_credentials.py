"""The open-source managed credential resolver contract (issue #1065).

A provider plugin registers ``managed_oauth_resolver:<provider>``. These tests
inject stand-ins for that interface and check that every outcome is explicit:
typed failures, no stale fallback, no secrets in representations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from preloop.services import managed_credentials as mc

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


class _Fresh:
    def __init__(self, token: str = "token-a", username: str | None = None):
        self.access_token = token
        self.expires_at = NOW + timedelta(hours=1)
        self.rotation_version = 3
        if username is not None:
            self.git_username = username


class _Resolver:
    """Records calls and returns or raises what the test configured."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


# Foreign error types shaped like the provider plugin's, never imported:
# three siblings under one base class, each carrying a short ``code``.
class ManagedCredentialError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReconnectRequiredError(ManagedCredentialError):
    pass


class CredentialPermissionError(ManagedCredentialError):
    pass


class CredentialUnavailableError(ManagedCredentialError):
    pass


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    mc.register_managed_resolver("bitbucket", None)


async def test_missing_resolver_is_an_explicit_unavailable_error() -> None:
    with pytest.raises(mc.ManagedCredentialUnavailableError) as error:
        await mc.resolve_managed_credential(
            account_id=uuid4(), tracker_id=uuid4(), provider="bitbucket"
        )
    assert error.value.code == "resolver_missing"
    assert "no managed-provider plugin" in error.value.actionable_message()


async def test_resolver_receives_binding_and_result_is_normalized() -> None:
    resolver = _Resolver(_Fresh("secret-a"))
    account_id, tracker_id = uuid4(), uuid4()
    credential = await mc.resolve_managed_credential(
        account_id=str(account_id),
        tracker_id=str(tracker_id),
        provider="bitbucket",
        repository="repo",
        resolver=resolver,
    )
    assert resolver.calls == [
        {
            "account_id": account_id,
            "tracker_id": tracker_id,
            "provider": "bitbucket",
            "repository": "repo",
            "force_refresh": False,
        }
    ]
    assert credential.access_token == "secret-a"
    assert credential.git_username == "x-token-auth"
    assert credential.expires_at.tzinfo is not None
    assert credential.rotation_version == 3
    assert "secret-a" not in repr(credential)
    assert "secret-a" not in str(credential)


@pytest.mark.parametrize(
    ("raised", "expected", "code"),
    [
        (
            ReconnectRequiredError("invalid_grant"),
            mc.ManagedReconnectRequiredError,
            "invalid_grant",
        ),
        (
            CredentialPermissionError("repository_not_bound"),
            mc.ManagedCredentialPermissionError,
            "repository_not_bound",
        ),
        (
            CredentialUnavailableError("not_configured"),
            mc.ManagedCredentialUnavailableError,
            "not_configured",
        ),
        (ManagedCredentialError("odd"), mc.ManagedCredentialUnavailableError, "odd"),
        (
            RuntimeError("provider body with secrets"),
            mc.ManagedCredentialUnavailableError,
            "resolver_error",
        ),
        (
            mc.ManagedReconnectRequiredError("disconnected"),
            mc.ManagedReconnectRequiredError,
            "disconnected",
        ),
    ],
)
async def test_foreign_failures_map_to_typed_errors(
    raised: BaseException, expected: type, code: str
) -> None:
    with pytest.raises(expected) as error:
        await mc.resolve_managed_credential(
            account_id=uuid4(),
            tracker_id=uuid4(),
            provider="bitbucket",
            resolver=_Resolver(raised),
        )
    assert error.value.code == code
    assert error.value.provider == "bitbucket"
    assert "secrets" not in str(error.value)


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(access_token="", expires_at=NOW, rotation_version=1),
        SimpleNamespace(access_token="bad token", expires_at=NOW, rotation_version=1),
        SimpleNamespace(access_token="ok", expires_at=None, rotation_version=1),
        _Fresh("ok", username="user@example.com"),
    ],
)
async def test_invalid_resolver_results_are_rejected(result: Any) -> None:
    with pytest.raises(mc.ManagedCredentialUnavailableError) as error:
        await mc.resolve_managed_credential(
            account_id=uuid4(),
            tracker_id=uuid4(),
            provider="bitbucket",
            resolver=_Resolver(result),
        )
    assert error.value.code == "invalid_credential"


async def test_registered_override_and_plugin_manager_lookup(monkeypatch) -> None:
    resolver = _Resolver(_Fresh("token-a"))
    mc.register_managed_resolver("bitbucket", resolver)
    assert mc.get_managed_resolver("bitbucket") is resolver
    mc.register_managed_resolver("bitbucket", None)

    class Manager:
        def get_service(self, name: str) -> Any:
            return resolver if name == "managed_oauth_resolver:bitbucket" else None

    import preloop.plugins.base as plugins

    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: Manager())
    assert mc.get_managed_resolver("bitbucket") is resolver
    assert mc.get_managed_resolver("gitlab") is None


def test_tracker_credential_source_binds_tenant_tracker_and_repository() -> None:
    account_id, tracker_id = uuid4(), uuid4()
    managed = SimpleNamespace(
        id=tracker_id,
        account_id=account_id,
        tracker_type="bitbucket",
        auth_type="managed_oauth",
        connection_details={"workspace": "ws", "repository": "repo"},
    )
    source = mc.tracker_credential_source(managed)
    assert source is not None
    assert (source.account_id, source.tracker_id) == (account_id, tracker_id)
    assert source.provider == "bitbucket"
    assert source.repository == "repo"
    assert (
        mc.tracker_credential_source(managed, repository="other").repository == "other"
    )
    pasted = SimpleNamespace(auth_type="oauth_token", tracker_type="bitbucket")
    assert mc.tracker_credential_source(pasted) is None
    assert mc.is_managed_tracker(None) is False
    assert mc.managed_feature_flag("bitbucket") == "bitbucket_cloud_oauth"
    assert mc.managed_feature_flag("github") is None


async def test_source_forwards_force_refresh() -> None:
    resolver = _Resolver(_Fresh("token-b"))
    source = mc.ManagedCredentialSource(
        account_id=uuid4(), tracker_id=uuid4(), provider="bitbucket", resolver=resolver
    )
    credential = await source(force_refresh=True)
    assert credential.access_token == "token-b"
    assert resolver.calls[0]["force_refresh"] is True
    assert "token-b" not in repr(source)
