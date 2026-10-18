"""Fleet graph isolation, provenance, impact direction and bounded reads."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Row
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.models import models
from preloop.services.fleet_map import build_fleet_map

MODULE = "preloop.services.fleet_map"


def agent(identifier: str = "agent-a", **changes: object) -> Row:
    """Create a safe synthetic agent row."""
    return Row(
        **(
            dict(
                id=identifier,
                display_name=identifier,
                agent_kind="codex",
                owner_user_id=None,
                lifecycle_state="active",
            )
            | changes
        )
    )


def model(identifier: str = "model-a", **changes: object) -> Row:
    """Create a credential-bearing model without allowing secret resolution."""
    return Row(
        **(
            dict(
                id=identifier,
                account_id="account-a",
                name=identifier,
                provider_name="openai",
                credentials_secret_id="secret-a",
                api_key="never-serialize-this",
                meta_data={},
                uses_ambient_credentials=False,
                runner_id=None,
            )
            | changes
        )
    )


def binding(**changes: object) -> Row:
    """Create one active binding."""
    return Row(
        **(
            dict(
                managed_agent_id="agent-a",
                ai_model_id="model-a",
                status="gateway_ready",
                binding_type="configured",
                config_key="model.primary",
            )
            | changes
        )
    )


@pytest.fixture
def sources() -> Iterator[tuple[MagicMock, MagicMock]]:
    """Replace persistence and optional extensions, preserving service logic."""
    with (
        patch(f"{MODULE}.crud") as crud,
        patch(f"{MODULE}.get_plugin_manager") as manager,
        patch(
            f"{MODULE}.filter_viewable", side_effect=lambda db, user, kind, rows: rows
        ),
    ):
        crud.crud_account.get.return_value = None
        crud.crud_managed_agent_enrollment.list_latest_for_fleet_map.return_value = []
        crud.crud_managed_agent.get_multi.return_value = [agent()]
        crud.crud_ai_model.get_multi.return_value = [model()]
        crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = [
            binding()
        ]
        crud.crud_flow_runner.get_multi.return_value = []
        crud.crud_user.get_multi.return_value = []
        manager.return_value.get_service.return_value = None
        with patch(
            f"{MODULE}.list_resources",
            side_effect=lambda db, source, **kw: {
                "agents": crud.crud_managed_agent,
                "models": crud.crud_ai_model,
                "runners": crud.crud_flow_runner,
                "users": crud.crud_user,
            }[source].get_multi(db, **kw),
        ):
            yield crud, manager


def viewer() -> models.User:
    """Return an account-scoped administrator."""
    return models.User(id=uuid4(), account_id=uuid4(), is_superuser=True)


def test_account_grouping_and_provenance_are_explicit(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_ai_model.get_multi.return_value += [
        model("model-b"),
        model("model-c", credentials_secret_id="secret-b"),
    ]
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value += [
        binding(ai_model_id="model-b", binding_type="observed"),
        binding(ai_model_id="model-c", config_key="gateway.autoregister.example"),
    ]
    user = viewer()
    graph = build_fleet_map(MagicMock(), user=user)
    assert len([n for n in graph.nodes if n.type == "provider_account"]) == 2
    links = [e for e in graph.edges if e.kind == "uses_model"]
    assert {e.provenance for e in links} == {"configured", "observed"}
    assert all(e.impact_target == "agent:agent-a" for e in links)
    assert all(e.impact_source.startswith("model:") for e in links)
    assert "never-serialize-this" not in graph.model_dump_json()
    for resource in (
        crud.crud_managed_agent,
        crud.crud_ai_model,
        crud.crud_flow_runner,
        crud.crud_user,
    ):
        assert resource.get_multi.call_args.kwargs["account_id"] == str(user.account_id)
        resource.get_multi.assert_called_once()
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.assert_called_once()


def test_inactive_and_hidden_models_do_not_create_edges(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = [
        binding(status="inactive"),
        binding(ai_model_id="hidden"),
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    assert not [e for e in graph.edges if e.source.startswith("agent:")]
    assert "hidden" not in graph.model_dump_json()


def test_resource_visibility_is_applied_before_linking(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    with patch(f"{MODULE}.filter_viewable", return_value=[]):
        graph = build_fleet_map(MagicMock(), user=viewer())
    assert not graph.nodes and not graph.edges


def test_source_failure_is_visible_and_other_sources_survive(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    _, manager = sources
    manager.return_value.get_service.return_value = MagicMock()
    manager.return_value.get_service.return_value.read.side_effect = RuntimeError(
        "internal secret text"
    )
    graph = build_fleet_map(MagicMock(), user=viewer())
    assert graph.unavailable == ["devices"]
    assert graph.nodes
    assert "internal secret text" not in graph.model_dump_json()


def test_caps_have_clusters_and_no_dangling_edges(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_managed_agent.get_multi.return_value = [
        agent(f"agent-{i:03}") for i in range(60)
    ]
    graph = build_fleet_map(MagicMock(), user=viewer(), limit=50)
    assert graph.truncated and len(graph.nodes) == 50
    assert any(c.type == "agent" and c.count > 0 for c in graph.clusters)
    ids = {n.id for n in graph.nodes}
    assert all(e.source in ids and e.target in ids for e in graph.edges)


def test_denied_sections_are_not_read(sources: tuple[MagicMock, MagicMock]) -> None:
    crud, _ = sources
    user = viewer()
    user.is_superuser = False
    with patch(
        f"{MODULE}.user_holds_permission",
        side_effect=lambda db, user, permission: permission == "view_device_fleet",
    ):
        graph = build_fleet_map(MagicMock(), user=user)
    assert not graph.nodes
    crud.crud_managed_agent.get_multi.assert_not_called()
    crud.crud_ai_model.get_multi.assert_not_called()
    crud.crud_flow_runner.get_multi.assert_not_called()


def test_authorizer_can_deny_section_even_for_superuser(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    with patch(
        f"{MODULE}.authorize",
        side_effect=lambda ctx, action, resource: Row(
            allowed=action == "view_device_fleet"
        ),
    ):
        graph = build_fleet_map(MagicMock(), user=viewer())
    assert not graph.nodes
    crud.crud_ai_model.get_multi.assert_not_called()


def test_fleet_gate_is_enforced_without_ee(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    user = viewer()
    user.is_superuser = False
    with (
        patch(f"{MODULE}.user_holds_permission", return_value=False),
        pytest.raises(HTTPException) as error,
    ):
        build_fleet_map(MagicMock(), user=user)
    assert error.value.status_code == 403


def test_report_only_key_cannot_read_graph(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    user = viewer()
    user._auth_api_key = Row(
        scopes=["report_discovery"], context_data={"device_report": {}}
    )
    with pytest.raises(HTTPException) as error:
        build_fleet_map(MagicMock(), user=user)
    assert error.value.status_code == 403


def test_fleet_projection_has_gaps_and_owner_impact(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, manager = sources
    crud.crud_user.get_multi.return_value = [
        Row(id="owner-a", username="jane", full_name="Jane Doe")
    ]
    crud.crud_managed_agent.get_multi.return_value = [agent(owner_user_id="owner-a")]
    manager.return_value.get_service.return_value = MagicMock()
    manager.return_value.get_service.return_value.read.return_value = dict(
        truncated=False,
        items=[
            dict(
                device_key="fp:example",
                hostname="Example laptop",
                runner_state="offline",
                users=[dict(id="owner-a", label="Jane Doe")],
                runners=[
                    dict(
                        id="runner-a",
                        name="Example runner",
                        state="offline",
                        registered_by_user_id="owner-a",
                    )
                ],
                agents=[
                    dict(
                        id="candidate-a",
                        source="candidate",
                        kind="codex",
                        status="new",
                        managed_agent_id=None,
                    ),
                    dict(
                        id="agent-a",
                        source="managed_agent",
                        kind="codex",
                        status="active",
                        managed_agent_id="agent-a",
                    ),
                ],
                findings=[dict(state="open")],
            )
        ],
    )
    crud.crud_flow_runner.get_multi.return_value = [
        Row(
            id="runner-a",
            name="Example runner",
            status="offline",
            last_heartbeat=None,
            registered_by_user_id="owner-a",
        )
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    nodes = {n.id: n for n in graph.nodes}
    assert "unknown_device" not in nodes["agent:agent-a"].gaps
    assert "discovered_agent" in nodes["agent:discovered:candidate-a"].gaps
    assert "offline_runner" in nodes["runner:runner-a"].gaps
    assert nodes["device:fp:example"].finding_count == 1
    impact = {(e.impact_source, e.impact_target) for e in graph.edges}
    assert ("agent:agent-a", "device:fp:example") in impact
    assert ("device:fp:example", "user:owner-a") in impact


def test_unknown_binding_status_does_not_claim_gateway_bypass(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = [
        binding(status="unresolved_credentials")
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    assert all("not_via_gateway" not in node.gaps for node in graph.nodes)
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = [
        binding(status="direct")
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    assert any(
        "not_via_gateway" in node.gaps
        for node in graph.nodes
        if node.type == "provider_account"
    )
    assert any("not_via_gateway" in edge.gaps for edge in graph.edges)


def test_legacy_configuration_is_linked_without_resurrecting_inactive_binding(
    sources,
) -> None:
    crud, _ = sources
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = []
    crud.crud_ai_model.get_multi.return_value = [
        model(meta_data={"gateway": {"model_alias": "example-model"}})
    ]
    crud.crud_managed_agent_enrollment.list_latest_for_fleet_map.return_value = [
        Row(
            managed_agent_id="agent-a",
            enrollment_type="cli_managed_config",
            created_at=datetime.now(UTC),
            managed_config={"model": "example-model"},
        )
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    assert any(
        edge.source == "agent:agent-a" and edge.target == "model:model-a"
        for edge in graph.edges
    )
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = [
        binding(status="inactive")
    ]
    crud.crud_managed_agent_enrollment.list_latest_for_fleet_map.reset_mock()
    graph = build_fleet_map(MagicMock(), user=viewer())
    assert not any(edge.kind == "uses_model" for edge in graph.edges)
    crud.crud_managed_agent_enrollment.list_latest_for_fleet_map.assert_not_called()


def test_bounded_map_preserves_provider_context_and_owner_facets(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_user.get_multi.return_value = [
        Row(id="jane", full_name="Jane Doe", username="jane")
    ]
    crud.crud_managed_agent.get_multi.return_value = [agent(owner_user_id="jane")] + [
        agent(f"agent-{i}") for i in range(60)
    ]
    graph = build_fleet_map(MagicMock(), user=viewer(), limit=50)
    nodes = {node.id: node for node in graph.nodes}
    assert "provider:openai" in nodes
    assert nodes["provider:openai"].owner_ids == ["user:jane"]
    assert nodes["model:model-a"].owner_ids == ["user:jane"]
    assert nodes["user:jane"].provider_ids == ["provider:openai"]
    assert graph.truncated


def test_fleet_projection_does_not_restore_permission_hidden_agents(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, manager = sources
    crud.crud_managed_agent.get_multi.return_value = []
    manager.return_value.get_service.return_value = MagicMock()
    manager.return_value.get_service.return_value.read.return_value = {
        "truncated": False,
        "items": [
            {
                "device_key": "example",
                "hostname": "Example device",
                "runner_state": "offline",
                "users": [],
                "runners": [],
                "findings": [],
                "agents": [
                    {
                        "managed_agent_id": "hidden-agent",
                        "id": "candidate-hidden",
                        "kind": "codex",
                        "status": "active",
                    }
                ],
            }
        ],
    }
    graph = build_fleet_map(MagicMock(), user=viewer())
    assert "hidden-agent" not in graph.model_dump_json()
    assert "candidate-hidden" not in graph.model_dump_json()


def test_stale_online_runner_is_offline_without_device_extension(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_flow_runner.get_multi.return_value = [
        Row(
            id="stale",
            name="Stale runner",
            registered_by_user_id=None,
            status="online",
            last_heartbeat=datetime.now(UTC) - timedelta(hours=1),
        )
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    node = next(node for node in graph.nodes if node.type == "runner")
    assert node.status == "offline" and node.gaps == ["offline_runner"]


def test_hidden_duplicate_alias_does_not_make_legacy_selection_unique(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = []
    crud.crud_ai_model.get_multi.return_value = [
        model("visible", meta_data={"gateway": {"model_alias": "shared"}}),
        model("hidden", meta_data={"gateway": {"model_alias": "shared"}}),
    ]
    crud.crud_managed_agent_enrollment.list_latest_for_fleet_map.return_value = [
        Row(
            managed_agent_id="agent-a",
            enrollment_type="cli_managed_config",
            created_at=datetime.now(UTC),
            managed_config={"model": "shared"},
        )
    ]
    with patch(
        f"{MODULE}.filter_viewable",
        side_effect=lambda db, user, kind, rows: [r for r in rows if r.id != "hidden"],
    ):
        graph = build_fleet_map(MagicMock(), user=viewer())
    assert not any(edge.kind == "uses_model" for edge in graph.edges)
    assert "hidden" not in graph.model_dump_json()


def test_partial_model_catalog_does_not_claim_unique_legacy_alias(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = []
    crud.crud_ai_model.get_multi.return_value = [
        model("visible", meta_data={"gateway": {"model_alias": "shared"}}),
        model("omitted", meta_data={"gateway": {"model_alias": "shared"}}),
    ]
    crud.crud_managed_agent_enrollment.list_latest_for_fleet_map.return_value = [
        Row(
            managed_agent_id="agent-a",
            enrollment_type="cli_managed_config",
            created_at=datetime.now(UTC),
            managed_config={"model": "shared"},
        )
    ]
    with patch(f"{MODULE}.SOURCE_LIMIT", 1):
        graph = build_fleet_map(MagicMock(), user=viewer())
    assert graph.truncated
    assert not any(edge.kind == "uses_model" for edge in graph.edges)


def test_legacy_fallback_uses_latest_enrollment_when_no_preferred_type_exists(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = []
    crud.crud_ai_model.get_multi.return_value = [
        model("older", meta_data={"gateway": {"model_alias": "older"}}),
        model("latest", meta_data={"gateway": {"model_alias": "latest"}}),
    ]
    now = datetime.now(UTC)
    crud.crud_managed_agent_enrollment.list_latest_for_fleet_map.return_value = [
        Row(
            managed_agent_id="agent-a",
            enrollment_type="a_legacy",
            created_at=now - timedelta(days=1),
            managed_config={"model": "older"},
        ),
        Row(
            managed_agent_id="agent-a",
            enrollment_type="z_legacy",
            created_at=now,
            managed_config={"model": "latest"},
        ),
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    assert [edge.target for edge in graph.edges if edge.kind == "uses_model"] == [
        "model:latest"
    ]


def test_built_in_binding_preserves_impact_without_operator_credentials(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    from preloop.plugins.account_hooks import (
        ALLOW,
        ACTION_RESOURCE_VIEW,
        VISIBLE_AI_MODEL,
        AuthorizationContext,
        Decision,
        filter_viewable,
        get_authorizer,
        register_authorizer,
    )

    crud, _ = sources
    crud.crud_ai_model.get_multi.return_value = [model(account_id=None), model("owned")]
    checked: list[str] = []

    def deny_model_rows(
        ctx: AuthorizationContext, action: str, resource: Any = None
    ) -> Decision:
        if (
            action == ACTION_RESOURCE_VIEW
            and ctx.attributes.get("resource_type") == VISIBLE_AI_MODEL
        ):
            checked.append(resource.id)
            return Decision("deny")
        return ALLOW

    previous = get_authorizer()
    register_authorizer(deny_model_rows)
    try:
        with (
            patch(f"{MODULE}.built_in_models_enabled", return_value=True),
            patch(f"{MODULE}.filter_viewable", filter_viewable),
        ):
            graph = build_fleet_map(MagicMock(), user=viewer())
    finally:
        register_authorizer(previous)
    assert crud.crud_ai_model.get_multi.call_args.kwargs["include_built_in"] is True
    assert any(
        e.kind == "uses_model" and e.target == "model:model-a" for e in graph.edges
    )
    assert checked == ["owned"]
    assert not any(n.id == "model:owned" for n in graph.nodes)
    assert not any(n.type == "provider_account" for n in graph.nodes)
    assert "secret-a" not in graph.model_dump_json()
    assert not graph.truncated


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        ({"sleep_signaled_at": datetime.now(UTC)}, "asleep"),
        ({"free_slots": 0}, "busy"),
        ({"os_idle_seconds": 600}, "idle"),
    ],
)
def test_runner_presence_matches_console(
    sources: tuple[MagicMock, MagicMock], signals: dict, expected: str
) -> None:
    crud, _ = sources
    crud.crud_flow_runner.get_multi.return_value = [
        Row(
            id="runner-a",
            name="Example runner",
            registered_by_user_id=None,
            status="online",
            last_heartbeat=datetime.now(UTC),
            **signals,
        )
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    runner = next(n for n in graph.nodes if n.type == "runner")
    assert runner.status == expected
    assert not runner.gaps


def test_legacy_alias_metadata_is_read_once_per_model(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    from preloop.api.endpoints.account import _ai_model_meta_lookup

    crud, _ = sources
    size = 40
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = []
    crud.crud_managed_agent.get_multi.return_value = [
        agent(f"agent-{i}") for i in range(size)
    ]
    crud.crud_ai_model.get_multi.return_value = [
        model(
            f"model-{i}",
            meta_data={
                "gateway": {"model_alias": "shared"},
                "managed_agent_id": f"agent-{i}",
            },
        )
        for i in range(size)
    ]
    crud.crud_managed_agent_enrollment.list_latest_for_fleet_map.return_value = [
        Row(
            managed_agent_id=f"agent-{i}",
            enrollment_type="cli_managed_config",
            created_at=datetime.now(UTC),
            managed_config={"model": "shared"},
        )
        for i in range(size)
    ]
    with patch(
        "preloop.api.endpoints.account._ai_model_meta_lookup",
        wraps=_ai_model_meta_lookup,
    ) as lookup:
        graph = build_fleet_map(MagicMock(), user=viewer())
    assert {(e.source, e.target) for e in graph.edges if e.kind == "uses_model"} == {
        (f"agent:agent-{i}", f"model:model-{i}") for i in range(size)
    }
    assert lookup.call_count <= size * 2


def test_runner_model_edge_uses_server_impact_direction(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_ai_model.get_multi.return_value = [model(runner_id="runner-a")]
    crud.crud_flow_runner.get_multi.return_value = [
        Row(
            id="runner-a",
            name="Example runner",
            registered_by_user_id=None,
            status="online",
            last_heartbeat=datetime.now(UTC),
        )
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    link = next(e for e in graph.edges if e.kind == "runs_on")
    assert (link.source, link.target) == ("model:model-a", "runner:runner-a")
    assert (link.impact_source, link.impact_target) == (link.target, link.source)
