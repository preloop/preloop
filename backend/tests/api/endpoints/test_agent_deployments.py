"""Endpoint authorization, model ownership and verified deployment results."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import agent_deployments as api
from preloop.services.agent_deployment import DeploymentError, DeploymentResult


def identity():
    return SimpleNamespace(id=uuid4(), account_id=uuid4(), is_superuser=False)


def test_non_owner_is_denied_without_optional_rbac_plugin():
    user = identity()
    account = SimpleNamespace(is_active=True, primary_user_id=uuid4())
    with (
        patch.object(api.crud_account, "get", return_value=account),
        patch.object(api.crud_user_role, "get_user_roles", return_value=[]),
    ):
        with pytest.raises(HTTPException) as exc:
            api.authorize_deployment(MagicMock(), user)
    assert exc.value.status_code == 403


def test_account_owner_can_deploy_without_optional_rbac_plugin():
    user = identity()
    account = SimpleNamespace(is_active=True, primary_user_id=user.id)
    with patch.object(api.crud_account, "get", return_value=account):
        api.authorize_deployment(MagicMock(), user)


def test_other_account_role_cannot_grant_deployment():
    user = identity()
    account = SimpleNamespace(is_active=True, primary_user_id=uuid4())
    role = SimpleNamespace(is_system_role=True, name="owner", account_id=uuid4())
    with (
        patch.object(api.crud_account, "get", return_value=account),
        patch.object(api.crud_user_role, "get_user_roles", return_value=[role]),
    ):
        with pytest.raises(HTTPException):
            api.authorize_deployment(MagicMock(), user)


@pytest.mark.parametrize(
    "agent",
    [
        None,
        SimpleNamespace(agent_kind="openclaw", lifecycle_state="active"),
        SimpleNamespace(agent_kind="hermes", lifecycle_state="paused"),
    ],
)
def test_host_claim_does_not_override_missing_wrong_kind_or_paused_agent(agent):
    evidence = DeploymentResult(str(uuid4()), "Hermes 2026.9.14", "selected-model")
    with patch.object(
        api.crud_managed_agent, "get_for_account", return_value=agent
    ) as lookup:
        with pytest.raises(DeploymentError):
            api.verify_registered_agent(
                MagicMock(),
                account_id="account",
                model_id="model",
                runtime="hermes",
                alias="selected-model",
                evidence=evidence,
            )
    assert lookup.call_args.kwargs["account_id"] == "account"


@pytest.mark.parametrize(
    "binding",
    [
        SimpleNamespace(
            ai_model_id="other", gateway_alias="selected-model", status="gateway_ready"
        ),
        SimpleNamespace(
            ai_model_id="model", gateway_alias="other-model", status="gateway_ready"
        ),
        SimpleNamespace(
            ai_model_id="model", gateway_alias="selected-model", status="inactive"
        ),
    ],
)
def test_wrong_model_or_unready_binding_cannot_report_success(binding):
    agent = SimpleNamespace(agent_kind="hermes", lifecycle_state="active")
    evidence = DeploymentResult(str(uuid4()), "Hermes 2026.9.14", "selected-model")
    with (
        patch.object(api.crud_managed_agent, "get_for_account", return_value=agent),
        patch.object(
            api.crud_managed_agent_ai_model_binding,
            "list_for_agent",
            return_value=[binding],
        ),
    ):
        with pytest.raises(DeploymentError):
            api.verify_registered_agent(
                MagicMock(),
                account_id="account",
                model_id="model",
                runtime="hermes",
                alias="selected-model",
                evidence=evidence,
            )


def test_verified_account_model_presence_and_version_return_real_summary():
    agent = SimpleNamespace(
        agent_kind="hermes", lifecycle_state="active", last_seen_at="now"
    )
    evidence = DeploymentResult(str(uuid4()), "Hermes 2026.9.14", "selected-model")
    binding = SimpleNamespace(
        ai_model_id="model", gateway_alias="selected-model", status="gateway_ready"
    )
    summary = {"id": evidence.agent_id, "display_name": "Real runtime"}
    with (
        patch.object(api.crud_managed_agent, "get_for_account", return_value=agent),
        patch.object(
            api.crud_managed_agent_ai_model_binding,
            "list_for_agent",
            return_value=[binding],
        ),
        patch.object(
            api.crud_managed_agent, "get_summary_for_account", return_value=summary
        ),
    ):
        assert (
            api.verify_registered_agent(
                MagicMock(),
                account_id="account",
                model_id="model",
                runtime="hermes",
                alias="selected-model",
                evidence=evidence,
            )
            == summary
        )
