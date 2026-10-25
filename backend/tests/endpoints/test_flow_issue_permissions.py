"""Flow and issue writes and their declared permissions in OSS builds.

The core edition has no access controls, so a member without any grant reaches
the operation. With the RBAC plugin installed but its checks disabled, the role
fallback still stops a missing grant before any work.
"""

import asyncio
import importlib
import inspect
from pkgutil import resolve_name
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.models import models
from preloop.utils import permissions


# Each boundary is the first resource lookup, connection, or service operation.
ROUTES = [
    ("flows", "create_flow", ("create_flows",), "crud_flow.get_by_name_and_account"),
    (
        "flows",
        "run_preset",
        ("execute_flows",),
        "preloop.services.preset_runner.run_preset_on_target",
    ),
    (
        "flows",
        "adopt_execution_continuation",
        ("execute_flows",),
        "crud_flow_feedback.release_read",
    ),
    (
        "flows",
        "send_execution_command",
        ("execute_flows",),
        "preloop.sync.services.event_bus.get_nats_client",
    ),
    ("flows", "trigger_flow_execution", ("execute_flows",), "crud_flow.get"),
    ("flows", "retry_flow_execution", ("execute_flows",), "crud_flow_execution.get"),
    ("flows", "update_flow", ("edit_flows",), "crud_flow.get"),
    ("flows", "delete_flow", ("delete_flows",), "crud_flow.get"),
    ("flows", "apply_preset_update", ("edit_flows",), "crud_flow.get"),
    ("flows", "dismiss_preset_update", ("edit_flows",), "crud_flow.get"),
    ("comments", "add_issue_comment", ("edit_issues",), "get_tracker_client"),
    (
        "issues",
        "create_issue",
        ("create_issues",),
        "preloop.models.crud.crud_organization.get",
    ),
    ("issues", "update_issue", ("edit_issues",), "crud_tracker.get_for_account"),
    ("issue_lifecycle", "refine_issue", ("edit_issues",), "run_lifecycle_endpoint"),
    ("issue_lifecycle", "ready_issue", ("edit_issues",), "run_lifecycle_endpoint"),
    (
        "issue_lifecycle",
        "reconcile_pickup",
        ("edit_issues", "execute_flows"),
        "run_lifecycle_endpoint",
    ),
    (
        "issue_lifecycle",
        "reconcile_audit",
        ("execute_flows",),
        "run_lifecycle_endpoint",
    ),
    (
        "issue_lifecycle",
        "verify_deployment",
        ("execute_flows",),
        "run_lifecycle_endpoint",
    ),
    (
        "security_maintenance",
        "create_release",
        ("edit_flows",),
        "run_lifecycle_endpoint",
    ),
    (
        "security_maintenance",
        "update_release",
        ("edit_flows",),
        "run_lifecycle_endpoint",
    ),
    (
        "security_maintenance",
        "schedule_baseline_audit",
        ("edit_flows",),
        "run_lifecycle_endpoint",
    ),
    (
        "security_maintenance",
        "accept_baseline",
        ("edit_flows",),
        "run_lifecycle_endpoint",
    ),
    ("security_maintenance", "ingest_scan", ("edit_flows",), "run_lifecycle_endpoint"),
    (
        "security_maintenance",
        "approve_item",
        ("decide_approvals",),
        "run_lifecycle_endpoint",
    ),
    (
        "security_maintenance",
        "deny_item",
        ("decide_approvals",),
        "run_lifecycle_endpoint",
    ),
    (
        "security_maintenance",
        "escalate_item",
        ("decide_approvals",),
        "run_lifecycle_endpoint",
    ),
    ("security_maintenance", "resume_item", ("edit_flows",), "run_lifecycle_endpoint"),
    (
        "security_maintenance",
        "submit_rebuilt_inputs",
        ("edit_flows",),
        "run_lifecycle_endpoint",
    ),
]


def _install_plugin_with_rbac_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install an RBAC plugin stand-in with its checks disabled.

    That is the only configuration in which the in-handler role fallback runs;
    the core edition admits every member.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    from preloop.config import settings

    monkeypatch.setattr(
        permissions, "_plugin_require_permission", lambda name: lambda func: func
    )
    monkeypatch.setattr(settings, "disable_rbac", True)


class OperationReached(BaseException):
    """Stop an allowed call at its first downstream boundary."""


@pytest.mark.parametrize(
    "module_name,handler,required,boundary",
    ROUTES,
    ids=[f"{module}.{handler}" for module, handler, _, _ in ROUTES],
)
@pytest.mark.parametrize("actor_kind", ["non_holder", "denied", "primary", "holder"])
@pytest.mark.asyncio
async def test_write_permission_before_operation(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    handler: str,
    required: tuple[str, ...],
    boundary: str,
    actor_kind: str,
) -> None:
    """Core members reach work; a reported missing grant stops before work.

    ``non_holder`` runs in the core edition (no plugin, no roles) and must reach
    the operation. ``denied``, ``primary`` and ``holder`` install an RBAC plugin
    stand-in with its checks disabled, where the role fallback still applies.
    """
    module = importlib.import_module(f"preloop.api.endpoints.{module_name}")
    endpoint = getattr(module, handler)
    actor = models.User(
        id=uuid4(),
        account_id=uuid4(),
        username="jane",
        is_superuser=False,
        is_active=True,
    )
    db = MagicMock()
    account = SimpleNamespace(
        primary_user_id=actor.id if actor_kind == "primary" else uuid4()
    )
    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    monkeypatch.setattr("preloop.models.crud.crud_account.get", lambda *a, **k: account)
    held = MagicMock(return_value=actor_kind == "holder")
    if actor_kind == "non_holder":
        assert permissions.members_have_full_access()
    else:
        _install_plugin_with_rbac_disabled(monkeypatch)
        monkeypatch.setattr(permissions, "user_holds_permission", held)

    target = (
        boundary if boundary.startswith("preloop.") else f"{module.__name__}.{boundary}"
    )
    owner_path, attr = target.rsplit(".", 1)
    owner = resolve_name(owner_path)
    operation = (
        AsyncMock if inspect.iscoroutinefunction(getattr(owner, attr)) else MagicMock
    )(side_effect=OperationReached)
    monkeypatch.setattr(owner, attr, operation)
    kwargs: dict[str, Any] = {"db": db, "current_user": actor}
    for name, param in inspect.signature(endpoint).parameters.items():
        if name not in kwargs and param.default is inspect.Parameter.empty:
            kwargs[name] = MagicMock()
    kwargs.update(
        {
            "flow_id": uuid4(),
            "execution_id": uuid4(),
            "issue_id": "EXAMPLE-1",
            "release_id": uuid4(),
            "item_id": uuid4(),
        }
    )
    kwargs = {
        k: v for k, v in kwargs.items() if k in inspect.signature(endpoint).parameters
    }
    if handler == "create_issue":
        kwargs["issue"] = SimpleNamespace(
            project="example",
            organization=None,
            organization_id=uuid4(),
            project_name=None,
            organization_name=None,
        )

    async def call() -> None:
        if inspect.iscoroutinefunction(endpoint):
            await endpoint(**kwargs)
        else:
            await asyncio.to_thread(endpoint, **kwargs)

    if actor_kind == "denied":
        with pytest.raises(HTTPException) as exc:
            await call()
        assert exc.value.status_code == 403
        assert required[0] in exc.value.detail
        operation.assert_not_called()
        assert db.mock_calls == []
    else:
        with pytest.raises(OperationReached):
            await call()
        operation.assert_called_once()
        if actor_kind != "non_holder":
            assert [call.args[2] for call in held.call_args_list] == (
                list(required) if actor_kind == "holder" else []
            )


@pytest.mark.parametrize("missing", ["edit_issues", "execute_flows"])
def test_pickup_requires_each_permission(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """With the RBAC plugin's checks off, each missing pickup grant stops work."""
    from preloop.api.endpoints import issue_lifecycle

    actor = models.User(id=uuid4(), account_id=uuid4(), is_superuser=False)
    _install_plugin_with_rbac_disabled(monkeypatch)
    monkeypatch.setattr("preloop.models.crud.crud_account.get", lambda *a, **k: None)
    monkeypatch.setattr(
        permissions, "user_holds_permission", lambda db, user, name: name != missing
    )
    operation = MagicMock()
    monkeypatch.setattr(issue_lifecycle, "run_lifecycle_endpoint", operation)
    with pytest.raises(HTTPException) as exc:
        issue_lifecycle.reconcile_pickup(
            db=MagicMock(), current_user=actor, issue_id=uuid4(), request=MagicMock()
        )
    assert exc.value.status_code == 403
    assert missing in exc.value.detail
    operation.assert_not_called()
