"""A flow's ``runner_pool`` must not reach another member's runner (#2176).

Pinning a runner (``agent_config.runner_id``) already needs the flow author
to own it, administer the account, or hold a ``flows`` grant. These tests
hold explicit pool routing (a pool naming a runner's id, name or label) to
the same rule at lease time.
"""

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from preloop.models.crud import crud_flow, crud_flow_execution, crud_flow_runner
from preloop.models.crud import owner_consent
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import ACTION_RUNNER_FLOW_USE, Decision
from preloop.services import runner_service
from preloop.services.runner_service import lease_job

OWNER = uuid4()
MEMBER = uuid4()
ADMIN = uuid4()
ACCOUNT = uuid4()


def _runner(registered_by: Optional[UUID] = OWNER) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        account_id=ACCOUNT,
        name="owner-laptop",
        registered_by_user_id=registered_by,
        flow_execution_consent="always",
        status="online",
        free_slots=1,
        capabilities={"docker_available": True},
        accept_untagged=False,
        paused=False,
    )


def _users() -> dict:
    return {
        uid: SimpleNamespace(id=uid, account_id=ACCOUNT, is_active=True)
        for uid in (OWNER, MEMBER, ADMIN)
    }


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Any:
    users = _users()
    state = SimpleNamespace(runner=_runner(), author=MEMBER, flow_exists=True)
    flow_id = uuid4()
    monkeypatch.setattr(
        crud_flow_execution, "admit_runtime_start", lambda *a, **k: True
    )
    monkeypatch.setattr(
        crud_flow_execution,
        "get",
        lambda *a, **k: SimpleNamespace(flow_id=flow_id),
    )
    monkeypatch.setattr(
        crud_flow,
        "get",
        lambda *a, **k: (
            SimpleNamespace(id=flow_id, created_by_user_id=state.author)
            if state.flow_exists
            else None
        ),
    )
    monkeypatch.setattr(
        runner_service.crud_user, "get", lambda db, id=None: users.get(id)
    )
    monkeypatch.setattr(
        runner_service,
        "_user_is_account_admin",
        lambda db, user: getattr(user, "id", None) == ADMIN,
    )
    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda *a, **k: [state.runner]
    )
    monkeypatch.setattr(owner_consent, "runner_for_execution", lambda *a, **k: None)
    monkeypatch.setattr(
        crud_flow_runner, "claim_free_slot", lambda db, runner_id: state.runner
    )
    monkeypatch.setattr(
        crud_flow_runner, "create_assignment", lambda *a, **k: SimpleNamespace()
    )
    monkeypatch.setattr(runner_service, "emit_runner_updated", lambda *a: None)
    monkeypatch.setattr(account_hooks, "_authorizer", None)
    return state


def _lease(state: Any, *, pool: Optional[str] = None, **kwargs: Any) -> Any:
    return lease_job(
        MagicMock(),
        account_id=ACCOUNT,
        pool=pool or state.runner.name,
        execution_id=uuid4(),
        payload={},
        **kwargs,
    )


def test_member_cannot_route_flow_to_another_members_runner_by_name(world) -> None:
    assert _lease(world) is None


def test_member_cannot_route_by_runner_id(world) -> None:
    assert _lease(world, pool=str(world.runner.id)) is None


def test_owner_routes_own_flow_by_pool(world) -> None:
    world.author = OWNER
    assert _lease(world) is world.runner


def test_account_admin_routes_by_pool(world) -> None:
    world.author = ADMIN
    assert _lease(world) is world.runner


def test_admin_registered_runner_stays_poolable_by_members(world) -> None:
    world.runner = _runner(registered_by=ADMIN)
    assert _lease(world) is world.runner


def test_ownerless_runner_stays_poolable(world) -> None:
    world.runner = _runner(registered_by=None)
    assert _lease(world) is world.runner


def test_authorless_legacy_flow_keeps_consent_gate(world) -> None:
    # Flows that predate authorship cannot be attributed; the runner's flow
    # consent (here ``always``) stays the gate, as before.
    world.author = None
    assert _lease(world) is world.runner


def test_missing_flow_is_refused_for_personal_runner(world) -> None:
    world.flow_exists = False
    assert _lease(world) is None


def test_inactive_author_is_refused(world, monkeypatch) -> None:
    users = _users()
    users[MEMBER].is_active = False
    monkeypatch.setattr(
        runner_service.crud_user, "get", lambda db, id=None: users.get(id)
    )
    world.author = OWNER  # owner path does not need the user row
    assert _lease(world) is world.runner
    world.author = MEMBER
    assert _lease(world) is None


def test_flows_grant_lets_member_route_by_pool(world, monkeypatch) -> None:
    calls = []

    def authorizer(ctx, action, resource):
        calls.append((action, dict(ctx.attributes), ctx.user))
        if action == ACTION_RUNNER_FLOW_USE:
            return Decision("allow", ("ee:runner_share:g1",))
        return None

    monkeypatch.setattr(account_hooks, "_authorizer", authorizer)
    assert _lease(world) is world.runner
    flow_use = [c for c in calls if c[0] == ACTION_RUNNER_FLOW_USE]
    assert flow_use, "pool routing must ask runner:flow_use"
    attrs = flow_use[0][1]
    assert attrs["operation"] == "lease"
    assert attrs["default_allowed"] is False
    assert flow_use[0][2].id == MEMBER


def test_authorizer_without_grant_keeps_member_out(world, monkeypatch) -> None:
    monkeypatch.setattr(account_hooks, "_authorizer", lambda *a: None)
    assert _lease(world) is None


def test_pinned_lease_is_not_rechecked_here(world) -> None:
    # ``agent_config.runner_id`` is checked when the flow is saved and by
    # ``runner:accept``; the pool gate only covers unpinned pool routing.
    assert _lease(world, required_runner_id=world.runner.id) is world.runner
