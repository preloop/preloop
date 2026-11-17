"""Configured consumers do not depend on traffic and respect visibility."""

from types import SimpleNamespace as Row
from unittest.mock import MagicMock, patch
from uuid import uuid4


from preloop.models import models
from preloop.services.ai_model_usage_links import configured_model_consumers

MODULE = "preloop.services.ai_model_usage_links"


def one_page(agents: list) -> object:
    """Serve ``agents`` as the only batch of every keyset scan."""

    def _batch(*_: object, after_id: object = None, **__: object) -> list:
        return [] if after_id is not None else list(agents)

    return _batch


def binding(model_id: str, **changes: object) -> Row:
    """Return a synthetic model slot."""
    return Row(
        **(
            dict(
                ai_model_id=model_id,
                status="gateway_ready",
                binding_type="configured",
                config_key="model.primary",
            )
            | changes
        )
    )


def test_explicit_config_deduplicates_and_excludes_observed_and_inactive() -> None:
    model_id = str(uuid4())
    user = models.User(account_id=uuid4(), is_superuser=True)
    agents = [
        Row(id=str(uuid4()), display_name=f"Agent {i}", agent_kind="codex")
        for i in range(4)
    ]
    flow = Row(id=uuid4(), name="Example flow", is_enabled=False)
    with patch(f"{MODULE}.crud") as crud:
        crud.crud_managed_agent.list_batch_after.side_effect = one_page(agents)
        crud.crud_managed_agent_ai_model_binding.list_for_agents.return_value = {
            agents[0].id: [binding(model_id), binding(model_id)],
            agents[1].id: [binding(model_id, status="inactive")],
            agents[2].id: [
                binding(model_id, config_key="gateway.autoregister.example")
            ],
            agents[3].id: [binding(model_id, binding_type="observed")],
        }
        crud.crud_flow.list_configured_for_model.return_value = [flow]
        result = configured_model_consumers(MagicMock(), user=user, model_id=model_id)
        assert [item.id for item in result.agents] == [agents[0].id]
        assert [item.id for item in result.flows] == [str(flow.id)]
        assert crud.crud_managed_agent.list_batch_after.call_args.kwargs[
            "account_id"
        ] == str(user.account_id)
        assert crud.crud_flow.list_configured_for_model.call_args.kwargs[
            "account_id"
        ] == str(user.account_id)
        assert (
            crud.crud_flow.list_configured_for_model.call_args.kwargs["model_id"]
            == model_id
        )
        crud.crud_managed_agent_enrollment.list_latest_by_agents.assert_not_called()


def test_legacy_enrollment_config_is_used_without_observed_alias_fallback() -> None:
    model_id = str(uuid4())
    user = models.User(account_id=uuid4(), is_superuser=True)
    agent = Row(id=str(uuid4()), display_name="Example agent", agent_kind="codex")
    model = Row(
        id=model_id,
        meta_data={"gateway": {"model_alias": "example-model"}},
        name="Example",
        provider_name="openai",
        model_identifier="example-model",
    )
    with patch(f"{MODULE}.crud") as crud:
        crud.crud_managed_agent.list_batch_after.side_effect = one_page([agent])
        crud.crud_managed_agent_ai_model_binding.list_for_agents.return_value = {}
        crud.crud_managed_agent_enrollment.list_latest_by_agents.return_value = {
            agent.id: {"cli_managed_config": Row()}
        }
        crud.crud_managed_agent_enrollment._to_summary.return_value = {
            "managed_config": {"model": "example-model"}
        }
        crud.crud_ai_model.get_by_account.return_value = [model]
        crud.crud_flow.list_configured_for_model.return_value = []
        result = configured_model_consumers(MagicMock(), user=user, model_id=model_id)
        assert [item.id for item in result.agents] == [agent.id]
        crud.crud_managed_agent_enrollment._to_summary.return_value = {
            "managed_config": {},
            "latest_model_alias": "example-model",
        }
        assert (
            configured_model_consumers(MagicMock(), user=user, model_id=model_id).agents
            == []
        )

        # Historical model ownership is not proof of the current selection.
        model.meta_data["managed_agent_id"] = agent.id
        crud.crud_managed_agent_enrollment._to_summary.return_value = {
            "managed_config": {"model": "different-model"}
        }
        assert (
            configured_model_consumers(MagicMock(), user=user, model_id=model_id).agents
            == []
        )


def test_hidden_resources_are_omitted_and_truncation_is_reported() -> None:
    user = models.User(account_id=uuid4(), is_superuser=True)
    with (
        patch(f"{MODULE}.crud") as crud,
        patch(f"{MODULE}.filter_viewable", return_value=[]),
        patch(f"{MODULE}.MAX_CONSUMERS", 1),
    ):
        crud.crud_managed_agent.list_batch_after.side_effect = one_page(
            [Row(id=uuid4()), Row(id=uuid4())]
        )
        crud.crud_managed_agent_ai_model_binding.list_for_agents.return_value = {}
        crud.crud_flow.list_configured_for_model.return_value = [Row(), Row()]
        result = configured_model_consumers(
            MagicMock(), user=user, model_id=str(uuid4())
        )
        assert result.truncated
        assert result.agents == result.flows == []


def test_consumer_permissions_are_checked_before_reads() -> None:
    user = models.User(account_id=uuid4(), is_superuser=False)
    with (
        patch(f"{MODULE}.crud") as crud,
        patch(f"{MODULE}.user_holds_permission", return_value=False),
    ):
        result = configured_model_consumers(
            MagicMock(), user=user, model_id=str(uuid4())
        )
        assert result.agents == result.flows == []
        crud.crud_managed_agent.list_batch_after.assert_not_called()
        crud.crud_flow.list_configured_for_model.assert_not_called()


def test_account_authorizer_can_deny_a_consumer_kind_even_for_superuser() -> None:
    user = models.User(account_id=uuid4(), is_superuser=True)
    with (
        patch(f"{MODULE}.crud") as crud,
        patch(f"{MODULE}.authorize", return_value=Row(allowed=False)),
    ):
        result = configured_model_consumers(
            MagicMock(), user=user, model_id=str(uuid4())
        )
        assert result.agents == result.flows == []
        crud.crud_managed_agent.list_batch_after.assert_not_called()
        crud.crud_flow.list_configured_for_model.assert_not_called()
