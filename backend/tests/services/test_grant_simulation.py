"""Synthetic grant dry runs apply the same gate without introspection HTTP."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from preloop.services.policy_simulation import PolicyEvaluationRequest, simulate_policy


@pytest.mark.asyncio
@pytest.mark.parametrize("active,decision", [(True, "deny"), (False, "allow")])
async def test_grant_rule_uses_only_supplied_sample(
    active: bool, decision: str
) -> None:
    request = PolicyEvaluationRequest(
        name="read_record",
        grant={"active": active},
        draft_rule={"action": "deny", "condition_expression": "grant.active == true"},
    )
    with (
        patch(
            "preloop.services.grant_introspection.grant_introspector.evaluate",
            new_callable=AsyncMock,
        ) as introspect,
        patch("preloop.services.policy_evaluator._log_policy_decision_async") as audit,
    ):
        result = await simulate_policy(
            request, db=MagicMock(), account_id=uuid4(), user_id=uuid4()
        )
    assert result.decision == decision
    introspect.assert_not_called()
    audit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample,fail_open,reason",
    [
        ({"active": False}, False, "grant_inactive"),
        ({"active": True, "exp": 1, "scope": ["read"]}, False, "grant_inactive"),
        ({"active": True}, False, "scope_not_granted"),
        ({"active": False, "available": False}, False, "introspection_unavailable"),
        ({"active": False, "available": False}, True, None),
        ({"active": True, "scope": ["read"]}, False, None),
    ],
)
async def test_yaml_sample_applies_shared_grant_gate_without_http(
    sample: dict, fail_open: bool, reason: str | None
) -> None:
    request = PolicyEvaluationRequest(
        name="read_record",
        server="protected",
        grant=sample,
        draft_yaml=f"""version: "1.0"
metadata:
  name: Synthetic grant simulation
mcp_servers:
  - name: protected
    url: https://mcp.example.com/mcp
    auth_type: bearer
    auth_config:
      token: synthetic-upstream
      introspection:
        endpoint: https://as.example.com/introspect
        client_id: synthetic-client
        client_secret: synthetic-secret
        required_scopes: [read]
        fail_open: {str(fail_open).lower()}
tools:
  - name: read_record
    source: protected
    conditions:
      - expression: "true"
        action: allow
""",
    )
    with patch(
        "preloop.services.grant_introspection.grant_introspector.evaluate",
        new_callable=AsyncMock,
    ) as introspect:
        result = await simulate_policy(
            request, db=MagicMock(), account_id=uuid4(), user_id=uuid4()
        )
    assert result.decision == ("deny" if reason else "allow")
    if reason:
        assert result.description == reason
    else:
        assert result.matched_rule == "draft-1"
    introspect.assert_not_called()


@pytest.mark.parametrize(
    "sample",
    [
        {"active": True, "token": "synthetic-secret"},
        {"active": "true"},
        {"active": True, "scope": "read"},
    ],
)
def test_synthetic_grant_rejects_credentials_and_wrong_types(sample: dict) -> None:
    with pytest.raises(ValidationError):
        PolicyEvaluationRequest(
            name="read_record", grant=sample, draft_rule={"action": "allow"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
@pytest.mark.parametrize("supplied", [False, True])
async def test_configured_account_server_requires_a_synthetic_sample(
    stored: bool, supplied: bool
) -> None:
    source = {"stored": True} if stored else {"draft_rule": {"action": "allow"}}
    request = PolicyEvaluationRequest(
        name="read_record",
        server="protected",
        grant={"active": False} if supplied else None,
        **source,
    )
    server = SimpleNamespace(
        id=uuid4(),
        auth_config={
            "token": "synthetic-upstream",
            "introspection": {
                "endpoint": "https://as.example.com/introspect",
                "client_id": "test",
                "client_secret": "synthetic-secret",
            },
        },
    )
    account_id = uuid4()
    with (
        patch(
            "preloop.services.policy_simulation.crud_mcp_server.get_by_name",
            return_value=server,
        ) as lookup,
        patch(
            "preloop.services.policy_simulation.load_sensitive_data_config",
            return_value=None,
        ),
        patch(
            "preloop.services.grant_introspection.grant_introspector.evaluate",
            new_callable=AsyncMock,
        ) as introspect,
        patch("preloop.services.policy_evaluator._log_policy_decision_async") as audit,
    ):
        if supplied:
            result = await simulate_policy(
                request, db=MagicMock(), account_id=account_id, user_id=uuid4()
            )
            assert result.decision == "deny" and result.description == "grant_inactive"
            assert "synthetic-secret" not in result.model_dump_json()
        else:
            with pytest.raises(ValueError, match="synthetic grant sample"):
                await simulate_policy(
                    request, db=MagicMock(), account_id=account_id, user_id=uuid4()
                )
        assert lookup.call_args.kwargs["account_id"] == str(account_id)
        introspect.assert_not_called()
        audit.assert_not_called()
