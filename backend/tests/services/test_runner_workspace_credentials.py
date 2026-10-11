"""Short-lived clone credentials for remote session checkouts (#1484).

The GitHub path must ask for exactly one repository with read-only contents,
the Bitbucket path must use the managed OAuth grant, every long-lived tracker
secret must be refused by name before a credential exists, and the token must
never reach a database row or a log line.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from preloop.services import managed_credentials as mc
from preloop.services import runner_workspace_credentials as rwc
from preloop.services.runner_service import persistable_job_payload

pytestmark = pytest.mark.asyncio

GITHUB_TOKEN = "ghs_scoped_read_token_0001"
BITBUCKET_TOKEN = "bb_oauth_access_token_0002"


def github_app_tracker(**overrides: Any) -> Any:
    base = dict(
        id=uuid4(),
        account_id=uuid4(),
        tracker_type="github",
        auth_type="github_app",
        connection_details={},
        oauth_installation=SimpleNamespace(external_id="123", provider="github"),
        resolved_api_key="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def bitbucket_tracker(**overrides: Any) -> Any:
    base = dict(
        id=uuid4(),
        account_id=uuid4(),
        tracker_type="bitbucket",
        auth_type="managed_oauth",
        connection_details={"workspace": "example"},
        oauth_installation=None,
        resolved_api_key="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Resolver:
    def __init__(self, token: str, remaining: timedelta = timedelta(hours=2)):
        self.token = token
        self.remaining = remaining
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            access_token=self.token,
            expires_at=datetime.now(timezone.utc) + self.remaining,
            rotation_version=1,
            git_username="x-token-auth",
        )


@pytest.fixture(autouse=True)
def _clear_resolver():
    yield
    mc.register_managed_resolver("bitbucket", None)


class _AuditSpy:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_action(self, db: Any, **kwargs: Any) -> None:
        self.rows.append(kwargs)


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> _AuditSpy:
    spy = _AuditSpy()
    monkeypatch.setattr(rwc, "crud_audit_log", spy)
    return spy


def _github_transport(
    seen: list[dict[str, Any]], *, permissions: dict[str, str] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "body": json.loads(request.content),
                "auth": request.headers.get("Authorization"),
            }
        )
        return httpx.Response(
            201,
            json={
                "token": GITHUB_TOKEN,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=60)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "repositories": [{"full_name": "example/app"}],
                "permissions": permissions or {"contents": "read", "metadata": "read"},
            },
        )

    return httpx.MockTransport(handler)


def _github_signing():
    return (
        patch(
            "preloop.services.publication_credentials.settings.github_app",
            SimpleNamespace(app_id="1", private_key="-----BEGIN KEY-----"),
        ),
        patch(
            "preloop.services.publication_credentials.jwt.encode",
            return_value="app-jwt",
        ),
    )


async def test_github_app_token_is_scoped_to_one_repository_read_only(
    audit: _AuditSpy, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[dict[str, Any]] = []
    tracker = github_app_tracker()
    signing, encode = _github_signing()
    with signing, encode, caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(transport=_github_transport(seen)) as client:
            credential = await rwc.mint_checkout_credential(
                tracker,
                "example/app",
                db=object(),
                actor_user_id=uuid4(),
                remote_session_id=uuid4(),
                audit_context={
                    "runner_id": "r1",
                    "runner_name": "laptop",
                    "harness": "copilot_cli",
                    "token": "must-not-appear",
                },
                client=client,
            )
    assert len(seen) == 1
    assert seen[0]["url"].endswith("/app/installations/123/access_tokens")
    assert seen[0]["body"] == {
        "repositories": ["app"],
        "permissions": {"contents": "read"},
    }
    assert credential.username == "x-access-token"
    assert credential.token == GITHUB_TOKEN
    assert credential.provider == "github"
    assert 0 < credential.seconds_remaining() <= 3600
    wire = credential.as_wire()
    assert set(wire) == {"username", "token", "expires_at"}
    assert wire["expires_at"].endswith("Z")
    assert credential["token"] == GITHUB_TOKEN
    assert GITHUB_TOKEN not in repr(credential)
    # Audit row: provider, repository, expiry, context; never the token.
    assert len(audit.rows) == 1
    row = audit.rows[0]
    assert row["action"] == "runner_session.checkout_credential_minted"
    assert row["resource_type"] == "runner_session"
    assert row["details"]["provider"] == "github"
    assert row["details"]["repository"] == "example/app"
    assert row["details"]["runner_name"] == "laptop"
    assert row["details"]["expires_at"] == wire["expires_at"]
    assert "token" not in row["details"]
    assert GITHUB_TOKEN not in json.dumps(row, default=str)
    assert GITHUB_TOKEN not in caplog.text


async def test_github_overscoped_answer_is_refused(audit: _AuditSpy) -> None:
    seen: list[dict[str, Any]] = []
    signing, encode = _github_signing()
    with signing, encode:
        async with httpx.AsyncClient(
            transport=_github_transport(
                seen, permissions={"contents": "write", "metadata": "read"}
            )
        ) as client:
            with pytest.raises(rwc.CheckoutCredentialError) as error:
                await rwc.mint_checkout_credential(
                    github_app_tracker(), "example/app", db=object(), client=client
                )
    assert error.value.code == "checkout_credential_unavailable"
    assert audit.rows == []


async def test_github_pat_tracker_is_refused_before_any_request(
    audit: _AuditSpy,
) -> None:
    seen: list[dict[str, Any]] = []
    tracker = github_app_tracker(
        auth_type="api_token", oauth_installation=None, resolved_api_key="ghp_longlived"
    )
    async with httpx.AsyncClient(transport=_github_transport(seen)) as client:
        with pytest.raises(rwc.CheckoutCredentialError) as error:
            await rwc.mint_checkout_credential(
                tracker, "example/app", db=object(), client=client
            )
    assert error.value.code == "checkout_requires_app_or_oauth"
    assert seen == []
    assert audit.rows == []
    assert "ghp_longlived" not in str(error.value)


async def test_github_app_without_installation_is_refused() -> None:
    tracker = github_app_tracker(oauth_installation=None)
    with pytest.raises(rwc.CheckoutCredentialError) as error:
        await rwc.mint_checkout_credential(tracker, "example/app")
    assert error.value.code == "checkout_requires_app_or_oauth"


async def test_github_missing_signing_configuration_is_named() -> None:
    with patch(
        "preloop.services.publication_credentials.settings.github_app",
        SimpleNamespace(app_id="", private_key=""),
    ):
        with pytest.raises(rwc.CheckoutCredentialError) as error:
            await rwc.mint_checkout_credential(github_app_tracker(), "example/app")
    assert error.value.code == "checkout_credential_unavailable"


async def test_bitbucket_managed_oauth_path_mints_through_the_grant(
    audit: _AuditSpy, caplog: pytest.LogCaptureFixture
) -> None:
    resolver = _Resolver(BITBUCKET_TOKEN)
    mc.register_managed_resolver("bitbucket", resolver)
    tracker = bitbucket_tracker()
    with caplog.at_level(logging.INFO):
        credential = await rwc.mint_checkout_credential(
            tracker, "example/ims", db=object(), actor_user_id=uuid4()
        )
    assert credential.provider == "bitbucket_cloud"
    assert credential.username == "x-token-auth"
    assert credential.token == BITBUCKET_TOKEN
    assert 0 < credential.seconds_remaining() <= 2 * 3600
    assert len(resolver.calls) == 1
    assert resolver.calls[0]["repository"] == "example/ims"
    assert resolver.calls[0]["force_refresh"] is False
    assert resolver.calls[0]["account_id"] == tracker.account_id
    assert audit.rows[0]["details"]["provider"] == "bitbucket_cloud"
    assert BITBUCKET_TOKEN not in json.dumps(audit.rows, default=str)
    assert BITBUCKET_TOKEN not in caplog.text


async def test_bitbucket_nearly_expired_grant_is_rotated_first() -> None:
    resolver = _Resolver(BITBUCKET_TOKEN, remaining=timedelta(seconds=30))
    mc.register_managed_resolver("bitbucket", resolver)
    await rwc.mint_checkout_credential(bitbucket_tracker(), "example/ims")
    assert [call["force_refresh"] for call in resolver.calls] == [False, True]


@pytest.mark.parametrize(
    "overrides, reason",
    [
        (
            {"auth_type": "api_token", "connection_details": {"workspace": "w"}},
            "API token",
        ),
        (
            {
                "auth_type": "api_token",
                "connection_details": {
                    "workspace": "w",
                    "token_kind": "access_token",
                    "repository": "example/ims",
                },
            },
            "access token",
        ),
        (
            {"auth_type": "app_password", "connection_details": {"workspace": "w"}},
            "app password",
        ),
        (
            {
                "auth_type": "api_token",
                "connection_details": {"workspace": "w", "token_kind": "app_password"},
            },
            "app password",
        ),
        (
            {"auth_type": "oauth_token", "connection_details": {"workspace": "w"}},
            "pasted OAuth",
        ),
    ],
)
async def test_bitbucket_long_lived_secrets_are_refused_before_resolution(
    audit: _AuditSpy, overrides: dict[str, Any], reason: str
) -> None:
    resolver = _Resolver(BITBUCKET_TOKEN)
    mc.register_managed_resolver("bitbucket", resolver)
    tracker = bitbucket_tracker(resolved_api_key="ATBBsecret", **overrides)
    with pytest.raises(rwc.CheckoutCredentialError) as error:
        await rwc.mint_checkout_credential(tracker, "example/ims", db=object())
    assert error.value.code == "checkout_requires_oauth"
    assert reason in str(error.value)
    assert "ATBBsecret" not in str(error.value)
    assert resolver.calls == []
    assert audit.rows == []


async def test_bitbucket_resolver_failure_is_named() -> None:
    class _Failing:
        async def resolve(self, **kwargs: Any) -> Any:
            raise mc.ManagedReconnectRequiredError("grant_revoked")

    mc.register_managed_resolver("bitbucket", _Failing())
    with pytest.raises(rwc.CheckoutCredentialError) as error:
        await rwc.mint_checkout_credential(bitbucket_tracker(), "example/ims")
    assert error.value.code == "checkout_credential_unavailable"
    assert "reconnect" in str(error.value)


@pytest.mark.parametrize("tracker_type", ["gitlab", "jira", "", None])
async def test_unsupported_provider_is_refused(tracker_type: Any) -> None:
    tracker = bitbucket_tracker(tracker_type=tracker_type)
    with pytest.raises(rwc.CheckoutCredentialError) as error:
        await rwc.mint_checkout_credential(tracker, "example/ims")
    assert error.value.code == "checkout_provider_unsupported"


@pytest.mark.parametrize(
    "repository",
    ["", "app", "a/b/c", "../b", "-x/b", "https://github.com/a/b", "a b/c", "a/.."],
)
async def test_invalid_repository_is_refused(repository: str) -> None:
    with pytest.raises(rwc.CheckoutCredentialError) as error:
        await rwc.mint_checkout_credential(github_app_tracker(), repository)
    assert error.value.code == "checkout_repository_invalid"


def test_persistable_job_payload_strips_checkout_credentials() -> None:
    payload = {
        "execution_id": "exec-1",
        "workspace": {
            "kind": "tracker_checkout",
            "repository": "example/app",
            "credential": {"username": "u", "token": GITHUB_TOKEN, "expires_at": "x"},
        },
        "credential": {"token": GITHUB_TOKEN},
        "nested": [{"credential": {"token": GITHUB_TOKEN}, "keep": 1}],
    }
    stored = persistable_job_payload(payload)
    assert GITHUB_TOKEN not in json.dumps(stored)
    assert stored["workspace"] == {
        "kind": "tracker_checkout",
        "repository": "example/app",
    }
    assert stored["nested"] == [{"keep": 1}]
    # The live payload is untouched.
    assert payload["workspace"]["credential"]["token"] == GITHUB_TOKEN


def test_normalize_authorized_directories_keeps_ids_labels_modes_only() -> None:
    raw = {
        "authorized_directories": [
            {
                "id": "dir_9f2c",
                "label": "ims",
                "mode": "write",
                "harnesses": "all",
                "path": "/home/u/ims",
            },
            {
                "id": "dir_a",
                "label": "",
                "mode": "read_only",
                "harnesses": ["copilot_cli", "Bad Id"],
            },
            {"id": "dir_9f2c", "label": "dup", "mode": "write"},
            {"id": "bad id", "label": "x", "mode": "write"},
            {"id": "dir_b", "label": "x", "mode": "rw"},
            {"id": "dir_c", "label": "x", "mode": "write", "harnesses": ["Bad Id"]},
            {"id": "dir_d", "label": "ctl\x01chars", "mode": "write", "harnesses": []},
            "not an object",
        ]
    }
    normalized = rwc.normalize_authorized_directories(raw)
    assert normalized == [
        {"id": "dir_9f2c", "label": "ims", "mode": "write", "harnesses": "all"},
        {
            "id": "dir_a",
            "label": "dir_a",
            "mode": "read_only",
            "harnesses": ["copilot_cli"],
        },
        {"id": "dir_d", "label": "ctlchars", "mode": "write", "harnesses": "all"},
    ]
    assert "path" not in json.dumps(normalized)
    assert rwc.normalize_authorized_directories(None) == []
    assert rwc.normalize_authorized_directories({"authorized_directories": "x"}) == []
    runner = SimpleNamespace(capabilities={"authorized_directories": normalized[:1]})
    assert rwc.runner_authorized_directories(runner) == normalized[:1]
    assert rwc.runner_authorized_directories(SimpleNamespace(capabilities=None)) == []
