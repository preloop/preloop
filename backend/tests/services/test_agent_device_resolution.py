"""Hostname fallback that places unlinked managed agents on fleet devices."""

from types import SimpleNamespace as Row
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.services.agent_device_links import agent_device_links
from preloop.services.agent_device_resolution import (
    agent_kind_for_harness,
    normalize_hostname,
    resolve_hostname_devices,
)


@pytest.mark.parametrize(
    "raw",
    [
        "Dimitriss-MacBook-Pro.local",
        "dimitriss-macbook-pro",
        "dimitriss_macbook_pro",
        "Dimitriss MacBook Pro",
        "dimitriss-macbook-pro.corp.example.com",
    ],
)
def test_hostnames_normalize_alike(raw: str) -> None:
    assert normalize_hostname(raw) == "dimitriss-macbook-pro"


@pytest.mark.parametrize("raw", [None, "", "  ", ".local", 3])
def test_unusable_hostnames_normalize_to_none(raw: Any) -> None:
    assert normalize_hostname(raw) is None


def test_harness_ids_map_to_agent_kinds() -> None:
    assert agent_kind_for_harness("codex_cli") == "codex"
    assert agent_kind_for_harness("vscode_copilot") == "vscode"
    assert agent_kind_for_harness("claude_code") == "claude_code"


def _device(key: str, hostname: str | None, users: tuple[str, ...] = ()) -> dict:
    return dict(
        device_key=key,
        hostname=hostname,
        users=[dict(id=u) for u in users],
        runners=[],
        agents=[],
    )


def _agent(**changes: Any) -> Row:
    return Row(
        **(
            dict(
                id="agent-a",
                owner_user_id="owner-a",
                enrollment_hostname="dimitriss-macbook-pro",
            )
            | changes
        )
    )


def test_founder_laptop_example_matches() -> None:
    devices = [_device("fp:17013ee1dc40", "Dimitriss-MacBook-Pro.local", ("owner-a",))]
    assert resolve_hostname_devices(devices, [_agent()]) == {
        "agent-a": "fp:17013ee1dc40"
    }


def test_ambiguous_hostname_links_nothing() -> None:
    devices = [
        _device("fp:one", "Dimitriss-MacBook-Pro.local"),
        _device("fp:two", "dimitriss-macbook-pro"),
    ]
    assert resolve_hostname_devices(devices, [_agent()]) == {}


def test_owner_mismatch_links_nothing() -> None:
    devices = [_device("fp:one", "Dimitriss-MacBook-Pro.local", ("someone-else",))]
    assert resolve_hostname_devices(devices, [_agent()]) == {}


def test_unknown_owner_or_users_still_link() -> None:
    devices = [_device("fp:one", "Dimitriss-MacBook-Pro.local")]
    assert resolve_hostname_devices(devices, [_agent()]) == {"agent-a": "fp:one"}
    devices = [_device("fp:one", "Dimitriss-MacBook-Pro.local", ("owner-a",))]
    assert resolve_hostname_devices(devices, [_agent(owner_user_id=None)]) == {
        "agent-a": "fp:one"
    }


def test_already_linked_agent_is_left_alone() -> None:
    linked = _device("fp:other", "other")
    linked["agents"] = [dict(managed_agent_id="agent-a")]
    devices = [_device("fp:one", "Dimitriss-MacBook-Pro.local"), linked]
    assert resolve_hostname_devices(devices, [_agent()]) == {}


def _agent_page(agents: list[Row], device: dict, *, allowed: bool = True) -> list:
    user = models.User(id=uuid4(), account_id=uuid4(), is_superuser=True)
    service = MagicMock()
    service.devices_for_agent.return_value = []
    service.read.return_value = dict(truncated=False, items=[device])
    module = "preloop.services.agent_device_links"
    with (
        patch(f"{module}.get_plugin_manager") as manager,
        patch(f"{module}.crud"),
        patch(f"{module}.list_resources", return_value=agents),
        patch(f"{module}.filter_viewable", side_effect=lambda db, u, k, rows: rows),
        patch("preloop.services.fleet_map._allowed", return_value=allowed),
    ):
        manager.return_value.get_service.return_value = service
        links = agent_device_links(MagicMock(), user=user, agent_id="agent-a")
        service.read.return_value = dict(truncated=True, items=[device])
        partial = agent_device_links(MagicMock(), user=user, agent_id="agent-a")
    assert partial == []
    return [link.device_key for link in links]


def _laptop() -> dict:
    device = _device("fp:laptop", "Example-Laptop.local", ("owner-a",))
    return device | dict(os="darwin", runner_state="online", last_seen_at=None)


def test_agent_page_uses_the_same_resolution() -> None:
    """The agent's Device strip names the device the fleet map places it on."""
    agent = _agent(enrollment_hostname="example-laptop")
    assert _agent_page([agent], _laptop()) == ["fp:laptop"]
    assert _agent_page([agent], _laptop(), allowed=False) == []


def test_agent_page_sees_sibling_ambiguity_like_the_map() -> None:
    device = _laptop()
    device["agents"] = [
        dict(
            id="r:cursor_cli",
            kind="cursor_cli",
            status="pending",
            managed_agent_id=None,
        )
    ]
    one = _agent(agent_kind="cursor", enrollment_hostname=None)
    two = _agent(id="agent-b", agent_kind="cursor", enrollment_hostname=None)
    assert _agent_page([one], device) == ["fp:laptop"]
    assert _agent_page([one, two], device) == []
    assert resolve_hostname_devices([device], [one, two]) == {}


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("Codex CLI", "codex"),
        ("GitHub Copilot CLI", "copilot_cli"),
        ("VS Code Copilot", "vscode"),
        ("Cursor CLI", "cursor"),
        ("devin_cli", "devin"),
        ("Claude Desktop", "claude_desktop"),
    ],
)
def test_display_names_map_to_agent_kinds(raw: str, kind: str) -> None:
    assert agent_kind_for_harness(raw) == kind


def _inventory_device(*kinds: str, hostname: str | None = "Laptop.local") -> dict:
    device = _device("fp:laptop", hostname, ("owner-a",))
    device["agents"] = [
        dict(id=f"runner-1:{k}", kind=k, status="pending", managed_agent_id=None)
        for k in kinds
    ]
    return device


def test_inventory_entry_places_the_one_managed_agent_of_its_kind() -> None:
    cursor = _agent(id="agent-cursor", agent_kind="cursor", enrollment_hostname=None)
    other = _agent(id="agent-codex", agent_kind="codex", enrollment_hostname=None)
    devices = [_inventory_device("cursor_cli")]
    assert resolve_hostname_devices(devices, [cursor, other]) == {
        "agent-cursor": "fp:laptop"
    }


def test_inventory_match_requires_the_same_owner() -> None:
    cursor = _agent(
        id="agent-cursor",
        agent_kind="cursor",
        owner_user_id="x",
        enrollment_hostname=None,
    )
    assert resolve_hostname_devices([_inventory_device("cursor_cli")], [cursor]) == {}


def test_inventory_match_rejects_an_inconsistent_enrollment_hostname() -> None:
    cursor = _agent(id="agent-cursor", agent_kind="cursor", enrollment_hostname="desk")
    assert resolve_hostname_devices([_inventory_device("cursor_cli")], [cursor]) == {}


def test_two_plausible_agents_of_one_kind_are_not_folded() -> None:
    one = _agent(id="agent-1", agent_kind="cursor", enrollment_hostname="laptop")
    two = _agent(id="agent-2", agent_kind="cursor", enrollment_hostname=None)
    assert resolve_hostname_devices([_inventory_device("cursor_cli")], [one, two]) == {}


def test_agent_page_skips_fallback_on_a_partial_agent_window() -> None:
    from preloop.services.fleet_map import SOURCE_LIMIT

    agents = [
        _agent(id=f"agent-{n}", enrollment_hostname=None) for n in range(SOURCE_LIMIT)
    ]
    agents.append(_agent(enrollment_hostname="example-laptop"))
    assert _agent_page(agents, _laptop()) == []


def test_agent_page_skips_fallback_past_the_device_window() -> None:
    from preloop.services.fleet_map import SOURCE_LIMIT

    user = models.User(id=uuid4(), account_id=uuid4(), is_superuser=True)
    service = MagicMock()
    service.devices_for_agent.return_value = []
    extra = [_device(f"fp:{n}", f"other-{n}") for n in range(SOURCE_LIMIT)]
    service.read.return_value = dict(truncated=False, items=[_laptop(), *extra])
    module = "preloop.services.agent_device_links"
    with (
        patch(f"{module}.get_plugin_manager") as manager,
        patch(f"{module}.crud"),
        patch(
            f"{module}.list_resources",
            return_value=[_agent(enrollment_hostname="example-laptop")],
        ),
        patch(f"{module}.filter_viewable", side_effect=lambda db, u, k, rows: rows),
        patch("preloop.services.fleet_map._allowed", return_value=True),
    ):
        manager.return_value.get_service.return_value = service
        assert agent_device_links(MagicMock(), user=user, agent_id="agent-a") == []
