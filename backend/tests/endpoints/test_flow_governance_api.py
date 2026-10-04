"""GET/PUT/DELETE /api/v1/account/governance/flows/{flow_id}."""

from __future__ import annotations

import uuid

import pytest

from preloop.models.crud import crud_account, crud_flow
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import Decision
from preloop.models.schemas.flow import FlowCreate


@pytest.fixture(autouse=True)
def _no_hooks():
    account_hooks.reset_account_hooks()
    yield
    account_hooks.reset_account_hooks()


def _flow(db_session, account_id):
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"Governed Flow {uuid.uuid4().hex[:8]}",
            trigger_event_source="github",
            trigger_event_types=["push"],
            prompt_template="Review {{payload.message}}",
            agent_type="openhands",
            agent_config={},
            account_id=account_id,
        ),
        account_id=account_id,
    )


def _url(flow_id):
    return f"/api/v1/account/governance/flows/{flow_id}"


def test_flow_governance_round_trip_and_reset(client, db_session, test_user):
    flow = _flow(db_session, test_user.account_id)
    account = crud_account.get(db_session, id=test_user.account_id)
    crud_account.update(
        db_session,
        db_obj=account,
        obj_in={
            "meta_data": {
                "subject_governance": {
                    "account_defaults": {"native_tool_approvals": "off"}
                }
            }
        },
    )

    initial = client.get(_url(flow.id))
    assert initial.status_code == 200
    body = initial.json()
    assert body["subject_type"] == "flows"
    assert body["has_override"] is False
    assert body["account_defaults"]["native_tool_approvals"] == "off"

    updated = client.put(
        _url(flow.id),
        json={
            "allowed_models": ["openai/gpt-5-mini"],
            "tool_rules": {"create_issue": [{"action": "require_approval"}]},
            "tool_enabled_overrides": {"delete_issue": False},
            "native_tool_approvals": "enforce",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["has_override"] is True
    assert updated.json()["config"]["allowed_models"] == ["openai/gpt-5-mini"]
    assert client.get(_url(flow.id)).json()["config"] == updated.json()["config"]

    reset = client.delete(_url(flow.id))
    assert reset.status_code == 200
    assert reset.json()["has_override"] is False
    assert reset.json()["config"]["allowed_models"] == []
    db_session.refresh(account)
    store = account.meta_data["subject_governance"]
    assert str(flow.id) not in store.get("flows", {})
    # Resetting a flow must not touch the account defaults.
    assert store["account_defaults"]["native_tool_approvals"] == "off"


def test_flow_governance_rejects_foreign_or_unknown_flow(client, db_session):
    assert client.get(_url(uuid.uuid4())).status_code == 404
    assert client.get(_url("not-a-uuid")).status_code == 404
    assert client.put(_url(uuid.uuid4()), json={}).status_code == 404


def test_flow_governance_rejects_foreign_workflow(client, db_session, test_user):
    flow = _flow(db_session, test_user.account_id)
    response = client.put(
        _url(flow.id), json={"approval_workflow_id": str(uuid.uuid4())}
    )
    assert response.status_code == 400


def test_flow_governance_write_requires_edit_flows(client, db_session, test_user):
    flow = _flow(db_session, test_user.account_id)
    actions: list[str] = []

    def authorizer(ctx, action, resource):
        actions.append(action)
        if action == "edit_flows":
            return Decision("deny", rule_ids=("r1",), reason="no edit")
        return Decision("allow")

    account_hooks.register_authorizer(authorizer)

    assert client.get(_url(flow.id)).status_code == 200
    assert client.put(_url(flow.id), json={}).status_code == 403
    assert client.delete(_url(flow.id)).status_code == 403
    assert actions == ["view_flows", "edit_flows", "edit_flows"]
