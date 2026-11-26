"""Readiness mirrors native fallback routing without sending notifications."""

from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services import approval_readiness as readiness


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Build synthetic routing facts; no providers or database are contacted."""
    account = models.Account(id=UUID(int=1), meta_data={})
    user = models.User(
        id=UUID(int=2),
        account_id=account.id,
        username="reviewer",
        full_name="Jane Doe",
        email="reviewer@example.com",
        is_active=True,
    )
    workflow = models.ApprovalWorkflow(
        id=UUID(int=3),
        account_id=account.id,
        name="Tool review",
        approval_type="standard",
        approval_mode="standard",
        timeout_seconds=300,
        approver_user_ids=[user.id],
        approvals_required=1,
    )
    monkeypatch.setattr(readiness.crud_readiness, "members", lambda *a, **k: [user])
    monkeypatch.setattr(readiness.crud_readiness, "deciders", lambda db, users: users)
    monkeypatch.setattr(readiness.crud_readiness, "recipients", lambda *a: {user.id})
    monkeypatch.setattr(
        readiness.crud_readiness, "chat_destinations", lambda *a, **k: []
    )
    monkeypatch.setattr(
        readiness.crud_readiness, "muted_user_ids", lambda *a, **k: set()
    )
    monkeypatch.setattr(readiness.crud_readiness, "preferences", lambda *a, **k: {})
    monkeypatch.setattr(readiness, "email_is_configured", lambda: True)
    monkeypatch.setattr(readiness, "get_apns_service", lambda: None)
    monkeypatch.setattr(readiness, "is_fcm_configured", lambda: False)
    monkeypatch.setattr(readiness, "is_push_proxy_configured", lambda: False)
    return MagicMock(), account, user, workflow


def test_owner_without_notifications_warns_but_can_decide(context: tuple) -> None:
    db, account, user, workflow = context
    result = readiness.describe_readiness(
        db, account=account, workflow=workflow, owner_id=user.id
    )
    assert result.deciders == ["Jane Doe"]
    assert result.destinations == []
    assert any("Only the agent's owner" in issue for issue in result.issues)
    db.commit.assert_not_called()


def test_email_destination_and_quorum(
    context: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, account, user, workflow = context
    monkeypatch.setattr(
        readiness.crud_readiness,
        "preferences",
        lambda *a, **k: {user.id: models.NotificationPreferences(enable_email=True)},
    )
    result = readiness.describe_readiness(db, account=account, workflow=workflow)
    assert result.destinations == ["Email"]
    assert not result.issues
    workflow.approvals_required = 2
    assert any(
        "requires 2" in issue
        for issue in readiness.describe_readiness(
            db, account=account, workflow=workflow
        ).issues
    )


def test_no_active_member_or_recipient(
    context: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, account, _, workflow = context
    monkeypatch.setattr(readiness.crud_readiness, "members", lambda *a, **k: [])
    result = readiness.describe_readiness(db, account=account, workflow=workflow)
    assert any("No active account member" in issue for issue in result.issues)
    assert any("No active workflow recipients" in issue for issue in result.issues)


def test_webhook_destination_does_not_need_user_notifications(context: tuple) -> None:
    db, account, _, workflow = context
    workflow.channel_configs = {"slack": {"url": "https://example.com/hook"}}
    result = readiness.describe_readiness(db, account=account, workflow=workflow)
    assert result.destinations == ["Slack"]
    assert "https://" not in result.model_dump_json()
    assert not result.issues


def test_ai_escalation_uses_recipients_but_original_timeout(
    context: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, account, _, workflow = context
    workflow.approval_mode = "ai_driven"
    workflow.ai_fallback_behavior = "escalate"
    workflow.escalation_workflow_id = UUID(int=4)
    fallback = models.ApprovalWorkflow(
        id=UUID(int=4),
        account_id=account.id,
        name="Human review",
        timeout_seconds=900,
        approvals_required=1,
    )
    get = MagicMock(return_value=fallback)
    monkeypatch.setattr(readiness.crud_approval_workflow, "get", get)
    recipients = MagicMock(return_value=set())
    monkeypatch.setattr(readiness.crud_readiness, "recipients", recipients)
    result = readiness.describe_readiness(db, account=account, workflow=workflow)
    assert result.timeout_seconds == 300
    assert "Human review" in result.workflow_name
    recipients.assert_called_once_with(db, fallback)


def test_ai_automatic_fallback_has_no_human_warning(context: tuple) -> None:
    db, account, _, workflow = context
    workflow.approval_mode = "ai_driven"
    workflow.ai_fallback_behavior = "deny"
    assert not readiness.describe_readiness(
        db, account=account, workflow=workflow
    ).issues


def test_all_recipient_bypass_mutes_destinations(
    context: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, account, user, workflow = context
    workflow.channel_configs = {"webhook": {"url": "https://example.com/hook"}}
    monkeypatch.setattr(
        readiness.crud_readiness,
        "muted_user_ids",
        lambda *a, **k: {user.id},
    )
    result = readiness.describe_readiness(db, account=account, workflow=workflow)
    assert not result.destinations
    assert any("muted" in issue for issue in result.issues)
    assert not any("No notification destination" in issue for issue in result.issues)


def test_native_workflow_precedence_and_no_creation(
    context: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, account, user, workflow = context
    account.meta_data = {
        "subject_governance": {
            "managed_agents": {
                str(user.id): {"approval_workflow_id": str(workflow.id)}
            },
            "account_defaults": {"approval_workflow_id": str(UUID(int=4))},
        }
    }
    get = MagicMock(return_value=workflow)
    monkeypatch.setattr(readiness.crud_approval_workflow, "get", get)
    assert readiness.native_workflow(db, account=account, agent_id=user.id) is workflow
    assert get.call_args.kwargs["id"] == workflow.id
    get.return_value = None
    default = MagicMock(return_value=None)
    first = MagicMock(return_value=[workflow])
    monkeypatch.setattr(readiness.crud_approval_workflow, "get_default", default)
    monkeypatch.setattr(readiness.crud_approval_workflow, "get_multi_by_account", first)
    assert readiness.native_workflow(db, account=account, agent_id=user.id) is workflow
    first.return_value = []
    assert readiness.native_workflow(db, account=account) is None
    assert readiness.describe_readiness(db, account=account, workflow=None).issues
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_push_requires_enabled_preference_token_and_provider(
    context: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enabled switch without a usable destination is not readiness."""
    db, account, user, workflow = context
    prefs = models.NotificationPreferences(
        enable_email=False,
        enable_mobile_push=True,
        notify_when_needed=True,
        mobile_device_tokens=[{"platform": "android", "token": "synthetic-token"}],
    )
    monkeypatch.setattr(
        readiness.crud_readiness, "preferences", lambda *a, **k: {user.id: prefs}
    )
    monkeypatch.setattr(readiness, "is_fcm_configured", lambda: False)
    assert not readiness.describe_readiness(
        db, account=account, workflow=workflow
    ).destinations
    monkeypatch.setattr(readiness, "is_fcm_configured", lambda: True)
    assert readiness.describe_readiness(
        db, account=account, workflow=workflow
    ).destinations == ["Mobile push"]
    prefs.notify_when_needed = False
    assert not readiness.describe_readiness(
        db, account=account, workflow=workflow
    ).destinations


@pytest.mark.parametrize("platform", ["ios", "android"])
def test_push_proxy_is_a_configured_destination(
    context: tuple, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """A proxy can deliver to registered devices without native push credentials."""
    db, account, user, workflow = context
    prefs = models.NotificationPreferences(
        enable_email=False,
        enable_mobile_push=True,
        notify_when_needed=True,
        mobile_device_tokens=[{"platform": platform, "token": "synthetic-token"}],
    )
    monkeypatch.setattr(
        readiness.crud_readiness, "preferences", lambda *a, **k: {user.id: prefs}
    )
    monkeypatch.setattr(readiness, "is_push_proxy_configured", lambda: True)
    result = readiness.describe_readiness(db, account=account, workflow=workflow)
    assert result.destinations == ["Mobile push"]
    assert not result.issues


def test_team_recipients_are_deduplicated_and_account_scoped(
    db_session: Session, test_user: models.User
) -> None:
    """The CRUD expansion includes an own team once and ignores another account."""
    from preloop.models.crud import crud_account
    from preloop.models.crud import approval_readiness as crud

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other example"}
    )
    team = models.Team(account_id=test_user.account_id, name="Reviewers")
    foreign = models.Team(account_id=other.id, name="Other reviewers")
    db_session.add_all([team, foreign])
    db_session.flush()
    db_session.add(models.TeamMembership(team_id=team.id, user_id=test_user.id))
    db_session.flush()
    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        approver_user_ids=[test_user.id],
        approver_team_ids=[team.id, foreign.id],
    )
    assert crud.recipients(db_session, workflow) == {test_user.id}


def test_external_service_does_not_claim_console_deciders(context: tuple) -> None:
    """External consent stays external even with no local notification settings."""
    db, account, _, workflow = context
    workflow.approval_config = {
        "external_approval_link": True,
        "webhook_url": "https://example.com/handoff",
    }
    result = readiness.describe_readiness(db, account=account, workflow=workflow)
    assert result.external
    assert result.deciders == []
    assert result.destinations == ["Webhook"]
    assert not result.issues


def test_batched_reads_preserve_team_permissions_and_bypass_scope(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three core reads cover every recipient while excluding foreign grants."""
    from datetime import datetime, timedelta
    from unittest.mock import patch
    from uuid import uuid4
    from sqlalchemy import select
    from preloop.models.crud import crud_account, crud_user
    from preloop.models.crud import approval_readiness as crud
    from preloop.plugins import account_hooks

    monkeypatch.setattr(crud, "members_have_full_access", lambda: False)
    monkeypatch.setattr(account_hooks, "_authorizer", None)
    users = [
        crud_user.create(
            db_session,
            obj_in={
                "account_id": test_user.account_id,
                "username": f"reviewer-{index}",
                "email": f"reviewer-{index}@example.com",
                "hashed_password": "synthetic",
                "is_active": True,
                "email_verified": True,
            },
        )
        for index in range(2)
    ]
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Other example"}
    )
    roles = [
        models.Role(account_id=aid, name=name, is_system_role=False)
        for aid, name in [
            (test_user.account_id, "Read approvals"),
            (test_user.account_id, "Decide approvals"),
            (foreign.id, "Foreign decisions"),
        ]
    ]
    db_session.add_all(roles)
    db_session.flush()
    for role, name in zip(
        roles, ["view_approvals", "decide_approvals", "decide_approvals"], strict=True
    ):
        permission = db_session.scalar(
            select(models.Permission).where(models.Permission.name == name)
        )
        if permission is None:
            permission = models.Permission(
                name=name, description="Synthetic permission", category="approvals"
            )
            db_session.add(permission)
            db_session.flush()
        db_session.add(
            models.RolePermission(role_id=role.id, permission_id=permission.id)
        )
    team = models.Team(account_id=test_user.account_id, name="Review team")
    db_session.add(team)
    db_session.flush()
    db_session.add_all(
        [models.UserRole(user_id=user.id, role_id=roles[0].id) for user in users]
        + [
            models.TeamMembership(user_id=users[0].id, team_id=team.id),
            models.TeamRole(team_id=team.id, role_id=roles[1].id),
            models.UserRole(user_id=users[1].id, role_id=roles[2].id),
        ]
    )
    for user in users:
        db_session.add(
            models.NotificationPreferences(user_id=user.id, enable_email=True)
        )
    now = datetime.utcnow()
    for user, agent_id, expiry in [
        (users[0], None, now + timedelta(hours=1)),
        (users[1], uuid4(), now + timedelta(hours=1)),
        (users[1], None, now - timedelta(hours=1)),
    ]:
        db_session.add(
            models.ApprovalBypass(
                account_id=test_user.account_id,
                user_id=user.id,
                managed_agent_id=agent_id,
                mode="mute_notifications",
                expires_at=expiry,
                created_by_user_id=test_user.id,
            )
        )
    db_session.flush()
    ids = {user.id for user in users}
    with patch.object(db_session, "scalars", wraps=db_session.scalars) as reads:
        assert crud.deciders(db_session, users) == [users[0]]
        assert (
            set(
                crud.preferences(
                    db_session, account_id=test_user.account_id, user_ids=ids
                )
            )
            == ids
        )
        assert crud.muted_user_ids(
            db_session, account_id=test_user.account_id, user_ids=ids, agent_id=None
        ) == {users[0].id}
        assert reads.call_count == 3
    monkeypatch.setattr(
        account_hooks,
        "_authorizer",
        lambda *args: account_hooks.Decision(effect="deny"),
    )
    assert crud.deciders(db_session, users) == []
