"""Opt-in discovery reporting: salt, report, dedupe, purge, permission, link.

The privacy contract is tested through the request schema: a report can
only carry salted hashes and a few enumerated fields, so a hostname, clear
path or MCP URL is a 422, never a stored row.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.requests import Request
from fastapi import HTTPException

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.auth.key_scopes import (
    api_key_allowed_on_channel,
    enforce_api_key_route_scope,
    is_device_scoped_api_key,
)
from preloop.models.crud import crud_api_key, crud_discovered_agent_candidate, crud_user
from preloop.models.crud.discovered_agent_candidate import (
    CONSOLE_LIST_LIMIT,
    ReportedCandidate,
    utc_now_naive,
)
from preloop.models.models.discovered_agent_candidate import DiscoveredAgentCandidate
from preloop.models.models.webhook_endpoint import (
    SOURCE_ACCOUNT,
    WebhookDelivery,
    WebhookEndpoint,
)
from preloop.services.event_webhooks.events import (
    EVENT_AGENT_DISCOVERED,
    EVENT_TYPES_V1,
    deterministic_event_id,
)
from preloop.services.event_webhooks.signing import generate_secret, secret_hint
from preloop.utils.encryption import encrypt_value

REPORTS = "/api/v1/agents/discovery-reports"
SALT = "/api/v1/agents/discovery-salt"
CANDIDATES = "/api/v1/agents/discovery-candidates"


def _hmac(salt: str, value: str) -> str:
    return hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest()


def _report(fingerprint: str, *candidates: dict) -> dict:
    return {
        "workstation_fingerprint": fingerprint,
        "cli_version": "0.18.0",
        "os": "darwin",
        "candidates": list(candidates),
    }


def _candidate(kind: str = "cursor", path_hash: str = "a" * 64, **extra) -> dict:
    return {
        "agent_kind": kind,
        "config_path_hash": path_hash,
        "mcp_server_count": 2,
        "enrolled": False,
        **extra,
    }


@pytest.fixture
def webhook_endpoint(db_session, test_user):
    endpoint = WebhookEndpoint(
        account_id=test_user.account_id,
        url="https://example.com/hook",
        secret_encrypted=encrypt_value(generate_secret()),
        secret_hint=secret_hint(generate_secret()),
        event_types=[EVENT_AGENT_DISCOVERED],
        active=True,
        source=SOURCE_ACCOUNT,
    )
    db_session.add(endpoint)
    db_session.flush()
    return endpoint


def _discovered_deliveries(db_session, account_id):
    return [
        row
        for row in db_session.query(WebhookDelivery)
        .filter(WebhookDelivery.account_id == account_id)
        .all()
        if row.event_type == EVENT_AGENT_DISCOVERED
    ]


def test_event_is_in_the_v1_catalogue():
    assert EVENT_AGENT_DISCOVERED in EVENT_TYPES_V1


def test_salt_is_issued_once_and_stable(client):
    first = client.get(SALT)
    assert first.status_code == 200
    body = first.json()
    assert len(body["salt"]) == 64
    assert body["algorithm"] == "hmac-sha256"
    assert body["retention_days"] == 90
    assert client.get(SALT).json()["salt"] == body["salt"]


def test_new_candidate_emits_agent_discovered_once(
    client, db_session, test_user, webhook_endpoint
):
    salt = client.get(SALT).json()["salt"]
    fingerprint = _hmac(salt, "machine-id-1")
    response = client.post(
        REPORTS, json=_report(fingerprint, _candidate(agent_version="1.2.3"))
    )
    assert response.status_code == 202
    assert response.json() == {"received": 1, "created": 1, "updated": 0}

    rows = crud_discovered_agent_candidate.list_for_account(
        db_session, account_id=test_user.account_id
    ).items
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "new"
    assert row.workstation_fingerprint == fingerprint

    deliveries = _discovered_deliveries(db_session, test_user.account_id)
    assert len(deliveries) == 1
    assert deliveries[0].event_id == deterministic_event_id(
        f"{EVENT_AGENT_DISCOVERED}:{row.id}"
    )
    data = deliveries[0].payload["data"]
    assert data["candidate_id"] == str(row.id)
    assert data["agent_kind"] == "cursor"
    assert data["agent_version"] == "1.2.3"
    assert set(data) == {
        "candidate_id",
        "agent_kind",
        "agent_version",
        "workstation_fingerprint",
        "config_path_hash",
        "mcp_server_count",
        "enrolled",
        "os_family",
        "status",
        "first_seen_at",
    }


def test_rereport_updates_last_seen_only_and_emits_nothing(
    client, db_session, test_user, webhook_endpoint
):
    fingerprint = "b" * 64
    assert client.post(REPORTS, json=_report(fingerprint, _candidate())).status_code
    row = crud_discovered_agent_candidate.list_for_account(
        db_session, account_id=test_user.account_id
    ).items[0]
    # Age the row so the bump is visible.
    earlier = row.last_seen_at - timedelta(days=3)
    row.last_seen_at = earlier
    row.first_seen_at = earlier
    db_session.commit()

    again = client.post(
        REPORTS,
        json=_report(
            fingerprint, _candidate(mcp_server_count=9, agent_version="9.9.9")
        ),
    )
    assert again.json() == {"received": 1, "created": 0, "updated": 1}

    rows = crud_discovered_agent_candidate.list_for_account(
        db_session, account_id=test_user.account_id
    ).items
    assert len(rows) == 1
    db_session.refresh(rows[0])
    assert rows[0].last_seen_at > earlier
    assert rows[0].first_seen_at == earlier
    assert rows[0].mcp_server_count == 2
    assert rows[0].agent_version is None
    assert len(_discovered_deliveries(db_session, test_user.account_id)) == 1


def test_distinct_key_parts_make_distinct_candidates(client, db_session, test_user):
    response = client.post(
        REPORTS,
        json=_report(
            "c" * 64,
            _candidate("cursor", "1" * 64),
            _candidate("cursor", "2" * 64),
            _candidate("claude_code", "1" * 64),
            _candidate("cursor", "1" * 64),
        ),
    )
    assert response.json() == {"received": 3, "created": 3, "updated": 0}
    client.post(REPORTS, json=_report("d" * 64, _candidate("cursor", "1" * 64)))
    assert (
        len(
            crud_discovered_agent_candidate.list_for_account(
                db_session, account_id=test_user.account_id
            ).items
        )
        == 4
    )


@pytest.mark.parametrize(
    "payload",
    [
        # Clear values where only hashes belong.
        _report("my-laptop.example.com", _candidate()),
        _report("e" * 64, _candidate(path_hash="/Users/jane/.cursor/mcp.json")),
        # Fields the privacy rules forbid have nowhere to go.
        {**_report("e" * 64, _candidate()), "hostname": "laptop"},
        {**_report("e" * 64, _candidate()), "username": "jane"},
        _report("e" * 64, _candidate(mcp_servers=["https://example.com/mcp"])),
        _report("e" * 64, _candidate(env={"API_KEY": "secret"})),
        _report("e" * 64, _candidate(agent_version="1.0 /home/jane")),
        {**_report("e" * 64, _candidate()), "os": "Darwin 23.1 jane-mbp"},
    ],
)
def test_report_rejects_anything_but_hashes_and_enums(client, payload):
    assert client.post(REPORTS, json=payload).status_code == 422


def test_purge_removes_candidates_unseen_for_90_days(db_session, test_user):
    now = utc_now_naive()
    crud_discovered_agent_candidate.record_report(
        db_session,
        account_id=test_user.account_id,
        workstation_fingerprint="f" * 64,
        os_family="linux",
        cli_version="0.18.0",
        candidates=[
            ReportedCandidate(agent_kind="stale", config_path_hash="1" * 64),
            ReportedCandidate(agent_kind="edge", config_path_hash="2" * 64),
            ReportedCandidate(agent_kind="fresh", config_path_hash="3" * 64),
        ],
        now=now,
    )
    db_session.commit()
    ages = {"stale": 91, "edge": 89, "fresh": 0}
    for row in crud_discovered_agent_candidate.list_for_account(
        db_session, account_id=test_user.account_id
    ).items:
        row.last_seen_at = now - timedelta(days=ages[row.agent_kind])
    db_session.commit()

    deleted = crud_discovered_agent_candidate.purge_stale(
        db_session, now=now, account_id=test_user.account_id
    )
    assert deleted == 1
    kinds = {
        row.agent_kind
        for row in crud_discovered_agent_candidate.list_for_account(
            db_session, account_id=test_user.account_id
        ).items
    }
    assert kinds == {"edge", "fresh"}


def test_purge_sweeper_pass_uses_the_crud_purge(monkeypatch):
    from preloop.services import discovery_candidate_purge as purge

    calls = []

    class _Db:
        def commit(self):
            calls.append("committed")

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(purge, "get_db_session", lambda: iter([_Db()]))
    monkeypatch.setattr(
        purge.crud_discovered_agent_candidate,
        "purge_stale",
        lambda db: calls.append("purged") or 3,
    )
    monkeypatch.setattr(
        purge.crud_discovery_observation,
        "purge_all_expired",
        lambda db: calls.append("observations purged") or 2,
    )
    assert purge.run_discovery_candidate_purge_once() == 5
    assert calls == ["purged", "observations purged", "committed", "closed"]


@pytest.fixture
def as_viewer(app, db_session, test_viewer_user):
    """Make the client act as a viewer, who lacks report_discovery."""
    user_id = test_viewer_user.id
    app.dependency_overrides[get_current_active_user] = lambda: crud_user.get(
        db_session, id=user_id
    )
    return test_viewer_user


def test_report_denied_without_report_discovery(client, as_viewer):
    assert client.post(REPORTS, json=_report("a" * 64, _candidate())).status_code == 403
    assert client.get(SALT).status_code == 403


def test_report_denied_for_api_key_without_the_scope(
    app, client, db_session, test_user
):
    user_id = test_user.id

    def _with_key():
        user = crud_user.get(db_session, id=user_id)
        user._auth_api_key = SimpleNamespace(id="k", scopes=["mcp:read"])
        return user

    app.dependency_overrides[get_current_active_user] = _with_key
    assert client.post(REPORTS, json=_report("a" * 64, _candidate())).status_code == 403


def test_report_allowed_for_device_scoped_key(app, client, db_session, test_user):
    user_id = test_user.id

    def _with_key():
        user = crud_user.get(db_session, id=user_id)
        user._auth_api_key = SimpleNamespace(id="k", scopes=["report_discovery"])
        return user

    app.dependency_overrides[get_current_active_user] = _with_key
    assert client.get(SALT).status_code == 200
    assert client.post(REPORTS, json=_report("a" * 64, _candidate())).status_code == 202


def _request(path: str, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )


def test_device_scoped_key_reaches_only_the_reporting_routes():
    key = SimpleNamespace(id="k", name="device", scopes=["report_discovery"])
    assert is_device_scoped_api_key(key)
    enforce_api_key_route_scope(key, _request(SALT))
    enforce_api_key_route_scope(key, _request(REPORTS, "POST"))
    for path in ("/api/v1/agents", CANDIDATES, "/api/v1/api-keys"):
        with pytest.raises(HTTPException) as exc:
            enforce_api_key_route_scope(key, _request(path))
        assert exc.value.status_code == 403
    assert api_key_allowed_on_channel(key, "account-events") is False
    # A personal key or a mixed-scope key is not device scoped.
    assert not is_device_scoped_api_key(SimpleNamespace(scopes=[]))
    assert not is_device_scoped_api_key(
        SimpleNamespace(scopes=["report_discovery", "mcp:read"])
    )


def _bearer_conn(token: str) -> SimpleNamespace:
    return SimpleNamespace(
        headers={"authorization": f"Bearer {token}"},
        scope={"path": "/mcp"},
    )


@pytest.mark.asyncio
async def test_device_scoped_key_is_refused_on_mcp_and_the_model_gateway(
    db_session, test_user
):
    """A report_discovery key must not authenticate MCP or the model gateway."""
    from preloop.services.mcp_http import PreloopBearerAuthBackend
    from preloop.services.model_gateway_auth import authenticate_bearer_token

    _device_key, device_token = crud_api_key.create_runtime_key(
        db_session,
        name="workstation discovery",
        account_id=test_user.account_id,
        user_id=test_user.id,
        scopes=["report_discovery"],
    )
    _personal_key, personal_token = crud_api_key.create_runtime_key(
        db_session,
        name="personal console",
        account_id=test_user.account_id,
        user_id=test_user.id,
        scopes=[],
    )

    assert await authenticate_bearer_token(device_token, db_session) is None
    personal_gateway = await authenticate_bearer_token(personal_token, db_session)
    assert personal_gateway is not None

    backend = PreloopBearerAuthBackend()

    def _same_session():
        yield db_session

    with (
        patch("preloop.services.mcp_http.get_db", _same_session),
        patch.object(db_session, "close"),
    ):
        assert await backend.authenticate(_bearer_conn(device_token)) is None
        assert await backend.authenticate(_bearer_conn(personal_token)) is not None


def test_console_list_and_mark_ignored(client, db_session, test_user):
    client.post(REPORTS, json=_report("a" * 64, _candidate("cursor")))
    listed = client.get(CANDIDATES)
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["truncated"] is False
    items = body["items"]
    assert len(items) == 1
    assert items[0]["status"] == "new"
    candidate_id = items[0]["id"]

    ignored = client.patch(f"{CANDIDATES}/{candidate_id}", json={"status": "ignored"})
    assert ignored.status_code == 200
    assert ignored.json()["status"] == "ignored"
    fresh = client.get(CANDIDATES, params={"status": "new"}).json()
    assert fresh["items"] == []
    assert fresh["total"] == 0
    assert fresh["truncated"] is False

    # Onboarded is set by enrollment linking only.
    assert (
        client.patch(
            f"{CANDIDATES}/{candidate_id}", json={"status": "onboarded"}
        ).status_code
        == 422
    )


def test_console_list_returns_total_above_the_cap(client, db_session, test_user):
    """A fleet larger than the console cap is counted, not silently dropped."""
    now = utc_now_naive()
    total = CONSOLE_LIST_LIMIT + 1
    db_session.add_all(
        [
            DiscoveredAgentCandidate(
                account_id=test_user.account_id,
                workstation_fingerprint="a" * 64,
                agent_kind="cursor",
                config_path_hash=f"{index:064x}",
                mcp_server_count=0,
                reported_enrolled=False,
                status="new",
                first_seen_at=now,
                last_seen_at=now,
            )
            for index in range(total)
        ]
    )
    db_session.commit()

    listed = client.get(CANDIDATES)
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == total
    assert body["truncated"] is True
    assert len(body["items"]) == CONSOLE_LIST_LIMIT


def test_mark_ignored_is_scoped_to_the_account(client, db_session):
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    outcome = crud_discovered_agent_candidate.record_report(
        db_session,
        account_id=other.id,
        workstation_fingerprint="9" * 64,
        os_family=None,
        cli_version=None,
        candidates=[ReportedCandidate(agent_kind="cursor", config_path_hash="9" * 64)],
    )[0]
    db_session.commit()
    response = client.patch(
        f"{CANDIDATES}/{outcome.candidate.id}", json={"status": "ignored"}
    )
    assert response.status_code == 404


def test_validated_enrollment_links_candidate(client, db_session, test_user):
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "workspace-discovery-link",
            "runtime_principal_name": "Claude Code Workspace",
        },
    )
    assert token_response.status_code == 201
    agent = client.get("/api/v1/agents").json()["items"][0]
    agent_id = agent["id"]
    kind = agent["agent_kind"]

    fingerprint = "7" * 64
    client.post(
        REPORTS,
        json=_report(
            fingerprint,
            _candidate(kind, "1" * 64),
            _candidate(kind, "2" * 64),
        ),
    )
    enrollment_id = client.post(
        f"/api/v1/agents/{agent_id}/enrollments",
        json={"enrollment_type": "cli_managed_config", "status": "applied"},
    ).json()["id"]

    # A failed validation links nothing.
    failed = client.post(
        f"/api/v1/agents/{agent_id}/enrollments/{enrollment_id}/validate",
        json={
            "status": "validation_failed",
            "workstation_fingerprint": fingerprint,
            "config_path_hash": "1" * 64,
        },
    )
    assert failed.status_code == 200
    assert {c["status"] for c in client.get(CANDIDATES).json()["items"]} == {"new"}

    ok = client.post(
        f"/api/v1/agents/{agent_id}/enrollments/{enrollment_id}/validate",
        json={
            "status": "validated",
            "workstation_fingerprint": fingerprint,
            "config_path_hash": "1" * 64,
        },
    )
    assert ok.status_code == 200
    by_path = {c["config_path_hash"]: c for c in client.get(CANDIDATES).json()["items"]}
    assert by_path["1" * 64]["status"] == "onboarded"
    assert by_path["1" * 64]["managed_agent_id"] == agent_id
    assert by_path["2" * 64]["status"] == "new"


def test_validate_rejects_a_clear_fingerprint(client):
    response = client.post(
        "/api/v1/agents/00000000-0000-0000-0000-000000000000/enrollments/"
        "00000000-0000-0000-0000-000000000000/validate",
        json={"status": "validated", "workstation_fingerprint": "jane-laptop"},
    )
    assert response.status_code == 422


def test_model_table_name():
    assert DiscoveredAgentCandidate.__tablename__ == "discovered_agent_candidate"
