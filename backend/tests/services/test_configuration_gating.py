"""Single-person governance is basic; multi-person configuration is commercial."""

from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException
from preloop.services.configuration_gating import (
    authorize_team_approval_configuration,
    register_configuration_authorizer,
    configuration_capabilities,
)


@pytest.fixture(autouse=True)
def clear_policy():
    register_configuration_authorizer(None)
    yield
    register_configuration_authorizer(None)


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"approver_user_ids": [uuid4()], "approvals_required": 1},
        {
            "approval_type": "standard",
            "timeout_seconds": 300,
            "async_approval_enabled": True,
        },
    ],
)
def test_single_person_workflow_needs_no_commercial_plugin(config):
    authorize_team_approval_configuration(None, "account", config)


@pytest.mark.parametrize(
    "config",
    [
        {"approver_user_ids": [uuid4(), uuid4()]},
        {"approver_team_ids": [uuid4()]},
        {"approvals_required": 2},
        {"escalation_user_ids": [uuid4()]},
        {"escalation_workflow_id": uuid4()},
        {"workflow_type": "multi_stage", "workflow_config": {"stages": [{}]}},
    ],
)
def test_advanced_configuration_is_paid_but_unchanged_existing_policy_survives(config):
    with pytest.raises(HTTPException) as error:
        authorize_team_approval_configuration(None, "account", config)
    assert error.value.status_code == 402
    authorize_team_approval_configuration(
        None, "account", config, SimpleNamespace(**config)
    )


def test_commercial_team_policy_authorizes_advanced_forms():
    calls = []
    register_configuration_authorizer(
        lambda db, account, feature: calls.append((account, feature))
    )
    authorize_team_approval_configuration(
        None, "account", {"approver_user_ids": [uuid4(), uuid4()]}
    )
    assert calls == [("account", "team_approvals")]
    assert configuration_capabilities(None, "account")["advanced_approvals"] is True


@pytest.mark.parametrize(
    "workflow",
    [
        {"approvals_required": 2},
        {"approver_users": ["alice", "bob"]},
        {"approver_teams": ["Engineering"]},
        {"escalation_users": ["alice"]},
    ],
)
def test_policy_import_cannot_bypass_advanced_configuration_gate(db_session, workflow):
    from preloop.services.policy.loader import PolicyApplier
    from preloop.services.policy.schema import PolicyDocument
    from preloop.models.crud import crud_approval_workflow
    from preloop.models.crud import crud_account

    account = crud_account.create(
        db_session, obj_in={"organization_name": "Import gating"}
    )
    policy = PolicyDocument.model_validate(
        {
            "metadata": {"name": "Core import"},
            "approval_workflows": [{"name": "Advanced imported", **workflow}],
        }
    )
    result = PolicyApplier(db_session, account.id).apply(policy)
    assert result.success is False
    assert (
        crud_approval_workflow.get_by_name(
            db_session, account_id=str(account.id), name="Advanced imported"
        )
        is None
    )


@pytest.fixture
def import_account(db_session):
    from preloop.models.crud import crud_account, crud_user

    account = crud_account.create(
        db_session, obj_in={"organization_name": "Import routing"}
    )
    users = []
    for name in ("alice", "bob"):
        users.append(
            crud_user.create(
                db_session,
                obj_in={
                    "account_id": account.id,
                    "username": name + uuid4().hex,
                    "email": uuid4().hex + "@example.test",
                    "hashed_password": "test",
                    "is_active": True,
                },
            )
        )
    crud_account.update(
        db_session, db_obj=account, obj_in={"primary_user_id": users[0].id}
    )
    return account, users


@pytest.mark.parametrize("explicit", [False, True])
def test_policy_import_keeps_basic_single_approver_workflow(
    db_session, import_account, explicit
):
    from preloop.services.policy.loader import PolicyApplier
    from preloop.services.policy.schema import PolicyDocument
    from preloop.models.crud import crud_approval_workflow

    account, users = import_account
    recipient = users[1] if explicit else users[0]
    workflow = {"name": "One human", "approvals_required": 1}
    if explicit:
        workflow["approver_users"] = [recipient.username]
    policy = PolicyDocument.model_validate(
        {"metadata": {"name": "Core import"}, "approval_workflows": [workflow]}
    )
    result = PolicyApplier(db_session, account.id).apply(policy)
    assert result.success is True, result.errors
    persisted = crud_approval_workflow.get_by_name(
        db_session, account_id=str(account.id), name="One human"
    )
    assert list(map(str, persisted.approver_user_ids)) == [str(recipient.id)]


def test_import_uses_authenticated_actor_default(db_session, import_account):
    from preloop.services.policy.loader import PolicyApplier
    from preloop.services.policy.schema import PolicyDocument
    from preloop.models.crud import crud_approval_workflow

    account, users = import_account
    policy = PolicyDocument.model_validate(
        {
            "metadata": {"name": "Core import"},
            "approval_workflows": [{"name": "Actor approval"}],
        }
    )
    result = PolicyApplier(db_session, account.id, actor_id=users[1].id).apply(policy)
    assert result.success, result.errors
    persisted = crud_approval_workflow.get_by_name(
        db_session, account_id=str(account.id), name="Actor approval"
    )
    assert list(map(str, persisted.approver_user_ids)) == [str(users[1].id)]


def test_advanced_import_is_idempotent_after_downgrade(db_session, import_account):
    from preloop.services.policy.loader import PolicyApplier
    from preloop.services.policy.schema import PolicyDocument
    from preloop.models.crud import crud_approval_workflow

    account, users = import_account
    policy = PolicyDocument.model_validate(
        {
            "metadata": {"name": "Advanced import"},
            "approval_workflows": [
                {
                    "name": "Two humans",
                    "approver_users": [user.username for user in users],
                    "approvals_required": 2,
                    "escalation_workflow": "Escalation",
                    "escalation_users": [users[1].username],
                },
                {"name": "Escalation", "approver_users": [users[0].username]},
            ],
        }
    )
    register_configuration_authorizer(lambda *_: None)
    result = PolicyApplier(db_session, account.id).apply(policy)
    assert result.success, result.errors
    register_configuration_authorizer(None)
    result = PolicyApplier(db_session, account.id).apply(policy)
    assert result.success, result.errors
    persisted = crud_approval_workflow.get_by_name(
        db_session, account_id=str(account.id), name="Two humans"
    )
    escalation = crud_approval_workflow.get_by_name(
        db_session, account_id=str(account.id), name="Escalation"
    )
    assert set(map(str, persisted.approver_user_ids)) == {
        str(user.id) for user in users
    }
    assert persisted.escalation_workflow_id == escalation.id
    assert list(map(str, persisted.escalation_user_ids)) == [str(users[1].id)]
    from preloop.services.policy.loader import export_current_policy

    exported = export_current_policy(db_session, account.id)
    exported_workflow = next(
        item for item in exported.approval_workflows if item.name == "Two humans"
    )
    assert set(exported_workflow.approver_users) == {user.username for user in users}
    exported_workflow.approver_users.reverse()
    result = PolicyApplier(db_session, account.id).apply(exported)
    assert result.success, result.errors


def test_import_rejects_foreign_recipient_before_creating_other_workflows(
    db_session, import_account
):
    from preloop.services.policy.loader import PolicyApplier
    from preloop.services.policy.schema import PolicyDocument
    from preloop.models.crud import crud_approval_workflow, crud_account, crud_user

    account, _ = import_account
    other = crud_account.create(db_session, obj_in={"organization_name": "Other"})
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": other.id,
            "username": "foreign" + uuid4().hex,
            "email": uuid4().hex + "@example.test",
            "hashed_password": "test",
        },
    )
    policy = PolicyDocument.model_validate(
        {
            "metadata": {"name": "Bad routing"},
            "approval_workflows": [
                {"name": "Valid first"},
                {"name": "Foreign", "approver_users": [user.username]},
            ],
        }
    )
    result = PolicyApplier(db_session, account.id).apply(policy)
    assert not result.success
    assert (
        crud_approval_workflow.get_multi_by_account(
            db_session, account_id=str(account.id)
        )
        == []
    )


@pytest.mark.asyncio
async def test_oss_member_reaches_approval_configuration_import(
    db_session, import_account, monkeypatch
):
    from io import BytesIO
    from starlette.datastructures import UploadFile
    from preloop.api.endpoints.policies import upload_policy
    from preloop.utils import permissions

    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    account, users = import_account
    # Core has no role checks, so the member passes the permission gate and
    # the import runs to completion.
    result = await upload_policy(
        file=UploadFile(BytesIO(b"metadata: {name: Test}")),
        dry_run=False,
        resolve_env=False,
        skip_missing_servers=False,
        account=account,
        current_user=users[1],
        db=db_session,
    )
    assert result is not None


def test_import_new_default_unmarks_existing_without_partial_flush(
    db_session, import_account
):
    from preloop.services.policy.loader import PolicyApplier
    from preloop.services.policy.schema import PolicyDocument
    from preloop.models.crud import crud_approval_workflow

    account, users = import_account
    existing = crud_approval_workflow.create(
        db_session,
        account_id=str(account.id),
        obj_in={
            "name": "Old default",
            "approval_type": "standard",
            "is_default": True,
            "approver_user_ids": [str(users[0].id)],
        },
    )
    policy = PolicyDocument.model_validate(
        {
            "metadata": {"name": "Default import"},
            "approval_workflows": [{"name": "New default", "is_default": True}],
        }
    )
    result = PolicyApplier(db_session, account.id).apply(policy)
    assert result.success, result.errors
    db_session.refresh(existing)
    assert not existing.is_default
    default = crud_approval_workflow.get_default(db_session, account_id=str(account.id))
    assert default.name == "New default"
    assert list(map(str, default.approver_user_ids)) == [str(users[0].id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mechanism", [None, "standard", "manual", "slack", "mattermost", "webhook"]
)
@pytest.mark.parametrize("mode", ["standard", "manual", "legacy-human"])
async def test_update_empty_human_routing_keeps_actor_as_approver(
    db_session, import_account, monkeypatch, mechanism, mode
):
    from preloop.api.endpoints.tools import (
        create_approval_workflow,
        update_approval_workflow,
    )
    from preloop.models.schemas.tool_configuration import (
        ApprovalWorkflowCreate,
        ApprovalWorkflowUpdate,
    )
    from preloop.utils import permissions

    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    account, users = import_account
    created = await create_approval_workflow(
        workflow_data=ApprovalWorkflowCreate(
            name="Actor default",
            approval_mode=mode,
            **({"approval_type": mechanism} if mechanism else {}),
        ),
        account=account,
        current_user=users[0],
        db=db_session,
    )
    assert list(map(str, created.approver_user_ids)) == [str(users[0].id)]
    updated = await update_approval_workflow(
        workflow_id=created.id,
        workflow_update=ApprovalWorkflowUpdate(
            approver_user_ids=[], approver_team_ids=[]
        ),
        account=account,
        current_user=users[0],
        db=db_session,
    )
    assert list(map(str, updated.approver_user_ids)) == [str(users[0].id)]


@pytest.mark.asyncio
async def test_ai_driven_empty_routing_is_not_defaulted_to_actor(
    db_session, import_account, monkeypatch
):
    from preloop.api.endpoints.tools import create_approval_workflow
    from preloop.models.schemas.tool_configuration import ApprovalWorkflowCreate
    from preloop.utils import permissions

    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    account, users = import_account
    created = await create_approval_workflow(
        workflow_data=ApprovalWorkflowCreate(
            name="AI empty",
            approval_mode="ai_driven",
            approval_type="slack",
        ),
        account=account,
        current_user=users[0],
        db=db_session,
    )
    assert not created.approver_user_ids
    assert not created.approver_team_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient_kind", ["user", "team"])
@pytest.mark.parametrize("mode", ["standard", "manual", "legacy-human", "ai_driven"])
async def test_create_and_update_preserve_explicit_approval_recipients(
    db_session, import_account, monkeypatch, recipient_kind, mode
):
    from preloop.api.endpoints.tools import (
        create_approval_workflow,
        update_approval_workflow,
    )
    from preloop.models import models
    from preloop.models.schemas.tool_configuration import (
        ApprovalWorkflowCreate,
        ApprovalWorkflowUpdate,
    )
    from preloop.utils import permissions

    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    register_configuration_authorizer(lambda *_: None)
    account, users = import_account
    team = models.Team(account_id=account.id, name="Reviewers")
    db_session.add(team)
    db_session.flush()
    recipients = {
        "approver_user_ids": [users[1].id] if recipient_kind == "user" else [],
        "approver_team_ids": [team.id] if recipient_kind == "team" else [],
    }
    created = await create_approval_workflow(
        workflow_data=ApprovalWorkflowCreate(
            name="Explicit recipients",
            approval_mode=mode,
            approval_type="slack",
            **recipients,
        ),
        account=account,
        current_user=users[0],
        db=db_session,
    )
    for key, expected in recipients.items():
        assert list(map(str, getattr(created, key) or [])) == list(map(str, expected))
    updated = await update_approval_workflow(
        workflow_id=created.id,
        workflow_update=ApprovalWorkflowUpdate(description="Keep routing"),
        account=account,
        current_user=users[0],
        db=db_session,
    )
    for key, expected in recipients.items():
        assert list(map(str, getattr(updated, key) or [])) == list(map(str, expected))


def test_missing_mode_uses_runtime_human_default():
    from preloop.api.endpoints.tools import _empty_human_approver_default

    actor = uuid4()
    assert _empty_human_approver_default(
        approval_mode=None,
        approval_type="slack",
        approver_user_ids=None,
        approver_team_ids=None,
        actor_id=actor,
    ) == [actor]
