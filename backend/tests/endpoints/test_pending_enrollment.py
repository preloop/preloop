"""Pending (manual-mode) enrollments: create, resume, cancel, expire, guide."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Optional
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_account, crud_ai_model
from preloop.models.models.webhook_endpoint import (
    SOURCE_ACCOUNT,
    WebhookDelivery,
    WebhookEndpoint,
)
from preloop.services.event_webhooks.signing import generate_secret, secret_hint
from preloop.services.managed_agent_onboarding_cleanup import (
    run_pending_enrollment_expiry_once,
)
from preloop.utils.encryption import encrypt_value

PLACEHOLDER = "<AGENT_KEY>"
BASE_URL = "https://preloop.example.com"


@pytest.fixture(autouse=True)
def public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the public base URL and the default TTL for every test."""
    monkeypatch.setattr(settings, "preloop_url", BASE_URL + "/")
    monkeypatch.setattr(settings, "preloop_pending_enrollment_ttl_hours", 72)


@pytest.fixture
def endpoint(db_session: Session, test_user: models.User) -> WebhookEndpoint:
    """Subscribe the account to ``agent.onboarded`` so emissions are stored."""
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


def _make_agent(
    db: Session, account_id: Any, owner_id: Any = None, **overrides: Any
) -> models.ManagedAgent:
    now = datetime.now(UTC).replace(tzinfo=None)
    source_id = f"src-{uuid4().hex[:8]}"
    fields: dict[str, Any] = dict(
        id=uuid4(),
        account_id=account_id,
        owner_user_id=owner_id,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id=source_id,
        display_name="Claude Code (example)",
        enrolled_via="cli",
        managed_mcp_servers=["preloop"],
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
        tags={},
    )
    fields.update(overrides)
    agent = models.ManagedAgent(**fields)
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def _credential(client: TestClient, agent_id: Any, name: str = "onboard") -> str:
    response = client.post(
        f"/api/v1/agents/{agent_id}/credentials", json={"name": name}
    )
    assert response.status_code == 201, response.text
    return response.json()["credential"]["id"]


def _secret(db: Session, account_id: Any) -> models.SecretReference:
    secret = models.SecretReference(
        account_id=account_id,
        name=f"key-{uuid4()}",
        secret_kind="ai_model_credentials",
        backend_type="encrypted_db",
        status="active",
    )
    db.add(secret)
    db.flush()
    return secret


def _model(
    db: Session,
    account_id: Any,
    name: str,
    secret_id: Any = None,
    agent_id: Any = None,
) -> models.AIModel:
    obj_in: dict[str, Any] = dict(
        account_id=account_id,
        name=name,
        provider_name="anthropic",
        model_identifier=f"example/{name.lower().replace(' ', '-')}",
    )
    if agent_id is not None:
        # Onboarding tags the models it creates with the agent they are for.
        obj_in["meta_data"] = {"managed_agent_id": str(agent_id)}
    model = crud_ai_model.create(db, obj_in=obj_in)
    if secret_id is not None:
        model.credentials_secret_id = secret_id
        db.flush()
    return model


def _manual_apply() -> dict[str, Any]:
    return {
        "files": [
            {
                "path": "~/.claude/settings.json",
                "format": "json",
                "exists": True,
                "merge": {
                    "env": {
                        "ANTHROPIC_BASE_URL": f"{BASE_URL}/anthropic",
                        "ANTHROPIC_API_KEY": PLACEHOLDER,
                    }
                },
                "remove": ["env.ANTHROPIC_AUTH_TOKEN"],
                "host_source": None,
            }
        ],
        "env": {"ANTHROPIC_API_KEY": PLACEHOLDER},
        "settings_flag": "claude --settings /tmp/example/settings.json",
        "limitations": ["Example limitation."],
    }


def _resources(
    *,
    agent_created: bool = False,
    agent_revived: bool = False,
    credential_ids: Optional[list[str]] = None,
    ai_model_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    return {
        "agent_created": agent_created,
        "agent_revived": agent_revived,
        "credential_ids": credential_ids or [],
        "ai_model_ids": ai_model_ids or [],
        "mcp_server_ids": [str(uuid4())],
    }


def _pending(
    client: TestClient,
    agent_id: Any,
    *,
    resources: Optional[dict[str, Any]] = None,
    manual_apply: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/agents/{agent_id}/enrollments",
        json={
            "enrollment_type": "cli_managed_config",
            "adapter_key": "claude_code",
            "status": "pending_local_config",
            "target_config_path": "~/.claude/settings.json",
            "managed_config": {
                "mcpServers": {"preloop": {"url": f"{BASE_URL}/mcp/v1"}},
                "manual_apply": manual_apply or _manual_apply(),
            },
            "backup_metadata": {"created_resources": resources or _resources()},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _onboarded_events(db: Session, user: models.User) -> list[dict[str, Any]]:
    rows = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.account_id == user.account_id)
        .all()
    )
    return [row.payload for row in rows if row.payload["type"] == "agent.onboarded"]


def _onboarding_state(client: TestClient, agent_id: Any) -> str:
    response = client.get(f"/api/v1/agents/{agent_id}")
    assert response.status_code == 200, response.text
    return response.json()["agent"]["onboarding_state"]


def _get(db: Session, model: Any, row_id: Any) -> Any:
    db.expire_all()
    return db.get(model, row_id)


def _age(db: Session, enrollment_id: str, hours: int) -> None:
    row = db.get(models.ManagedAgentEnrollment, enrollment_id)
    row.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours)
    db.add(row)
    db.commit()


# --- create -----------------------------------------------------------------


def test_pending_create_is_stored_and_does_not_count_as_onboarded(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    endpoint: WebhookEndpoint,
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)

    body = _pending(client, agent.id)

    assert body["status"] == "pending_local_config"
    assert body["managed_config"]["manual_apply"] == _manual_apply()
    assert body["backup_metadata"]["created_resources"]["agent_created"] is False
    created = datetime.fromisoformat(body["created_at"])
    assert datetime.fromisoformat(body["expires_at"]) == created + timedelta(hours=72)
    assert body["last_validated_at"] is None
    assert _onboarded_events(db_session, test_user) == []


def test_non_pending_enrollment_has_no_expiry(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    response = client.post(
        f"/api/v1/agents/{agent.id}/enrollments",
        json={"enrollment_type": "cli_managed_config", "status": "applied"},
    )
    assert response.status_code == 201
    assert response.json()["expires_at"] is None


def test_agent_reports_waiting_for_local_config_while_pending(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    assert _onboarding_state(client, agent.id) == "incomplete"

    _pending(client, agent.id)

    assert _onboarding_state(client, agent.id) == "waiting_for_local_config"
    listing = client.get("/api/v1/agents").json()["items"]
    row = next(item for item in listing if item["id"] == str(agent.id))
    assert row["onboarding_state"] == "waiting_for_local_config"
    assert row["mcp_proxy_configured"] is False
    assert row["model_gateway_configured"] is False


def test_real_looking_agent_key_is_redacted_in_manual_apply(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    leaked = "agt_" + "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6S7t8U9v0W1x"
    manual = _manual_apply()
    manual["env"]["ANTHROPIC_API_KEY"] = leaked
    manual["files"][0]["merge"]["env"]["ANTHROPIC_API_KEY"] = leaked
    manual["limitations"] = [f"Authorization: Bearer {leaked}"]

    body = _pending(client, agent.id, manual_apply=manual)

    stored = body["managed_config"]["manual_apply"]
    assert leaked not in str(stored)
    assert stored["env"]["ANTHROPIC_API_KEY"] == PLACEHOLDER
    assert stored["files"][0]["merge"]["env"]["ANTHROPIC_API_KEY"] == PLACEHOLDER
    assert stored["limitations"] == [f"Authorization: Bearer {PLACEHOLDER}"]
    row = db_session.get(models.ManagedAgentEnrollment, body["id"])
    assert leaked not in str(row.managed_config)


def test_pending_create_rejects_malformed_created_resources(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    response = client.post(
        f"/api/v1/agents/{agent.id}/enrollments",
        json={
            "enrollment_type": "cli_managed_config",
            "status": "pending_local_config",
            "backup_metadata": {"created_resources": {"credential_ids": "nope"}},
        },
    )
    assert response.status_code == 422


# --- validate (resume) ------------------------------------------------------


def test_validate_moves_pending_to_validated_and_emits_once(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    endpoint: WebhookEndpoint,
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    enrollment = _pending(client, agent.id)
    url = f"/api/v1/agents/{agent.id}/enrollments/{enrollment['id']}/validate"
    result = {"mcp_proxy_configured": True, "gateway_base_url_ok": True}

    response = client.post(
        url, json={"status": "validated", "validation_result": result}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "validated"
    assert body["last_applied_at"] is not None
    assert body["last_validated_at"] is not None
    assert body["expires_at"] is None
    assert len(_onboarded_events(db_session, test_user)) == 1
    assert _onboarding_state(client, agent.id) != "waiting_for_local_config"

    again = client.post(url, json={"status": "validated", "validation_result": result})
    assert again.status_code == 200
    assert len(_onboarded_events(db_session, test_user)) == 1


def test_failed_resume_keeps_the_enrollment_pending(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    endpoint: WebhookEndpoint,
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    enrollment = _pending(client, agent.id)

    response = client.post(
        f"/api/v1/agents/{agent.id}/enrollments/{enrollment['id']}/validate",
        json={"status": "validation_failed", "validation_result": {"error": "x"}},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending_local_config"
    assert body["validation_result"]["error"] == "x"
    assert body["expires_at"] is not None
    assert _onboarded_events(db_session, test_user) == []


def test_applied_resume_leaves_the_pending_state_and_is_never_swept(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    endpoint: WebhookEndpoint,
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    created = _credential(client, agent.id)
    enrollment = _pending(
        client,
        agent.id,
        resources=_resources(agent_created=True, credential_ids=[created]),
    )

    response = client.post(
        f"/api/v1/agents/{agent.id}/enrollments/{enrollment['id']}/validate",
        json={"status": "applied", "validation_result": {"user_confirmed": True}},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied"
    assert body["last_applied_at"] is not None
    assert body["validation_result"]["user_confirmed"] is True
    assert body["expires_at"] is None
    assert _onboarded_events(db_session, test_user) == []
    assert _onboarding_state(client, agent.id) != "waiting_for_local_config"

    _age(db_session, enrollment["id"], hours=500)
    assert run_pending_enrollment_expiry_once(db_session) == 0
    row = _get(db_session, models.ManagedAgentEnrollment, enrollment["id"])
    assert row.status == "applied"
    assert _get(db_session, models.ManagedAgentCredential, created).status == "active"
    assert _get(db_session, models.ManagedAgent, agent.id).lifecycle_state == "active"


def test_validate_is_refused_after_cancel(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    endpoint: WebhookEndpoint,
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    keep = _credential(client, agent.id, "existing")
    enrollment = _pending(client, agent.id)
    base = f"/api/v1/agents/{agent.id}/enrollments/{enrollment['id']}"
    assert client.post(f"{base}/cancel").status_code == 200

    response = client.post(f"{base}/validate", json={"status": "validated"})

    assert response.status_code == 409
    assert "cancelled" in response.json()["detail"]
    assert "run onboarding again" in response.json()["detail"]
    assert _onboarded_events(db_session, test_user) == []
    assert _get(db_session, models.ManagedAgentCredential, keep).status == "active"


def test_validate_is_refused_after_expiry(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    endpoint: WebhookEndpoint,
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    _credential(client, agent.id, "existing")
    enrollment = _pending(client, agent.id)
    _age(db_session, enrollment["id"], hours=73)
    assert run_pending_enrollment_expiry_once(db_session) == 1

    response = client.post(
        f"/api/v1/agents/{agent.id}/enrollments/{enrollment['id']}/validate",
        json={"status": "validated"},
    )

    assert response.status_code == 409
    assert "expired" in response.json()["detail"]
    assert _onboarded_events(db_session, test_user) == []


# --- cancel -----------------------------------------------------------------


def test_cancel_removes_credential_model_secret_and_created_agent(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    agent_id = agent.id
    credential_id = _credential(client, agent_id)
    credential = db_session.get(models.ManagedAgentCredential, credential_id)
    api_key_id = credential.api_key_id
    secret = _secret(db_session, test_user.account_id)
    secret_id = secret.id
    model = _model(
        db_session, test_user.account_id, "Onboard model", secret_id, agent_id
    )
    model_id = model.id
    db_session.commit()
    enrollment = _pending(
        client,
        agent_id,
        resources=_resources(
            agent_created=True,
            credential_ids=[credential_id],
            ai_model_ids=[str(model_id)],
        ),
    )

    response = client.post(
        f"/api/v1/agents/{agent_id}/enrollments/{enrollment['id']}/cancel"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["removed"] == {"credentials": 1, "ai_models": 1, "agent": "deleted"}
    assert body["enrollment"]["id"] == enrollment["id"]
    assert body["enrollment"]["status"] == "cancelled"
    assert body["enrollment"]["expires_at"] is None
    assert _get(db_session, models.ManagedAgent, agent_id) is None
    assert _get(db_session, models.AIModel, model_id) is None
    assert _get(db_session, models.SecretReference, secret_id) is None
    assert _get(db_session, models.ApiKey, api_key_id).is_active is False
    audit = (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.account_id == test_user.account_id,
            models.AuditLog.action == "managed_agent_enrollment_cancelled",
        )
        .one()
    )
    assert audit.details["removed"]["agent"] == "deleted"
    assert client.get(f"/api/v1/agents/{agent_id}").status_code == 404


def test_cancel_keeps_a_shared_secret_and_an_existing_agent_with_another_key(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    earlier = _credential(client, agent.id, "earlier")
    created = _credential(client, agent.id, "onboard")
    secret = _secret(db_session, test_user.account_id)
    secret_id = secret.id
    reused = _model(db_session, test_user.account_id, "Reused model", secret_id)
    fresh = _model(db_session, test_user.account_id, "Fresh model", secret_id, agent.id)
    reused_id, fresh_id = reused.id, fresh.id
    db_session.commit()
    enrollment = _pending(
        client,
        agent.id,
        resources=_resources(credential_ids=[created], ai_model_ids=[str(fresh_id)]),
    )

    response = client.post(
        f"/api/v1/agents/{agent.id}/enrollments/{enrollment['id']}/cancel"
    )

    assert response.status_code == 200, response.text
    assert response.json()["removed"] == {
        "credentials": 1,
        "ai_models": 1,
        "agent": "kept",
    }
    assert _get(db_session, models.ManagedAgentCredential, created).status == "revoked"
    assert _get(db_session, models.ManagedAgentCredential, earlier).status == "active"
    assert _get(db_session, models.ManagedAgent, agent.id).lifecycle_state == "active"
    assert _get(db_session, models.AIModel, fresh_id) is None
    assert _get(db_session, models.AIModel, reused_id) is not None
    assert _get(db_session, models.SecretReference, secret_id) is not None
    assert _onboarding_state(client, agent.id) == "incomplete"


def test_cancel_decommissions_an_existing_agent_left_without_a_key(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    created = _credential(client, agent.id)
    enrollment = _pending(
        client, agent.id, resources=_resources(credential_ids=[created])
    )

    response = client.post(
        f"/api/v1/agents/{agent.id}/enrollments/{enrollment['id']}/cancel"
    )

    assert response.status_code == 200, response.text
    assert response.json()["removed"]["agent"] == "decommissioned"
    row = _get(db_session, models.ManagedAgent, agent.id)
    assert row.lifecycle_state == "decommissioned"


def test_cancel_is_refused_unless_pending(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    _credential(client, agent.id, "existing")
    applied = client.post(
        f"/api/v1/agents/{agent.id}/enrollments",
        json={"enrollment_type": "cli_managed_config", "status": "applied"},
    ).json()
    base = f"/api/v1/agents/{agent.id}/enrollments"

    assert client.post(f"{base}/{applied['id']}/cancel").status_code == 409
    assert client.post(f"{base}/{uuid4()}/cancel").status_code == 404

    pending = _pending(client, agent.id)
    assert client.post(f"{base}/{pending['id']}/cancel").status_code == 200
    assert client.post(f"{base}/{pending['id']}/cancel").status_code == 409


# --- rollback ---------------------------------------------------------------


def test_rollback_cleans_up_without_an_enrollment(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    agent_id = agent.id
    credential_id = _credential(client, agent_id)
    model = _model(
        db_session, test_user.account_id, "Rollback model", agent_id=agent_id
    )
    model_id = model.id
    db_session.commit()

    response = client.post(
        f"/api/v1/agents/{agent_id}/onboarding-rollback",
        json={
            "created_resources": _resources(
                agent_created=True,
                credential_ids=[credential_id],
                ai_model_ids=[str(model_id)],
            ),
            "reason": "config write failed",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "removed": {"credentials": 1, "ai_models": 1, "agent": "deleted"}
    }
    assert _get(db_session, models.ManagedAgent, agent_id) is None
    assert _get(db_session, models.AIModel, model_id) is None
    audit = (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.account_id == test_user.account_id,
            models.AuditLog.action == "managed_agent_onboarding_rolled_back",
        )
        .one()
    )
    assert audit.details["reason"] == "config write failed"


def test_rollback_keeps_an_onboarded_agent_and_models_bound_elsewhere(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    other = _make_agent(db_session, test_user.account_id, test_user.id)
    created = _credential(client, agent.id)
    model = _model(db_session, test_user.account_id, "Shared model")
    model_id = model.id
    db_session.add(
        models.ManagedAgentAIModelBinding(
            account_id=test_user.account_id,
            managed_agent_id=other.id,
            ai_model_id=model_id,
            gateway_alias="example/shared-model",
            config_key="model",
            first_seen_at=datetime.now(UTC).replace(tzinfo=None),
            last_seen_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db_session.commit()
    validated = client.post(
        f"/api/v1/agents/{agent.id}/enrollments",
        json={"enrollment_type": "cli_managed_config", "status": "validated"},
    )
    assert validated.status_code == 201

    response = client.post(
        f"/api/v1/agents/{agent.id}/onboarding-rollback",
        json={
            "created_resources": _resources(
                agent_created=True,
                credential_ids=[created],
                ai_model_ids=[str(model_id), "not-a-uuid"],
            )
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "removed": {"credentials": 1, "ai_models": 0, "agent": "kept"}
    }
    assert _get(db_session, models.AIModel, model_id) is not None
    assert _get(db_session, models.ManagedAgent, agent.id).lifecycle_state == "active"


def test_rollback_ignores_another_accounts_resources(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    _credential(client, agent.id, "existing")
    sibling = _make_agent(db_session, test_user.account_id, test_user.id)
    sibling_credential = _credential(client, sibling.id, "sibling")
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Other Organization"}
    )
    foreign_agent = _make_agent(db_session, foreign.id)
    foreign_agent_id = foreign_agent.id
    foreign_secret = _secret(db_session, foreign.id)
    foreign_secret_id = foreign_secret.id
    foreign_model = _model(db_session, foreign.id, "Foreign model", foreign_secret_id)
    foreign_model_id = foreign_model.id
    db_session.commit()
    resources = _resources(
        agent_created=True,
        credential_ids=[sibling_credential],
        ai_model_ids=[str(foreign_model_id)],
    )

    response = client.post(
        f"/api/v1/agents/{agent.id}/onboarding-rollback",
        json={"created_resources": resources},
    )
    foreign_response = client.post(
        f"/api/v1/agents/{foreign_agent_id}/onboarding-rollback",
        json={"created_resources": resources},
    )

    assert response.status_code == 200, response.text
    assert response.json()["removed"]["credentials"] == 0
    assert response.json()["removed"]["ai_models"] == 0
    assert foreign_response.status_code == 404
    assert _get(db_session, models.AIModel, foreign_model_id) is not None
    assert _get(db_session, models.SecretReference, foreign_secret_id) is not None
    assert _get(db_session, models.ManagedAgent, foreign_agent_id) is not None
    assert (
        _get(db_session, models.ManagedAgentCredential, sibling_credential).status
        == "active"
    )


@pytest.mark.parametrize("via", ["rollback", "cancel"])
def test_a_revived_agent_is_decommissioned_again(
    client: TestClient, db_session: Session, test_user: models.User, via: str
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    old_run = client.post(
        f"/api/v1/agents/{agent.id}/enrollments",
        json={"enrollment_type": "cli_managed_config", "status": "restored"},
    )
    assert old_run.status_code == 201
    surviving = _credential(client, agent.id, "survivor")
    created = _credential(client, agent.id, "onboard")
    resources = _resources(agent_revived=True, credential_ids=[created])

    if via == "rollback":
        response = client.post(
            f"/api/v1/agents/{agent.id}/onboarding-rollback",
            json={"created_resources": resources},
        )
    else:
        enrollment = _pending(client, agent.id, resources=resources)
        assert enrollment["backup_metadata"]["created_resources"]["agent_revived"]
        response = client.post(
            f"/api/v1/agents/{agent.id}/enrollments/{enrollment['id']}/cancel"
        )

    assert response.status_code == 200, response.text
    assert response.json()["removed"] == {
        "credentials": 1,
        "ai_models": 0,
        "agent": "decommissioned",
    }
    row = _get(db_session, models.ManagedAgent, agent.id)
    assert row.lifecycle_state == "decommissioned"
    credential = _get(db_session, models.ManagedAgentCredential, surviving)
    assert credential.status == "revoked"
    assert _get(db_session, models.ApiKey, credential.api_key_id).is_active is False


def test_an_untouched_agent_without_a_key_is_kept(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)

    response = client.post(
        f"/api/v1/agents/{agent.id}/onboarding-rollback",
        json={"created_resources": _resources()},
    )

    assert response.status_code == 200, response.text
    assert response.json()["removed"]["agent"] == "kept"
    assert _get(db_session, models.ManagedAgent, agent.id).lifecycle_state == "active"


# --- expiry job -------------------------------------------------------------


def test_expiry_job_cleans_only_stale_pending_enrollments(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    stale_agent = _make_agent(db_session, test_user.account_id, test_user.id)
    stale_agent_id = stale_agent.id
    stale_credential = _credential(client, stale_agent_id)
    model = _model(
        db_session, test_user.account_id, "Stale model", agent_id=stale_agent_id
    )
    model_id = model.id
    db_session.commit()
    stale = _pending(
        client,
        stale_agent_id,
        resources=_resources(
            agent_created=True,
            credential_ids=[stale_credential],
            ai_model_ids=[str(model_id)],
        ),
    )
    fresh_agent = _make_agent(db_session, test_user.account_id, test_user.id)
    fresh_credential = _credential(client, fresh_agent.id)
    fresh = _pending(
        client,
        fresh_agent.id,
        resources=_resources(agent_created=True, credential_ids=[fresh_credential]),
    )
    applied_agent = _make_agent(db_session, test_user.account_id, test_user.id)
    applied = client.post(
        f"/api/v1/agents/{applied_agent.id}/enrollments",
        json={"enrollment_type": "cli_managed_config", "status": "applied"},
    ).json()
    _age(db_session, stale["id"], hours=73)
    _age(db_session, fresh["id"], hours=71)
    _age(db_session, applied["id"], hours=500)

    assert run_pending_enrollment_expiry_once(db_session) == 1
    assert run_pending_enrollment_expiry_once(db_session) == 0

    assert _get(db_session, models.ManagedAgent, stale_agent_id) is None
    assert _get(db_session, models.AIModel, model_id) is None
    row = _get(db_session, models.ManagedAgentEnrollment, fresh["id"])
    assert row.status == "pending_local_config"
    credential = _get(db_session, models.ManagedAgentCredential, fresh_credential)
    assert credential.status == "active"
    assert _get(db_session, models.ManagedAgentEnrollment, applied["id"]).status == (
        "applied"
    )
    audit = (
        db_session.query(models.AuditLog)
        .filter(
            models.AuditLog.account_id == test_user.account_id,
            models.AuditLog.action == "managed_agent_enrollment_expired",
        )
        .one()
    )
    assert audit.user_id is None
    assert audit.details["enrollment_id"] == stale["id"]


def test_expiry_job_marks_a_kept_agents_enrollment_expired(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    _credential(client, agent.id, "existing")
    enrollment = _pending(client, agent.id)
    later = datetime.now(UTC) + timedelta(hours=73)

    assert run_pending_enrollment_expiry_once(db_session, now=later) == 1

    row = _get(db_session, models.ManagedAgentEnrollment, enrollment["id"])
    assert row.status == "expired"
    assert _onboarding_state(client, agent.id) == "incomplete"


def test_expiry_job_publishes_the_decommissioned_agent(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services import managed_agent_onboarding_cleanup as cleanup

    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    created = _credential(client, agent.id)
    enrollment = _pending(
        client, agent.id, resources=_resources(credential_ids=[created])
    )
    _age(db_session, enrollment["id"], hours=73)
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(cleanup, "emit_account_event", events.append)

    assert run_pending_enrollment_expiry_once(db_session) == 1

    published = [
        event for event in events if event.get("type") == "managed_agent_decommissioned"
    ]
    assert len(published) == 1
    assert published[0]["topic"] == "managed_agents"
    assert published[0]["payload"]["id"] == str(agent.id)
    assert published[0]["payload"]["lifecycle_state"] == "decommissioned"


def test_expiry_job_honours_the_configured_ttl(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    _credential(client, agent.id, "existing")
    enrollment = _pending(client, agent.id)
    _age(db_session, enrollment["id"], hours=3)
    assert run_pending_enrollment_expiry_once(db_session) == 0

    monkeypatch.setattr(settings, "preloop_pending_enrollment_ttl_hours", 2)

    assert run_pending_enrollment_expiry_once(db_session) == 1


# --- manual setup guide -----------------------------------------------------


def test_manual_setup_returns_the_pending_instructions(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    enrollment = _pending(client, agent.id)

    response = client.get(f"/api/v1/agents/{agent.id}/manual-setup")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == str(agent.id)
    assert body["agent_kind"] == "claude_code"
    assert body["display_name"] == "Claude Code (example)"
    assert body["pending"] == {
        "enrollment_id": enrollment["id"],
        "status": "pending_local_config",
        "created_at": enrollment["created_at"],
        "expires_at": enrollment["expires_at"],
    }
    assert body["instructions"] == _manual_apply()
    assert body["resume_command"] == (
        'preloop agents onboard --resume "Claude Code (example)"'
    )


def test_manual_setup_generates_a_claude_code_baseline(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)

    body = client.get(f"/api/v1/agents/{agent.id}/manual-setup").json()

    assert body["pending"] is None
    env = {
        "ANTHROPIC_BASE_URL": f"{BASE_URL}/anthropic",
        "ANTHROPIC_API_KEY": PLACEHOLDER,
    }
    instructions = body["instructions"]
    assert instructions["env"] == env
    assert instructions["settings_flag"] == "claude --settings <file>"
    assert len(instructions["limitations"]) == 2
    settings_file, mcp_file = instructions["files"]
    assert settings_file == {
        "path": "~/.claude/settings.json",
        "format": "json",
        # A baseline must say "merge", never "create with exactly this".
        "exists": True,
        "merge": {"env": env},
        "remove": [],
        "host_source": None,
    }
    assert mcp_file["path"] == "~/.claude.json"
    assert mcp_file["merge"] == {
        "mcpServers": {
            "preloop": {
                "type": "http",
                "url": f"{BASE_URL}/mcp/v1",
                "headers": {"Authorization": f"Bearer {PLACEHOLDER}"},
            }
        }
    }


@pytest.mark.parametrize(
    ("kind", "path", "file_format", "merge"),
    [
        (
            "codex",
            "~/.codex/config.toml",
            "toml",
            {
                "mcp_servers": {
                    "preloop": {
                        "url": f"{BASE_URL}/mcp/v1",
                        "http_headers": {"Authorization": f"Bearer {PLACEHOLDER}"},
                    }
                }
            },
        ),
        (
            "cursor",
            "~/.cursor/mcp.json",
            "json",
            {
                "mcpServers": {
                    "preloop": {
                        "url": f"{BASE_URL}/mcp/v1",
                        "headers": {"Authorization": f"Bearer {PLACEHOLDER}"},
                    }
                }
            },
        ),
    ],
)
def test_manual_setup_generates_other_baselines(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    kind: str,
    path: str,
    file_format: str,
    merge: dict[str, Any],
) -> None:
    source_type = "codex" if kind == "codex" else "desktop_agent"
    agent = _make_agent(
        db_session,
        test_user.account_id,
        test_user.id,
        agent_kind=kind,
        session_source_type=source_type,
    )

    body = client.get(f"/api/v1/agents/{agent.id}/manual-setup").json()

    assert body["agent_kind"] == kind
    instructions = body["instructions"]
    assert [item["path"] for item in instructions["files"]] == [path]
    assert instructions["files"][0]["format"] == file_format
    assert instructions["files"][0]["merge"] == merge
    assert instructions["env"] == {}
    assert instructions["settings_flag"] is None


def test_manual_setup_has_no_instructions_for_other_kinds(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(
        db_session,
        test_user.account_id,
        test_user.id,
        agent_kind="openclaw",
        session_source_type="openclaw",
    )

    response = client.get(f"/api/v1/agents/{agent.id}/manual-setup")

    assert response.status_code == 200
    assert response.json()["instructions"] is None
    assert response.json()["pending"] is None
    assert client.get(f"/api/v1/agents/{uuid4()}/manual-setup").status_code == 404


def test_manual_setup_falls_back_to_the_baseline_after_cancel(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    _credential(client, agent.id, "existing")
    enrollment = _pending(client, agent.id)
    client.post(f"/api/v1/agents/{agent.id}/enrollments/{enrollment['id']}/cancel")

    body = client.get(f"/api/v1/agents/{agent.id}/manual-setup").json()

    assert body["pending"] is None
    assert body["instructions"]["settings_flag"] == "claude --settings <file>"


# --- secrets in enrollment JSON ---------------------------------------------

PROVIDER_KEY = "sk-ant-" + "api03-Example0123456789abcdefghij"
AGENT_KEY = "agt_" + "M1n2B3v4C5x6Z7l8K9j0H1g2F3d4S5a6P7o8I9u0Y1t"
SESSION_TOKEN = "example-session-token-value"


def _secret_configs() -> dict[str, Any]:
    return {
        "discovered_config": {
            "env": {
                "ANTHROPIC_API_KEY": PROVIDER_KEY,
                "AWS_SESSION_TOKEN": SESSION_TOKEN,
                "ANTHROPIC_MODEL": "example-model",
            },
            "experimental_bearer_token": SESSION_TOKEN,
        },
        "managed_config": {
            "env": {
                "ANTHROPIC_API_KEY": AGENT_KEY,
                "ANTHROPIC_BASE_URL": f"{BASE_URL}/anthropic",
            },
            "mcpServers": {
                "preloop": {"headers": {"Authorization": f"Bearer {AGENT_KEY}"}}
            },
            "manual_apply": _manual_apply(),
        },
        "backup_metadata": {"previous": {"CLAUDE_CODE_OAUTH_TOKEN": SESSION_TOKEN}},
        "validation_result": {"probe_header": f"Bearer {AGENT_KEY}"},
    }


def _assert_clean(value: Any) -> None:
    text = str(value)
    for secret in (PROVIDER_KEY, AGENT_KEY, SESSION_TOKEN):
        assert secret not in text
    assert "<redacted>" in text


def test_create_redacts_secrets_in_stored_and_served_configs(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)

    response = client.post(
        f"/api/v1/agents/{agent.id}/enrollments",
        json={
            "enrollment_type": "cli_managed_config",
            "status": "pending_local_config",
            **_secret_configs(),
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    _assert_clean(body)
    assert body["discovered_config"]["env"]["ANTHROPIC_MODEL"] == "example-model"
    assert body["managed_config"]["env"]["ANTHROPIC_BASE_URL"].endswith("/anthropic")
    assert body["managed_config"]["manual_apply"] == _manual_apply()
    row = _get(db_session, models.ManagedAgentEnrollment, body["id"])
    for column in (
        row.discovered_config,
        row.managed_config,
        row.backup_metadata,
        row.validation_result,
    ):
        _assert_clean(column)
    assert row.managed_config["manual_apply"] == _manual_apply()
    detail = client.get(f"/api/v1/agents/{agent.id}")
    assert detail.status_code == 200
    _assert_clean(detail.json())
    setup = client.get(f"/api/v1/agents/{agent.id}/manual-setup").json()
    assert setup["instructions"] == _manual_apply()


def test_validate_and_restore_redact_merged_client_json(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    created = client.post(
        f"/api/v1/agents/{agent.id}/enrollments",
        json={"enrollment_type": "cli_managed_config", "status": "applied"},
    ).json()
    base = f"/api/v1/agents/{agent.id}/enrollments/{created['id']}"

    validated = client.post(
        f"{base}/validate",
        json={
            "status": "validated",
            "validation_result": {"env": {"ANTHROPIC_API_KEY": PROVIDER_KEY}},
        },
    )
    restored = client.post(
        f"{base}/restore",
        json={
            "backup_metadata": {"env": {"AWS_SESSION_TOKEN": SESSION_TOKEN}},
            "validation_result": {"header": f"Bearer {AGENT_KEY}"},
        },
    )

    assert validated.status_code == 200, validated.text
    assert restored.status_code == 200, restored.text
    _assert_clean(validated.json())
    _assert_clean(restored.json())
    row = _get(db_session, models.ManagedAgentEnrollment, created["id"])
    _assert_clean(row.validation_result)
    _assert_clean(row.backup_metadata)


def test_a_row_stored_with_secrets_is_served_redacted(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    row = models.ManagedAgentEnrollment(
        account_id=test_user.account_id,
        managed_agent_id=agent.id,
        created_by_user_id=test_user.id,
        enrollment_type="cli_managed_config",
        status="pending_local_config",
        **_secret_configs(),
    )
    db_session.add(row)
    db_session.commit()
    assert PROVIDER_KEY in str(row.discovered_config)

    listing = client.get(f"/api/v1/agents/{agent.id}/enrollments")
    detail = client.get(f"/api/v1/agents/{agent.id}")
    setup = client.get(f"/api/v1/agents/{agent.id}/manual-setup")

    assert listing.status_code == 200 and detail.status_code == 200
    _assert_clean(listing.json())
    _assert_clean(detail.json())
    assert listing.json()[0]["managed_config"]["manual_apply"] == _manual_apply()
    assert setup.json()["instructions"] == _manual_apply()


def test_rollback_never_deletes_a_model_without_this_agents_onboarding_tag(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """The rollback path needs only manage_agents, so it must not double as a
    way to delete arbitrary account models."""
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    other = _make_agent(db_session, test_user.account_id, test_user.id)
    untagged = _model(db_session, test_user.account_id, "Untagged model")
    elsewhere = _model(
        db_session, test_user.account_id, "Other agent model", agent_id=other.id
    )
    agent_id, untagged_id, elsewhere_id = agent.id, untagged.id, elsewhere.id
    db_session.commit()

    response = client.post(
        f"/api/v1/agents/{agent_id}/onboarding-rollback",
        json={
            "created_resources": _resources(
                ai_model_ids=[str(untagged_id), str(elsewhere_id)]
            ),
            "reason": "example",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["removed"]["ai_models"] == 0
    db_session.expire_all()
    assert crud_ai_model.get(db_session, id=untagged_id) is not None
    assert crud_ai_model.get(db_session, id=elsewhere_id) is not None


def test_rollback_never_deletes_a_model_older_than_the_onboarding(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """The tag is editable through the model API. A model that existed before
    the onboarding must survive even when it carries this agent's tag."""
    agent = _make_agent(db_session, test_user.account_id, test_user.id)
    old = _model(
        db_session, test_user.account_id, "Long standing model", agent_id=agent.id
    )
    old.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3)
    agent_id, old_id = agent.id, old.id
    db_session.commit()

    response = client.post(
        f"/api/v1/agents/{agent_id}/onboarding-rollback",
        json={
            "created_resources": _resources(ai_model_ids=[str(old_id)]),
            "reason": "example",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["removed"]["ai_models"] == 0
    db_session.expire_all()
    assert crud_ai_model.get(db_session, id=old_id) is not None
