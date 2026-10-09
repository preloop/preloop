"""Sharing uses transactional receipts and closed recipient projections."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models.hierarchy import place_under
from preloop.models.crud.resource_share import (
    ShareConflictError,
    crud_resource_share as crud,
    install_materializer,
    uninstall_materializer,
)
from preloop.schemas.resource_share import ResourceShareDefinition


@pytest.fixture
def tree(db_session: Session) -> Any:
    owner = models.Account(organization_name="Parent")
    child = models.Account(organization_name="Child")
    sibling = models.Account(organization_name="Sibling")
    outsider = models.Account(organization_name="Unrelated")
    db_session.add(owner)
    db_session.flush()
    place_under(child, owner)
    place_under(sibling, owner)
    db_session.add_all([child, sibling, outsider])
    user = models.User(
        account_id=owner.id,
        username=str(uuid4()),
        email=f"{uuid4()}@example.com",
        hashed_password="synthetic",
    )
    db_session.add(user)
    db_session.flush()
    now = datetime.now(timezone.utc)
    resources = {
        "ai_model": models.AIModel(
            account_id=owner.id,
            name="Shared model",
            provider_name="example",
            model_identifier="synthetic-model",
            api_key="synthetic-secret",
        ),
        "mcp_server": models.MCPServer(
            account_id=owner.id,
            name="Shared tool",
            url="https://example.com/mcp",
            auth_config={"token": "synthetic-secret"},
        ),
        "managed_agent": models.ManagedAgent(
            account_id=owner.id,
            display_name="Shared worker",
            agent_kind="codex",
            session_source_type="codex",
            session_source_id=str(uuid4()),
            lifecycle_updated_at=now,
            last_seen_at=now,
        ),
    }
    db_session.add_all(resources.values())
    db_session.flush()
    install_materializer(lambda rules, subject, resource, rule_id: True)
    try:
        yield owner, child, sibling, outsider, user, resources
    finally:
        uninstall_materializer()


@pytest.mark.parametrize("kind", ["ai_model", "mcp_server", "managed_agent"])
def test_selected_share_is_safe_and_revocation_stops_next_target(
    db_session: Session, tree: Any, kind: str
) -> None:
    owner, child, sibling, outsider, user, resources = tree
    resource = resources[kind]
    result = crud.create(
        db_session,
        owner_account_id=owner.id,
        user_id=user.id,
        definition=ResourceShareDefinition(
            resource_type=kind,
            resource_id=resource.id,
            target_mode="selected",
            selected_account_ids=[child.id],
        ),
    )
    assert result["recipient_account_ids"] == [str(child.id)]
    public = crud.public_read(
        db_session, account_id=child.id, resource_type=kind, resource_id=resource.id
    )
    assert public is not None
    assert public.owner_name == "Parent" and public.is_shared and public.read_only
    if kind in {"ai_model", "managed_agent"}:
        from preloop.models.crud.budget_configuration import validate_budget_subject

        assert (
            validate_budget_subject(
                db_session,
                account_id=child.id,
                subject_type=kind,
                subject_id=resource.id,
            )
            is resource
        )
        with pytest.raises(ValueError, match="not found"):
            validate_budget_subject(
                db_session,
                account_id=sibling.id,
                subject_type=kind,
                subject_id=resource.id,
            )
    assert "synthetic-secret" not in public.model_dump_json()
    assert (
        not {
            "api_key",
            "auth_config",
            "url",
            "runtime_session_id",
            "credentials_secret_id",
        }
        & type(public).model_fields.keys()
    )
    for account in [sibling, outsider]:
        assert (
            crud.visible_resource(
                db_session,
                account_id=account.id,
                resource_type=kind,
                resource_id=resource.id,
            )
            is None
        )
    crud.revoke(
        db_session, owner_account_id=owner.id, user_id=user.id, share_id=result["id"]
    )
    assert crud.visible_ids(db_session, account_id=child.id, resource_type=kind) == []


def test_all_share_materializes_new_child_transactionally(
    db_session: Session, tree: Any
) -> None:
    owner, child, sibling, outsider, user, resources = tree
    model = resources["ai_model"]
    crud.create(
        db_session,
        owner_account_id=owner.id,
        user_id=user.id,
        definition=ResourceShareDefinition(
            resource_type="ai_model", resource_id=model.id, target_mode="all"
        ),
    )
    new_child = models.Account(organization_name="New child")
    place_under(new_child, owner)
    db_session.add(new_child)
    db_session.commit()
    assert crud.visible_ids(
        db_session, account_id=new_child.id, resource_type="ai_model"
    ) == [model.id]
    assert (
        crud.visible_ids(db_session, account_id=outsider.id, resource_type="ai_model")
        == []
    )


def test_cross_account_owner_and_target_substitution_rejected(
    db_session: Session, tree: Any
) -> None:
    owner, child, sibling, outsider, user, resources = tree
    with pytest.raises(ShareConflictError):
        crud.owned_account(db_session, account_id=owner.id, current_account_id=child.id)
    with pytest.raises(ShareConflictError):
        crud.create(
            db_session,
            owner_account_id=owner.id,
            user_id=user.id,
            definition=ResourceShareDefinition(
                resource_type="ai_model",
                resource_id=resources["ai_model"].id,
                target_mode="selected",
                selected_account_ids=[outsider.id],
            ),
        )


@pytest.mark.asyncio
async def test_shared_agent_command_has_consumer_history_and_live_runtime_binding(
    db_session: Session, tree: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock
    from preloop.services.agent_control_dispatch import dispatch_operator_message
    from preloop.models.crud import (
        crud_agent_control_command,
        crud_api_key,
        crud_runtime_session,
    )
    from preloop.api.auth.jwt import _managed_agent_for_api_key
    from preloop.api.endpoints import agent_control

    owner, child, sibling, outsider, user, resources = tree
    agent = resources["managed_agent"]
    owner_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=owner.id,
        session_source_type=agent.session_source_type,
        session_source_id=agent.session_source_id,
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
    )
    agent.runtime_session_id = owner_session.id
    child_user = models.User(
        account_id=child.id,
        username=str(uuid4()),
        email=f"{uuid4()}@example.com",
        hashed_password="synthetic",
    )
    db_session.add(child_user)
    db_session.flush()
    share = crud.create(
        db_session,
        owner_account_id=owner.id,
        user_id=user.id,
        definition=ResourceShareDefinition(
            resource_type="managed_agent",
            resource_id=agent.id,
            target_mode="selected",
            selected_account_ids=[child.id],
        ),
    )
    from preloop.models.crud import agent_control_connection as control_connection

    monkeypatch.setattr(control_connection, "release_read_transaction", lambda db: None)
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_control.agent_control_manager, "send_to_agent", deliver)
    result = await dispatch_operator_message(
        db_session,
        managed_agent=agent,
        text="Synthetic task",
        start_new_session=True,
        consuming_account_id=child.id,
        created_by_user_id=child_user.id,
    )
    record = crud_agent_control_command.get_for_consumer(
        db_session,
        account_id=child.id,
        command_id=result.command_id,
        managed_agent_id=agent.id,
    )
    assert record is not None and record.account_id == owner.id
    assert record.consuming_account_id == child.id
    session = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(child.id),
        runtime_session_id=str(result.history_session_id),
    )
    assert session is not None and record.runtime_session_id == session.id
    assert crud.owner_agent_sessions(
        db_session, owner_account_id=owner.id, agent_id=agent.id
    )[0]["id"] == str(session.id)
    with pytest.raises(ShareConflictError):
        crud.owner_agent_sessions(
            db_session, owner_account_id=sibling.id, agent_id=agent.id
        )
    public_envelope = agent_control._public_shared_command_envelope(result.envelope)
    assert "api_key" not in public_envelope.model_dump_json()
    assert "gateway" not in public_envelope.payload
    token = result.envelope.payload["metadata"]["gateway"]["api_key"]
    key = crud_api_key.get_by_key(db_session, key=token, account_id=str(child.id))
    assert key is not None and key.user_id == child_user.id
    assert _managed_agent_for_api_key(db_session, key) is agent
    crud.revoke(
        db_session, owner_account_id=owner.id, user_id=user.id, share_id=share["id"]
    )
    assert _managed_agent_for_api_key(db_session, key) is agent
    assert (
        crud.shared_agent_spend_owner(
            db_session, account_id=child.id, agent_id=agent.id
        )
        == owner.id
    )
    owner_budget = models.BudgetPolicy(
        account_id=owner.id,
        subject_type="managed_agent",
        subject_id=agent.id,
        period=models.BudgetPeriod.daily,
        hard_limit_usd=1.0,
    )
    db_session.add(owner_budget)
    db_session.flush()
    assert crud.shared_agent_budget_policies(
        db_session, account_id=child.id, agent_id=agent.id
    ) == [owner_budget]
    from preloop.services.subject_governance import set_subject_governance

    owner.meta_data = set_subject_governance(
        owner.meta_data or {},
        subject_type="managed_agents",
        subject_id=str(agent.id),
        config={"allowed_models": ["openai/example-model"]},
    )
    db_session.flush()
    assert crud.shared_agent_governance(
        db_session, account_id=child.id, agent_id=agent.id
    )["allowed_models"] == ["openai/example-model"]
    from contextlib import contextmanager
    from preloop.models.crud.access_rule import CRUDAccessRule

    @contextmanager
    def same_session() -> Any:
        yield db_session

    access = CRUDAccessRule()
    monkeypatch.setattr(access, "session", same_session)
    child.parent_account_id = None
    child.root_account_id = child.id
    child.hierarchy_path = [child.id]
    child.hierarchy_depth = 0
    db_session.flush()
    bundle = access.bundle(account_id=child.id)
    assert bundle["subjects"][("agent", str(agent.id))]["home_account"]["id"] == str(
        owner.id
    )
    assert bundle["key_agents"][str(key.id)] == str(agent.id)
    assert (
        crud_agent_control_command.get_for_consumer(
            db_session,
            account_id=child.id,
            command_id=result.command_id,
            managed_agent_id=agent.id,
        )
        is record
    )
    assert (
        crud_agent_control_command.get_for_consumer(
            db_session,
            account_id=sibling.id,
            command_id=result.command_id,
            managed_agent_id=agent.id,
        )
        is None
    )
    from preloop.services.agent_control_dispatch import AgentControlDispatchError

    with pytest.raises(AgentControlDispatchError, match="not found"):
        await dispatch_operator_message(
            db_session,
            managed_agent=agent,
            text="Next task",
            start_new_session=True,
            consuming_account_id=child.id,
            created_by_user_id=child_user.id,
        )
    assert deliver.call_count == 1


def test_owner_model_price_excludes_arbitrary_metadata(
    db_session: Session, tree: Any
) -> None:
    owner, child, sibling, outsider, user, resources = tree
    model = resources["ai_model"]
    model.meta_data = {
        "pricing": {"input_price_per_1k": 0.02, "api_key": "synthetic-secret"}
    }
    crud.create(
        db_session,
        owner_account_id=owner.id,
        user_id=user.id,
        definition=ResourceShareDefinition(
            resource_type="ai_model", resource_id=model.id, target_mode="all"
        ),
    )
    projection = crud.public_read(
        db_session, account_id=child.id, resource_type="ai_model", resource_id=model.id
    )
    assert projection.price == {"input_price_per_1k": 0.02}


def test_shared_gateway_alias_prices_and_usage_stay_with_consumer(
    db_session: Session, tree: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock
    from sqlalchemy import select
    from preloop.plugins.account_hooks import (
        register_visibility_provider,
        VisibilityProvider,
    )
    from preloop.services.model_gateway_auth import ModelGatewayAuthContext
    from preloop.services.model_gateway_errors import ModelGatewayAPIError
    from preloop.services.openai_gateway import OpenAIGatewayService
    from preloop.models.crud import crud_model_price_override

    owner, child, sibling, outsider, user, resources = tree
    model = resources["ai_model"]
    model.provider_name = "openai"
    model.meta_data = {
        "gateway": {
            "enabled": True,
            "model_alias": "openai/example-model",
            "provider_adapter": "preloop",
            "responses_api": "transcode",
        }
    }
    child_user = models.User(
        account_id=child.id,
        username=str(uuid4()),
        email=f"{uuid4()}@example.com",
        hashed_password="synthetic",
    )
    db_session.add(child_user)
    db_session.flush()

    class Receipts(VisibilityProvider):
        def extra_visible_ids(
            self, db: Any, account_id: Any, resource_type: str
        ) -> list[Any]:
            return crud.visible_ids(
                db, account_id=account_id, resource_type=resource_type
            )

    register_visibility_provider(Receipts())
    share = crud.create(
        db_session,
        owner_account_id=owner.id,
        user_id=user.id,
        definition=ResourceShareDefinition(
            resource_type="ai_model",
            resource_id=model.id,
            target_mode="selected",
            selected_account_ids=[child.id],
        ),
    )
    owner_price = crud_model_price_override.create(
        db_session,
        obj_in={
            "account_id": owner.id,
            "ai_model_id": model.id,
            "model_alias": "openai/example-model",
            "input_price_per_1k": 1.0,
            "output_price_per_1k": 2.0,
            "currency": "USD",
        },
    )
    crud_model_price_override.create(
        db_session,
        obj_in={
            "account_id": child.id,
            "ai_model_id": model.id,
            "model_alias": "openai/example-model",
            "input_price_per_1k": 999.0,
            "output_price_per_1k": 999.0,
            "currency": "USD",
        },
    )
    upstream = Mock()
    upstream.completion.return_value = {
        "id": "synthetic-completion",
        "created": 1710000000,
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 1000,
            "total_tokens": 2000,
        },
    }
    service = OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="synthetic", user=child_user),
        upstream_backend=upstream,
    )
    try:
        assert (
            service._resolve_requested_model("openai/example-model", provider="openai")
            is model
        )
        from preloop.services.model_gateway_budget import ModelGatewayBudgetService

        assert ModelGatewayBudgetService(
            db_session, service.auth_context
        )._pricing_override_for_request(model, {"model": "openai/example-model"})[
            "id"
        ] == str(owner_price.id)
        service.create_chat_completion(
            {
                "model": "openai/example-model",
                "messages": [{"role": "user", "content": "Synthetic task"}],
            }
        )
        usage = db_session.scalar(
            select(models.ApiUsage).where(
                models.ApiUsage.account_id == child.id,
                models.ApiUsage.ai_model_id == model.id,
            )
        )
        assert usage is not None and usage.estimated_cost == pytest.approx(3.0)
        assert upstream.completion.call_args.kwargs["api_key"] == "synthetic-secret"

        def chunks() -> Any:
            yield {
                "id": "synthetic-stream",
                "object": "chat.completion.chunk",
                "created": 1710000000,
                "model": "example-model",
                "choices": [
                    {"index": 0, "delta": {"content": "first"}, "finish_reason": None}
                ],
            }
            crud.revoke(
                db_session,
                owner_account_id=owner.id,
                user_id=user.id,
                share_id=share["id"],
            )
            yield {
                "id": "synthetic-stream",
                "object": "chat.completion.chunk",
                "created": 1710000000,
                "model": "example-model",
                "choices": [
                    {"index": 0, "delta": {"content": "last"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

        upstream.completion.return_value = chunks()
        stream = OpenAIGatewayService(
            db_session,
            ModelGatewayAuthContext(token="synthetic", user=child_user),
            upstream_backend=upstream,
        )
        events = list(
            stream.stream_chat_completion(
                {
                    "model": "openai/example-model",
                    "messages": [{"role": "user", "content": "Synthetic stream"}],
                    "stream": True,
                }
            )
        )
        stream.flush_deferred_stream_record()
        assert any("first" in event for event in events) and any(
            "last" in event for event in events
        )
        assert any("[DONE]" in event for event in events)
        fresh = OpenAIGatewayService(
            db_session, ModelGatewayAuthContext(token="synthetic", user=child_user)
        )
        with pytest.raises(
            ModelGatewayAPIError, match="model no longer shared with this account"
        ):
            fresh._resolve_requested_model("openai/example-model", provider="openai")
    finally:
        register_visibility_provider(None)


def test_shared_mcp_owner_approval_and_secret_projection(
    db_session: Session, tree: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services import dynamic_fastmcp
    from preloop.plugins.account_hooks import (
        register_visibility_provider,
        VisibilityProvider,
    )
    from preloop.models.schemas.mcp_tool import MCPToolResponse

    owner, child, sibling, outsider, user, resources = tree
    server = resources["mcp_server"]
    server.status = "active"
    tool = models.MCPTool(
        mcp_server_id=server.id,
        name="example_tool",
        description="Synthetic tool",
        input_schema={"type": "object"},
        discovered_at=datetime.now(timezone.utc).isoformat(),
    )
    db_session.add(tool)
    db_session.flush()
    share = crud.create(
        db_session,
        owner_account_id=owner.id,
        user_id=user.id,
        definition=ResourceShareDefinition(
            resource_type="mcp_server",
            resource_id=server.id,
            target_mode="selected",
            selected_account_ids=[child.id],
            require_approval=True,
        ),
    )

    class Receipts(VisibilityProvider):
        def extra_visible_ids(
            self, db: Any, account_id: Any, resource_type: str
        ) -> list[Any]:
            return crud.visible_ids(
                db, account_id=account_id, resource_type=resource_type
            )

    register_visibility_provider(Receipts())
    from preloop.models.crud import crud_mcp_tool

    assert crud_mcp_tool.get_by_visible_servers_for_account(
        db_session, account_id=str(child.id), server_ids=[server.id]
    ) == [tool]
    assert (
        crud_mcp_tool.get_by_visible_servers_for_account(
            db_session, account_id=str(sibling.id), server_ids=[server.id]
        )
        == []
    )
    monkeypatch.setattr(dynamic_fastmcp, "get_db", lambda: iter([db_session]))
    monkeypatch.setattr(db_session, "close", lambda: None)
    try:
        assert (
            dynamic_fastmcp._shared_tool_ceiling(str(child.id), tool.name, {}, None)
            == "require_approval"
        )
        assert dynamic_fastmcp._resolve_proxied_tool_server(
            db_session, str(child.id), tool.name
        ).auth_config == {"token": "synthetic-secret"}
        public = crud.public_read(
            db_session,
            account_id=child.id,
            resource_type="mcp_server",
            resource_id=server.id,
        )
        assert (
            "auth_config" not in public.model_dump()
            and "synthetic-secret" not in public.model_dump_json()
        )
        assert (
            not {"auth_config", "api_key", "credentials_secret_id", "token"}
            & MCPToolResponse.model_fields.keys()
        )
        crud.revoke(
            db_session, owner_account_id=owner.id, user_id=user.id, share_id=share["id"]
        )
        assert (
            dynamic_fastmcp._resolve_proxied_tool_server(
                db_session, str(child.id), tool.name
            )
            is None
        )
    finally:
        register_visibility_provider(None)
