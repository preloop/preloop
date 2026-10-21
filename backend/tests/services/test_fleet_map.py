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
        with (
            patch(f"{MODULE}.list_decommissioned_agents", return_value=[]),
            patch(
                f"{MODULE}.list_resources",
                side_effect=lambda db, source, **kw: {
                    "agents": crud.crud_managed_agent,
                    "models": crud.crud_ai_model,
                    "runners": crud.crud_flow_runner,
                    "users": crud.crud_user,
                }[source].get_multi(db, **kw),
            ),
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
                        kind="cursor",
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


def test_agents_carry_onboarded_or_inactive_state(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    now = datetime.now(UTC)
    crud.crud_managed_agent.get_multi.return_value = [
        agent("fresh", last_seen_at=now),
        agent("stale", last_seen_at=now - timedelta(days=30)),
        agent("paused", lifecycle_state="suspended", last_seen_at=now),
    ]
    retired = [agent(f"old-{i}", lifecycle_state="decommissioned") for i in range(4)]
    with (
        patch(f"{MODULE}.list_decommissioned_agents", return_value=retired),
        patch(
            f"{MODULE}.filter_viewable",
            side_effect=lambda db, user, kind, rows: [
                row for row in rows if row.id != "old-3"
            ],
        ),
    ):
        graph = build_fleet_map(MagicMock(), user=viewer())
    states = {n.id: n.agent_state for n in graph.nodes if n.type == "agent"}
    assert states == {
        "agent:fresh": "onboarded",
        "agent:stale": "inactive",
        "agent:paused": "inactive",
    }
    # A decommissioned agent hidden from the caller is not counted either.
    assert graph.decommissioned_agents == 3


def test_model_instances_collapse_to_one_canonical_model(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_managed_agent.get_multi.return_value = [agent("a"), agent("b")]
    crud.crud_ai_model.get_multi.return_value = [
        model("m1", model_identifier="anthropic/claude-opus-5-5"),
        model(
            "m2",
            model_identifier="claude-opus-5-5-20261001",
            credentials_secret_id="secret-b",
        ),
    ]
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = [
        binding(managed_agent_id="a", ai_model_id="m1"),
        binding(managed_agent_id="b", ai_model_id="m2"),
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    model_nodes = [n for n in graph.nodes if n.type == "model"]
    assert [(n.id, n.label) for n in model_nodes] == [
        ("model:claude-opus-5-5", "Claude Opus 5.5")
    ]
    uses = {(e.source, e.account_id) for e in graph.edges if e.kind == "uses_model"}
    assert uses == {
        ("agent:a", "provider_account:secret-a"),
        ("agent:b", "provider_account:secret-b"),
    }
    accounts = [n for n in graph.nodes if n.type == "provider_account"]
    assert {n.parent_id for n in accounts} == {"provider:openai"}
    # Blast radius from an account still reaches only its own agent.
    impact = {
        (e.impact_source, e.impact_target)
        for e in graph.edges
        if e.kind == "uses_account"
    }
    assert impact == {
        ("provider_account:secret-a", "agent:a"),
        ("provider_account:secret-b", "agent:b"),
    }


def test_provider_impact_goes_through_its_accounts_not_a_shared_model(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_managed_agent.get_multi.return_value = [agent("a"), agent("b")]
    crud.crud_ai_model.get_multi.return_value = [
        model("m1", model_identifier="claude-opus-5-5", provider_name="anthropic"),
        model(
            "m2",
            model_identifier="claude-opus-5-5",
            provider_name="bedrock",
            credentials_secret_id="secret-b",
        ),
    ]
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = [
        binding(managed_agent_id="a", ai_model_id="m1"),
        binding(managed_agent_id="b", ai_model_id="m2"),
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    assert not [e for e in graph.edges if e.kind == "provided_by"]
    reach = {"provider:anthropic"}
    for _ in range(4):
        reach |= {e.impact_target for e in graph.edges if e.impact_source in reach}
    assert "agent:a" in reach and "agent:b" not in reach


@pytest.mark.parametrize(
    ("identifier", "label"),
    [
        ("gpt-6.1-sol", "GPT 6.1 Sol"),
        ("claude-fable-5-1", "Claude Fable 5.1"),
        ("openrouter/deepseek-v4.1:latest", "Deepseek V4.1"),
        ("glm-5.3", "GLM 5.3"),
    ],
)
def test_canonical_model_labels(identifier: str, label: str) -> None:
    from preloop.services.fleet_map import canonical_model

    assert canonical_model(identifier)[1] == label


def test_discovered_duplicates_and_dismissed_are_not_repeated(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, manager = sources
    crud.crud_managed_agent.get_multi.return_value = [agent("agent-a")]
    manager.return_value.get_service.return_value = MagicMock()
    manager.return_value.get_service.return_value.read.return_value = dict(
        truncated=False,
        items=[
            dict(
                device_key="fp:example",
                hostname="Example laptop",
                runner_state="online",
                users=[],
                runners=[],
                findings=[],
                agents=[
                    dict(id="i1", kind="cursor", status="pending"),
                    dict(id="c1", kind="cursor", status="pending"),
                    dict(id="c2", kind="gemini", status="dismissed"),
                    dict(id="c6", kind="windsurf", status="ignored"),
                    dict(id="c3", kind="codex", status="pending"),
                    dict(
                        id="c4",
                        kind="codex",
                        status="onboarded",
                        managed_agent_id="agent-a",
                    ),
                    dict(
                        id="c5",
                        kind="devin",
                        status="onboarded",
                        managed_agent_id="decommissioned-agent",
                    ),
                ],
            )
        ],
    )
    graph = build_fleet_map(MagicMock(), user=viewer())
    agents = sorted(n.id for n in graph.nodes if n.type == "agent")
    assert agents == ["agent:agent-a", "agent:discovered:i1"]
    nodes = {n.id: n for n in graph.nodes}
    assert nodes["agent:discovered:i1"].agent_state == "discovered"


def test_agent_providers_follow_its_account_not_the_shared_model(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    crud.crud_managed_agent.get_multi.return_value = [agent("a"), agent("b")]
    crud.crud_ai_model.get_multi.return_value = [
        model("m1", model_identifier="claude-opus-5-5", provider_name="anthropic"),
        model(
            "m2",
            model_identifier="claude-opus-5-5",
            provider_name="bedrock",
            credentials_secret_id="secret-b",
        ),
    ]
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = [
        binding(managed_agent_id="a", ai_model_id="m1"),
        binding(managed_agent_id="b", ai_model_id="m2"),
    ]
    graph = build_fleet_map(MagicMock(), user=viewer())
    nodes = {n.id: n for n in graph.nodes}
    assert nodes["agent:a"].provider_ids == ["provider:anthropic"]
    assert nodes["agent:b"].provider_ids == ["provider:bedrock"]


def test_saturated_decommissioned_count_marks_the_map_partial(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    from preloop.services.fleet_map import SOURCE_LIMIT

    retired = [
        agent(f"old-{i}", lifecycle_state="decommissioned")
        for i in range(SOURCE_LIMIT + 1)
    ]
    with patch(f"{MODULE}.list_decommissioned_agents", return_value=retired):
        graph = build_fleet_map(MagicMock(), user=viewer())
    assert graph.decommissioned_agents == SOURCE_LIMIT
    assert graph.truncated


def _runnerless_device(*agent_ids: str) -> dict[str, Any]:
    return dict(
        device_key="fp:example",
        hostname=None,
        runner_state="none",
        users=[],
        runners=[],
        agents=[
            dict(
                id=f"candidate-{aid}",
                source="candidate",
                kind="codex",
                status="onboarded",
                managed_agent_id=aid,
            )
            for aid in agent_ids
        ],
        findings=[],
    )


def test_runnerless_device_is_named_by_its_agents_enrollment_hostname(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, manager = sources
    crud.crud_managed_agent.get_multi.return_value = [
        agent("agent-a", enrollment_hostname="example-laptop"),
        agent("agent-b", enrollment_hostname="example-laptop"),
    ]
    service = manager.return_value.get_service.return_value = MagicMock()
    service.read.return_value = dict(
        truncated=False, items=[_runnerless_device("agent-a", "agent-b")]
    )
    nodes = {n.id: n for n in build_fleet_map(MagicMock(), user=viewer()).nodes}
    device = nodes["device:fp:example"]
    assert device.label == "example-laptop"
    assert "unknown_device" not in device.gaps
    for aid in ("agent-a", "agent-b"):
        assert "unknown_device" not in nodes[f"agent:{aid}"].gaps


def test_conflicting_enrollment_hostnames_do_not_name_the_device(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, manager = sources
    crud.crud_managed_agent.get_multi.return_value = [
        agent("agent-a", enrollment_hostname="example-one"),
        agent("agent-b", enrollment_hostname="example-two"),
    ]
    service = manager.return_value.get_service.return_value = MagicMock()
    service.read.return_value = dict(
        truncated=False, items=[_runnerless_device("agent-a", "agent-b")]
    )
    nodes = {n.id: n for n in build_fleet_map(MagicMock(), user=viewer()).nodes}
    assert nodes["device:fp:example"].label == "Unknown device"
    assert "unknown_device" in nodes["device:fp:example"].gaps


def test_activity_window_keeps_only_active_dependencies(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, _ = sources
    now = datetime.now(UTC)
    crud.crud_user.get_multi.return_value = [
        Row(id="owner-a", username="jane", full_name="Jane Doe"),
        Row(id="owner-b", username="joe", full_name="Joe Doe"),
    ]
    crud.crud_managed_agent.get_multi.return_value = [
        agent("agent-a", owner_user_id="owner-a", last_seen_at=now),
        agent(
            "agent-b",
            owner_user_id="owner-b",
            last_seen_at=now - timedelta(days=40),
        ),
        agent("agent-c"),
    ]
    crud.crud_ai_model.get_multi.return_value = [
        model(),
        model("model-b", provider_name="mistral", credentials_secret_id="secret-b"),
    ]
    crud.crud_managed_agent_ai_model_binding.list_for_fleet_map.return_value = [
        binding(),
        binding(managed_agent_id="agent-b", ai_model_id="model-b"),
    ]
    everything = build_fleet_map(MagicMock(), user=viewer())
    assert "agent:agent-b" in {n.id for n in everything.nodes}
    assert everything.active_within_minutes is None

    # Configured on an active agent is not activity: no gateway row, no model.
    idle = build_fleet_map(MagicMock(), user=viewer(), active_within_minutes=43200)
    idle_ids = {n.id for n in idle.nodes}
    assert {"agent:agent-a", "user:owner-a"} <= idle_ids
    assert not [n for n in idle.nodes if n.type in {"model", "provider"}]

    usage = [
        Row(
            ai_model_id="model-a",
            principal_type=None,
            principal_id=None,
            flow_id=None,
            user_id=None,
            requests=1,
            last_at=(now - timedelta(minutes=5)).replace(tzinfo=None),
        )
    ]
    with patch("preloop.models.crud.fleet_activity.gateway_edges", return_value=usage):
        graph = build_fleet_map(
            MagicMock(), user=viewer(), active_within_minutes=60 * 24 * 30
        )
        padded = build_fleet_map(
            MagicMock(),
            user=viewer(),
            active_within_minutes=60 * 24 * 30,
            include_idle=True,
        )
    ids = {n.id for n in graph.nodes}
    assert graph.active_within_minutes == 43200
    assert {"agent:agent-a", "user:owner-a", "provider:openai"} <= ids
    assert not ids & {
        "agent:agent-b",
        "agent:agent-c",
        "user:owner-b",
        "provider:mistral",
    }
    assert [n for n in graph.nodes if n.type == "model"]
    assert not any(n.idle for n in graph.nodes)
    assert all(e.source in ids and e.target in ids for e in graph.edges)
    flagged = {n.id: n.idle for n in padded.nodes}
    assert flagged["provider:mistral"] is True and flagged["agent:agent-b"] is True
    assert flagged["provider:openai"] is False


def test_activity_window_reads_device_and_runner_evidence(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    from preloop.services.fleet_map import _seen_at

    crud, manager = sources
    now = datetime.now(UTC)
    crud.crud_managed_agent.get_multi.return_value = []
    crud.crud_flow_runner.get_multi.return_value = [
        Row(
            id=name,
            name=name,
            registered_by_user_id=None,
            last_heartbeat=seen,
            is_active=True,
            status="online",
        )
        for name, seen in (
            ("runner-new", now),
            ("runner-old", now - timedelta(days=60)),
        )
    ]
    manager.return_value.get_service.return_value = MagicMock()
    manager.return_value.get_service.return_value.read.return_value = dict(
        truncated=False,
        items=[
            dict(
                device_key=key,
                hostname=key,
                runner_state="online",
                users=[],
                runners=[],
                agents=[],
                findings=[],
                last_seen_at=seen,
            )
            for key, seen in (
                ("fp:new", (now - timedelta(hours=2)).isoformat()),
                ("fp:old", now - timedelta(days=60)),
            )
        ],
    )
    graph = build_fleet_map(MagicMock(), user=viewer(), active_within_minutes=60 * 24)
    ids = {n.id for n in graph.nodes}
    assert {"runner:runner-new", "device:fp:new"} <= ids
    assert not ids & {"runner:runner-old", "device:fp:old"}

    assert _seen_at(now.isoformat().replace("+00:00", "Z")) == now
    assert _seen_at("not a date") is None
    assert _seen_at(None) is None


def _laptop(
    key: str = "fp:laptop",
    hostname: str = "Dimitriss-MacBook-Pro.local",
    user_ids: tuple[str, ...] = ("owner-a",),
    agents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return dict(
        device_key=key,
        hostname=hostname,
        runner_state="online",
        users=[dict(id=uid, source="runner_registrant") for uid in user_ids],
        runners=[dict(id=f"runner-{key}", hostname=hostname, state="online")],
        agents=agents or [],
        findings=[],
    )


def _fleet(
    sources: tuple[MagicMock, MagicMock],
    agents: list[Row],
    devices: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    crud, manager = sources
    crud.crud_managed_agent.get_multi.return_value = agents
    service = manager.return_value.get_service.return_value = MagicMock()
    service.read.return_value = dict(truncated=False, items=devices)
    graph = build_fleet_map(MagicMock(), user=viewer())
    return {n.id: n for n in graph.nodes}, {e.id: e for e in graph.edges}


def _hosted(edges: dict[str, Any], device: str) -> set[str]:
    return {
        e.target
        for e in edges.values()
        if e.kind == "hosts" and e.source == f"device:{device}"
    }


def test_unlinked_agent_is_placed_on_the_device_its_hostname_names(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    cursor = agent(
        "agent-cursor",
        agent_kind="cursor",
        owner_user_id="owner-a",
        enrollment_hostname="dimitriss-macbook-pro",
    )
    inventory = dict(
        id="runner:cursor_cli",
        source="inventory",
        kind="cursor_cli",
        status="pending",
        managed_agent_id=None,
    )
    nodes, edges = _fleet(sources, [cursor], [_laptop(agents=[inventory])])
    assert "unknown_device" not in nodes["agent:agent-cursor"].gaps
    edge = edges["hosts:device:fp:laptop:agent:agent-cursor:observed"]
    assert edge.provenance == "observed"
    # The inventory entry for the same harness folds into the managed agent.
    assert _hosted(edges, "fp:laptop") == {"agent:agent-cursor"}
    assert not any(n.agent_state == "discovered" for n in nodes.values())


def test_fingerprint_linked_agent_folds_inventory_harness_of_its_kind(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    codex = agent("agent-codex", agent_kind="codex")
    entries = [
        dict(
            id="cand-1",
            source="candidate",
            kind="codex",
            status="onboarded",
            managed_agent_id="agent-codex",
        ),
        dict(
            id="runner:codex_cli",
            source="inventory",
            kind="codex_cli",
            status="pending",
            managed_agent_id=None,
        ),
    ]
    nodes, edges = _fleet(sources, [codex], [_laptop(agents=entries)])
    assert _hosted(edges, "fp:laptop") == {"agent:agent-codex"}


def test_two_devices_with_the_same_hostname_link_neither(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    cursor = agent("agent-cursor", enrollment_hostname="dimitriss-macbook-pro")
    nodes, edges = _fleet(
        sources,
        [cursor],
        [_laptop("fp:one"), _laptop("fp:two", hostname="dimitriss_macbook_pro")],
    )
    assert "unknown_device" in nodes["agent:agent-cursor"].gaps
    assert not _hosted(edges, "fp:one") | _hosted(edges, "fp:two")


def test_owner_mismatch_does_not_link(sources: tuple[MagicMock, MagicMock]) -> None:
    cursor = agent(
        "agent-cursor",
        owner_user_id="someone-else",
        enrollment_hostname="dimitriss-macbook-pro",
    )
    nodes, edges = _fleet(sources, [cursor], [_laptop()])
    assert "unknown_device" in nodes["agent:agent-cursor"].gaps
    assert not _hosted(edges, "fp:laptop")


def test_fingerprint_link_wins_over_hostname(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    codex = agent("agent-codex", enrollment_hostname="dimitriss-macbook-pro")
    linked = dict(
        id="cand-1",
        source="candidate",
        kind="codex",
        status="onboarded",
        managed_agent_id="agent-codex",
    )
    other = _laptop("fp:other", hostname="other", agents=[linked])
    nodes, edges = _fleet(sources, [codex], [_laptop(), other])
    assert _hosted(edges, "fp:other") == {"agent:agent-codex"}
    assert not _hosted(edges, "fp:laptop")


def test_ambiguous_inventory_entry_keeps_its_discovered_node(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    agents = [
        agent(f"agent-{n}", agent_kind="cursor", owner_user_id="owner-a")
        for n in ("one", "two")
    ]
    inventory = dict(
        id="runner:cursor_cli",
        source="inventory",
        kind="cursor_cli",
        status="pending",
        managed_agent_id=None,
    )
    nodes, edges = _fleet(sources, agents, [_laptop(agents=[inventory])])
    assert _hosted(edges, "fp:laptop") == {"agent:discovered:runner:cursor_cli"}
    for aid in ("agent:agent-one", "agent:agent-two"):
        assert "unknown_device" in nodes[aid].gaps


def test_partial_agent_window_places_nothing_by_hostname(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    from preloop.services.fleet_map import SOURCE_LIMIT

    agents = [agent("agent-x", enrollment_hostname="dimitriss-macbook-pro")]
    agents += [agent(f"agent-{n}", agent_kind="cursor") for n in range(SOURCE_LIMIT)]
    # The capped graph may drop the node, so check the resolver is never asked.
    with patch(f"{MODULE}.resolve_hostname_devices") as resolve:
        _fleet(sources, agents, [_laptop(user_ids=())])
    resolve.assert_not_called()


def test_truncated_device_projection_places_nothing_by_hostname(
    sources: tuple[MagicMock, MagicMock],
) -> None:
    crud, manager = sources
    crud.crud_managed_agent.get_multi.return_value = [
        agent("agent-x", enrollment_hostname="dimitriss-macbook-pro")
    ]
    service = manager.return_value.get_service.return_value = MagicMock()
    service.read.return_value = dict(truncated=True, items=[_laptop(user_ids=())])
    graph = build_fleet_map(MagicMock(), user=viewer())
    assert not any(e.kind == "hosts" for e in graph.edges)
