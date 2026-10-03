"""``agent.onboarded`` fires once per enrollment that reaches ``validated``."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.models.models.webhook_endpoint import (
    SOURCE_ACCOUNT,
    WebhookDelivery,
    WebhookEndpoint,
)
from preloop.services.event_webhooks.signing import generate_secret, secret_hint
from preloop.utils.encryption import encrypt_value

PRIVATE_PATH = "/Users/jane/.codex/config.toml"
PRIVATE_HOST = "jane-laptop.example.com"
PRIVATE_URL = "https://internal-mcp.example.com/mcp"


@pytest.fixture
def endpoint(db_session, test_user) -> WebhookEndpoint:
    secret = generate_secret()
    row = WebhookEndpoint(
        account_id=test_user.account_id,
        url="https://example.com/hook",
        secret_encrypted=encrypt_value(secret),
        secret_hint=secret_hint(secret),
        event_types=["agent.onboarded"],
        active=True,
        source=SOURCE_ACCOUNT,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _make_agent(db_session, test_user, *, source_id: str, **overrides):
    now = datetime.now(UTC).replace(tzinfo=None)
    fields = dict(
        id=uuid4(),
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        agent_kind="codex",
        session_source_type="codex",
        session_source_id=source_id,
        session_reference=PRIVATE_PATH,
        enrollment_hostname=PRIVATE_HOST,
        display_name=f"Codex {source_id}",
        enrolled_via="runtime_session_token",
        managed_mcp_servers=["preloop", "github"],
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
        tags={},
    )
    fields.update(overrides)
    agent = models.ManagedAgent(**fields)
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


def _enroll(client, agent_id) -> str:
    response = client.post(
        f"/api/v1/agents/{agent_id}/enrollments",
        json={
            "enrollment_type": "cli_managed_config",
            "adapter_key": "codex",
            "status": "applied",
            "target_config_path": PRIVATE_PATH,
            "discovered_config": {"servers": {"github": {"url": PRIVATE_URL}}},
            "managed_config": {
                "servers": {"preloop": {"url": "https://preloop.example.com/mcp/v1"}}
            },
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def _validate(client, agent_id, enrollment_id, status="validated", result=None):
    response = client.post(
        f"/api/v1/agents/{agent_id}/enrollments/{enrollment_id}/validate",
        json={
            "status": status,
            "validation_result": result
            or {
                "mcp_proxy_configured": True,
                "gateway_provider_ok": True,
                "gateway_base_url_ok": True,
            },
        },
    )
    assert response.status_code == 200
    return response.json()


def _events(db_session, test_user) -> list[dict]:
    rows = (
        db_session.query(WebhookDelivery)
        .filter(WebhookDelivery.account_id == test_user.account_id)
        .order_by(WebhookDelivery.created_at)
        .all()
    )
    return [row.payload for row in rows if row.payload["type"] == "agent.onboarded"]


def test_creating_an_enrollment_does_not_fire(client, db_session, test_user, endpoint):
    agent = _make_agent(db_session, test_user, source_id="codex-create-only")
    _enroll(client, agent.id)
    assert _events(db_session, test_user) == []


def test_fires_once_on_validate_with_created_outcome(
    client, db_session, test_user, endpoint
):
    agent = _make_agent(db_session, test_user, source_id="codex-first")
    enrollment_id = _enroll(client, agent.id)

    _validate(client, agent.id, enrollment_id)

    events = _events(db_session, test_user)
    assert len(events) == 1
    data = events[0]["data"]
    assert data == {
        "agent_id": str(agent.id),
        "agent_name": "Codex codex-first",
        "agent_kind": "codex",
        "source_type": "discovered",
        "outcome": "created",
        "enrollment_id": enrollment_id,
        "owner_user_id": str(test_user.id),
        "actor_user_id": str(test_user.id),
        "gateway_routed": True,
        "mcp_rewritten": True,
        "mcp_server_count": 2,
    }
    assert events[0]["version"] == "1"
    assert events[0]["occurred_at"]


def test_payload_never_carries_host_paths_or_urls(
    client, db_session, test_user, endpoint
):
    agent = _make_agent(db_session, test_user, source_id="codex-private")
    _validate(client, agent.id, _enroll(client, agent.id))

    body = str(_events(db_session, test_user)[0])
    for secret in (PRIVATE_PATH, PRIVATE_HOST, PRIVATE_URL, "jane", "/mcp"):
        assert secret not in body


def test_revalidate_does_not_fire_again(client, db_session, test_user, endpoint):
    agent = _make_agent(db_session, test_user, source_id="codex-revalidate")
    enrollment_id = _enroll(client, agent.id)

    _validate(client, agent.id, enrollment_id)
    _validate(client, agent.id, enrollment_id)
    # Even a round trip through a failed check keeps one event per enrollment.
    _validate(client, agent.id, enrollment_id, status="validation_failed")
    _validate(client, agent.id, enrollment_id)

    assert len(_events(db_session, test_user)) == 1


def test_failed_validation_does_not_fire(client, db_session, test_user, endpoint):
    agent = _make_agent(db_session, test_user, source_id="codex-failed")
    enrollment_id = _enroll(client, agent.id)

    _validate(client, agent.id, enrollment_id, status="validation_failed")
    _validate(client, agent.id, enrollment_id, status="validation_inconclusive")

    assert _events(db_session, test_user) == []


def test_relink_sets_outcome_relinked(client, db_session, test_user, endpoint):
    agent = _make_agent(db_session, test_user, source_id="codex-relink")
    first = _enroll(client, agent.id)
    _validate(client, agent.id, first)
    restore = client.post(
        f"/api/v1/agents/{agent.id}/enrollments/{first}/restore",
        json={"backup_metadata": {}, "validation_result": {}},
    )
    assert restore.status_code == 200

    second = _enroll(client, agent.id)
    _validate(client, agent.id, second)

    events = _events(db_session, test_user)
    assert [e["data"]["outcome"] for e in events] == ["created", "relinked"]
    assert events[1]["data"]["enrollment_id"] == second
    assert events[0]["id"] != events[1]["id"]


def test_merge_sets_outcome_merged(client, db_session, test_user, endpoint):
    survivor = _make_agent(db_session, test_user, source_id="codex-survivor")
    duplicate = _make_agent(db_session, test_user, source_id="codex-duplicate")
    _validate(client, survivor.id, _enroll(client, survivor.id))

    merge = client.post(
        f"/api/v1/agents/{survivor.id}/merge",
        json={"duplicate_agent_id": str(duplicate.id), "dry_run": False},
    )
    assert merge.status_code == 200, merge.text

    _validate(client, survivor.id, _enroll(client, survivor.id))
    # A later re-link with no new merge is a plain re-link again.
    _validate(client, survivor.id, _enroll(client, survivor.id))

    outcomes = [e["data"]["outcome"] for e in _events(db_session, test_user)]
    assert outcomes == ["created", "merged", "relinked"]


def test_first_onboarding_of_a_merge_survivor_is_merged(
    client, db_session, test_user, endpoint
):
    survivor = _make_agent(db_session, test_user, source_id="codex-survivor-new")
    duplicate = _make_agent(db_session, test_user, source_id="codex-dup-new")
    merge = client.post(
        f"/api/v1/agents/{survivor.id}/merge",
        json={"duplicate_agent_id": str(duplicate.id), "dry_run": False},
    )
    assert merge.status_code == 200, merge.text

    _validate(client, survivor.id, _enroll(client, survivor.id))

    assert _events(db_session, test_user)[0]["data"]["outcome"] == "merged"


def test_custom_agent_reports_custom_source(client, db_session, test_user, endpoint):
    agent = _make_agent(
        db_session,
        test_user,
        source_id="custom-agent",
        enrolled_via="operator_registration",
        managed_mcp_servers=[],
    )
    _validate(
        client,
        agent.id,
        _enroll(client, agent.id),
        result={"ok": True},
    )

    data = _events(db_session, test_user)[0]["data"]
    assert data["source_type"] == "custom"
    assert data["mcp_server_count"] == 0


def test_mcp_only_onboarding_is_not_gateway_routed(
    client, db_session, test_user, endpoint
):
    agent = _make_agent(db_session, test_user, source_id="codex-mcp-only")
    _validate(
        client,
        agent.id,
        _enroll(client, agent.id),
        result={"mcp_proxy_configured": True},
    )

    data = _events(db_session, test_user)[0]["data"]
    assert data["mcp_rewritten"] is True
    assert data["gateway_routed"] is False
