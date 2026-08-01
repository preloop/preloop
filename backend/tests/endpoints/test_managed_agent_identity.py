"""Tests for managed-agent rekey and merge endpoints (#112 part b)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from preloop.models import models
from preloop.models.crud import crud_managed_agent
from preloop.models.models.budget import BudgetPeriod, BudgetPolicy, BudgetSpendActivity
from preloop.services.usage_fingerprint import event_fingerprint


def _make_agent(
    db_session,
    test_user,
    *,
    source_id: str,
    source_type: str = "codex",
    lifecycle_state: str = "active",
    display_name: str | None = None,
    session_reference: str | None = "/tmp/codex/config.toml",
    tags: dict | None = None,
):
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        runtime_session_id=None,
        agent_kind=source_type,
        session_source_type=source_type,
        session_source_id=source_id,
        session_reference=session_reference,
        display_name=display_name or f"{source_type} {source_id}",
        enrolled_via="runtime_session_token",
        lifecycle_state=lifecycle_state,
        lifecycle_updated_at=now,
        last_seen_at=now,
        tags=tags or {},
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)
    return agent


def _add_usage(
    db_session,
    test_user,
    *,
    principal_id: str,
    action_type: str = "model_gateway",
    model_alias: str = "gpt-5",
    estimated_cost: float = 0.25,
    meta_data: dict | None = None,
):
    row = models.ApiUsage(
        id=uuid4(),
        account_id=test_user.account_id,
        endpoint="/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.01,
        action_type=action_type,
        model_alias=model_alias,
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        estimated_cost=estimated_cost,
        runtime_principal_type="codex",
        runtime_principal_id=principal_id,
        meta_data=meta_data or {},
        timestamp=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_rekey_moves_usage_and_appends_previous_ids(client, db_session, test_user):
    agent = _make_agent(db_session, test_user, source_id="codex-oldid000001")
    _add_usage(db_session, test_user, principal_id="codex-oldid000001")

    response = client.post(
        f"/api/v1/agents/{agent.id}/rekey",
        json={
            "new_session_source_id": "codex-newid000001",
            "principal_identity": {
                "hostname": "tuvok",
                "config_path": "/tmp/codex/config.toml",
                "source_type": "codex",
                "derivation": "v2",
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent"]["session_source_id"] == "codex-newid000001"
    assert body["agent"]["enrollment_hostname"] == "tuvok"
    assert body["counts"]["usage_moved"] == 1

    db_session.refresh(agent)
    assert agent.session_source_id == "codex-newid000001"
    assert "codex-oldid000001" in (agent.tags or {}).get("identity.previous_ids", "")

    remaining = (
        db_session.query(models.ApiUsage)
        .filter(models.ApiUsage.runtime_principal_id == "codex-newid000001")
        .count()
    )
    assert remaining == 1


def test_rekey_conflict_returns_409(client, db_session, test_user):
    _make_agent(db_session, test_user, source_id="codex-taken0000001")
    agent = _make_agent(db_session, test_user, source_id="codex-source000001")
    response = client.post(
        f"/api/v1/agents/{agent.id}/rekey",
        json={"new_session_source_id": "codex-taken0000001"},
    )
    assert response.status_code == 409


def test_rekey_idempotent_same_id(client, db_session, test_user):
    agent = _make_agent(db_session, test_user, source_id="codex-same00000001")
    response = client.post(
        f"/api/v1/agents/{agent.id}/rekey",
        json={"new_session_source_id": "codex-same00000001"},
    )
    assert response.status_code == 200
    assert response.json()["counts"]["usage_moved"] == 0


def test_merge_refusals(client, db_session, test_user):
    codex = _make_agent(db_session, test_user, source_id="codex-a")
    hermes = _make_agent(
        db_session, test_user, source_id="hermes-a", source_type="hermes"
    )
    custom = _make_agent(
        db_session, test_user, source_id="custom-a", source_type="custom"
    )
    already = _make_agent(
        db_session,
        test_user,
        source_id="codex-merged",
        tags={"merged_into": str(codex.id)},
    )

    cross = client.post(
        f"/api/v1/agents/{codex.id}/merge",
        json={"duplicate_agent_id": str(hermes.id), "dry_run": True},
    )
    assert cross.status_code == 409

    custom_resp = client.post(
        f"/api/v1/agents/{codex.id}/merge",
        json={"duplicate_agent_id": str(custom.id), "dry_run": True},
    )
    assert custom_resp.status_code == 409

    double = client.post(
        f"/api/v1/agents/{codex.id}/merge",
        json={"duplicate_agent_id": str(already.id), "dry_run": True},
    )
    assert double.status_code == 409

    unknown = client.post(
        f"/api/v1/agents/{codex.id}/merge",
        json={"duplicate_agent_id": str(uuid4()), "dry_run": True},
    )
    assert unknown.status_code == 404


def test_merge_dry_run_and_execute_with_budget_policy_keep_survivor(
    client, db_session, test_user
):
    survivor = _make_agent(db_session, test_user, source_id="codex-survivor01")
    duplicate = _make_agent(db_session, test_user, source_id="codex-duplicate01")
    _add_usage(db_session, test_user, principal_id="codex-duplicate01")

    # Conflicting budget policies: survivor kept, duplicate dropped.
    survivor_policy = BudgetPolicy(
        id=uuid4(),
        account_id=test_user.account_id,
        subject_type="managed_agent",
        subject_id=survivor.id,
        model_alias="gpt-5",
        period=BudgetPeriod.monthly,
        hard_limit_usd=10.0,
        soft_limit_usd=5.0,
    )
    duplicate_policy = BudgetPolicy(
        id=uuid4(),
        account_id=test_user.account_id,
        subject_type="managed_agent",
        subject_id=duplicate.id,
        model_alias="gpt-5",
        period=BudgetPeriod.monthly,
        hard_limit_usd=99.0,
        soft_limit_usd=50.0,
    )
    db_session.add_all([survivor_policy, duplicate_policy])
    spend = BudgetSpendActivity(
        id=uuid4(),
        account_id=test_user.account_id,
        subject_type="managed_agent",
        subject_id=duplicate.id,
        model_alias="gpt-5",
        period=BudgetPeriod.monthly,
        period_start=datetime(2026, 8, 1),
        spend_usd=1.5,
    )
    db_session.add(spend)
    db_session.commit()

    dry = client.post(
        f"/api/v1/agents/{survivor.id}/merge",
        json={"duplicate_agent_id": str(duplicate.id), "dry_run": True},
    )
    assert dry.status_code == 200, dry.text
    dry_body = dry.json()
    assert dry_body["dry_run"] is True
    assert dry_body["counts"]["usage_moved"] == 1
    assert dry_body["counts"]["budget_policies_dropped"] == 1

    # Dry-run must not archive the duplicate.
    db_session.refresh(duplicate)
    assert duplicate.lifecycle_state == "active"
    assert (duplicate.tags or {}).get("merged_into") is None

    execute = client.post(
        f"/api/v1/agents/{survivor.id}/merge",
        json={"duplicate_agent_id": str(duplicate.id), "dry_run": False},
    )
    assert execute.status_code == 200, execute.text
    body = execute.json()
    assert body["dry_run"] is False
    assert body["counts"]["budget_policies_dropped"] == 1
    assert body["counts"]["dropped_budget_policies"][0]["kept_policy_id"] == str(
        survivor_policy.id
    )

    db_session.refresh(duplicate)
    assert duplicate.lifecycle_state == "decommissioned"
    assert (duplicate.tags or {})["merged_into"] == str(survivor.id)
    assert (
        db_session.query(models.ApiUsage)
        .filter(models.ApiUsage.runtime_principal_id == "codex-survivor01")
        .count()
        == 1
    )
    assert (
        db_session.query(BudgetPolicy)
        .filter(BudgetPolicy.id == duplicate_policy.id)
        .count()
        == 0
    )
    assert (
        db_session.query(BudgetPolicy)
        .filter(BudgetPolicy.id == survivor_policy.id)
        .count()
        == 1
    )


def test_merge_deletes_duplicate_imported_fingerprint_collisions(
    client, db_session, test_user
):
    survivor = _make_agent(db_session, test_user, source_id="codex-surv-import")
    duplicate = _make_agent(db_session, test_user, source_id="codex-dup-import")
    ts = datetime(2026, 7, 15, 12, 0, 0)
    shared_meta_base = {
        "import_source": "cursor_csv",
        "source_session_id": "sess-shared",
    }
    survivor_fp = event_fingerprint(
        source="cursor_csv",
        agent_principal_id="codex-surv-import",
        timestamp=ts,
        model="composer-1",
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost=0.1,
        session_id="sess-shared",
    )
    dup_fp = event_fingerprint(
        source="cursor_csv",
        agent_principal_id="codex-dup-import",
        timestamp=ts,
        model="composer-1",
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost=0.1,
        session_id="sess-shared",
    )
    survivor_row = models.ApiUsage(
        id=uuid4(),
        account_id=test_user.account_id,
        endpoint="/imported",
        method="POST",
        status_code=200,
        duration=0.0,
        action_type="imported_usage",
        model_alias="composer-1",
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        estimated_cost=0.1,
        runtime_principal_type="codex",
        runtime_principal_id="codex-surv-import",
        meta_data={**shared_meta_base, "import_fingerprint": survivor_fp},
        timestamp=ts,
    )
    dup_row = models.ApiUsage(
        id=uuid4(),
        account_id=test_user.account_id,
        endpoint="/imported",
        method="POST",
        status_code=200,
        duration=0.0,
        action_type="imported_usage",
        model_alias="composer-1",
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        estimated_cost=0.1,
        runtime_principal_type="codex",
        runtime_principal_id="codex-dup-import",
        meta_data={**shared_meta_base, "import_fingerprint": dup_fp},
        timestamp=ts,
    )
    db_session.add_all([survivor_row, dup_row])
    db_session.commit()

    response = client.post(
        f"/api/v1/agents/{survivor.id}/merge",
        json={"duplicate_agent_id": str(duplicate.id), "dry_run": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["usage_deleted"] == 1
    assert (
        db_session.query(models.ApiUsage)
        .filter(models.ApiUsage.account_id == test_user.account_id)
        .count()
        == 1
    )


def test_upsert_preserves_display_name(db_session, test_user):
    agent = crud_managed_agent.upsert_from_runtime_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=None,
        session_source_type="codex",
        session_source_id="codex-preserve-name",
        display_name="Original Name",
        session_reference="/tmp/codex/config.toml",
        enrollment_hostname="tuvok",
        identity_derivation="v2",
    )
    db_session.commit()
    updated = crud_managed_agent.upsert_from_runtime_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=None,
        session_source_type="codex",
        session_source_id="codex-preserve-name",
        display_name="Renamed At Onboard",
        session_reference="/tmp/codex/config.toml",
    )
    db_session.commit()
    assert updated.display_name == "Original Name"
    assert str(updated.id) == str(agent.id)
