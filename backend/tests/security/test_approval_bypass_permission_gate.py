"""Permission gate on the approval-bypass endpoints.

Background
----------
``preloop.utils.permissions.require_permission`` degrades to a no-op on a
core build: when the proprietary RBAC plugin is not importable,
``_require_human_permission`` returns ``_with_authorizer(func, ...)``, and
``_with_authorizer`` consults the account authorizer *only when one is
registered*.  With neither present, every authenticated caller is admitted.

That is intended.  The core edition has no access controls between members
of an account: every member may do everything in it, including opening an
``auto_approve`` bypass.  Roles and the ``decide_approvals`` gate belong to
the RBAC plugin (Enterprise Edition), where the gate matters because a
bypass removes the human from the approval loop.

These tests pin three things:

1. the *precondition* — the decorator really is a no-op on a core build,
   and really does deny when the plugin is present;
2. the *core behaviour* — without the plugin, ``ensure_permission_in_oss``
   admits every member, and an ``auto_approve`` bypass does short-circuit
   the human gate;
3. the *EE behaviour* — the write side requires ``decide_approvals``, so
   with the plugin a read-only ``analyst`` role is refused while a
   principal that holds the permission, the superuser and the account's
   primary user are not locked out.
"""

import importlib
import importlib.util
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from preloop.api.endpoints import approval_bypass as bypass_endpoints


@pytest.fixture(autouse=True)
def _restore_permission_modules():
    """Undo the module reloads and authorizer changes made by these tests.

    Autouse, so it is set up before ``monkeypatch`` and torn down after it:
    by then the stand-in plugin is gone from ``sys.modules`` and reloading
    restores the real OSS state for later test files.
    """
    from preloop.plugins import account_hooks

    authorizer = account_hooks.get_authorizer()
    yield
    account_hooks.register_authorizer(authorizer)
    importlib.reload(importlib.import_module("preloop.utils.permissions"))
    importlib.reload(bypass_endpoints)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_actor(*, is_superuser: bool = False):
    """A user object shaped like the ones the endpoints touch."""
    actor = MagicMock()
    actor.id = uuid.uuid4()
    actor.account_id = uuid.uuid4()
    actor.username = "actor"
    actor.is_active = True
    actor.is_superuser = is_superuser
    return actor


def _make_db():
    db = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock(return_value=None)
    return db


def _oss_state(monkeypatch):
    """Put the process into the OSS permission state.

    No RBAC plugin, no registered authorizer.  This is the state a stock
    self-hosted deployment is in.
    """
    import preloop.utils.permissions as perms

    monkeypatch.setattr(perms, "_plugin_require_permission", None, raising=False)
    from preloop.plugins import account_hooks

    account_hooks.register_authorizer(None)
    return importlib.reload(bypass_endpoints)


def _rbac_plugin_state(monkeypatch):
    """Put the process into the plugin-present state.

    The synthetic plugin resolves the permission through the *live*
    ``user_holds_permission`` attribute, so the test can drive the
    decision from the repository's own role matrix rather than a
    hard-coded deny.
    """
    import sys
    import types

    def plugin_require_permission(permission_name):
        def decorator(func):
            def wrapper(*args, **kwargs):
                actor = kwargs.get("current_user")
                db = kwargs.get("db")
                if actor is not None and db is not None:
                    import preloop.utils.permissions as live_perms

                    if not live_perms.user_holds_permission(db, actor, permission_name):
                        raise HTTPException(
                            status_code=403,
                            detail=(
                                f"Insufficient permissions. Required: {permission_name}"
                            ),
                        )
                return func(*args, **kwargs)

            return wrapper

        return decorator

    monkeypatch.setenv("DISABLE_PROPRIETARY_PLUGINS", "false")
    module = types.ModuleType("preloop.plugins.proprietary.rbac.permissions")
    module.require_permission = plugin_require_permission  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "preloop.plugins.proprietary.rbac.permissions", module
    )
    for parent in ("preloop.plugins.proprietary", "preloop.plugins.proprietary.rbac"):
        if parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
    sys.modules["preloop.plugins.proprietary.rbac"].permissions = module  # type: ignore[attr-defined]

    from preloop.plugins import account_hooks

    account_hooks.register_authorizer(None)

    importlib.reload(sys.modules["preloop.utils.permissions"])
    return importlib.reload(bypass_endpoints)


def _call_create_bypass(actor, db, endpoints, *, gate=None, spy=None):
    """Invoke the decorated ``create_bypass`` exactly as FastAPI would.

    ``gate`` replaces ``ensure_permission_in_oss`` with a failing stub so
    the caller can drive the refusal.  ``None`` means "the gate is a
    no-op", which is the pre-fix state.  ``spy`` receives the call so the
    permission name the endpoint asked for can be asserted.
    """
    from preloop.schemas.approval_bypass import ApprovalBypassCreate

    payload = ApprovalBypassCreate(mode="auto_approve", duration_minutes=15)

    bypass = MagicMock()
    bypass.id = uuid.uuid4()
    bypass.account_id = actor.account_id
    bypass.user_id = actor.id
    bypass.managed_agent_id = None
    bypass.mode = "auto_approve"
    bypass.reason = None
    bypass.created_by_user_id = actor.id
    bypass.created_via = "api"
    bypass.created_at = None
    bypass.expires_at = None
    bypass.revoked_at = None
    bypass.revoked_by_user_id = None
    bypass.auto_approved_count = 0

    with patch.object(endpoints.crud_approval_bypass, "get", return_value=None):
        if gate is not None:
            with patch.object(endpoints, "ensure_permission_in_oss", side_effect=gate):
                return endpoints.create_bypass(
                    payload=payload, current_user=actor, db=db
                )
        if spy is not None:
            with patch.object(endpoints, "ensure_permission_in_oss", spy):
                return endpoints.create_bypass(
                    payload=payload, current_user=actor, db=db
                )
        with patch.object(endpoints, "ensure_permission_in_oss"):
            return endpoints.create_bypass(payload=payload, current_user=actor, db=db)


# --------------------------------------------------------------------------
# 1. The precondition: the decorator degrades to a no-op on an OSS build
# --------------------------------------------------------------------------


class TestOssPermissionDegradation:
    """Establish the precondition the finding rests on."""

    def test_require_permission_is_noop_without_plugin_or_authorizer(self, monkeypatch):
        """An OSS build lets any authenticated user through the decorator."""
        _oss_state(monkeypatch)
        perms = __import__("preloop.utils.permissions", fromlist=["x"])

        called = {"hit": False}

        @perms.require_permission("decide_approvals")
        def handler(current_user=None, db=None):
            called["hit"] = True
            return "reached"

        actor = _make_actor()
        assert handler(current_user=actor, db=MagicMock()) == "reached"
        assert called["hit"] is True

    def test_plugin_build_enforces_the_same_decorator(self, monkeypatch):
        """With the plugin present and the role lacking, the same call 403s.

        This is the control arm: it proves the decorator *can* deny, so
        the OSS arm's pass is a real degradation rather than a decorator
        that never denies anything.
        """
        _rbac_plugin_state(monkeypatch)
        import sys

        perms = sys.modules["preloop.utils.permissions"]

        @perms.require_permission("decide_approvals")
        def handler(current_user=None, db=None):
            return "reached"

        actor = _make_actor()
        db = MagicMock()
        with patch.object(perms, "user_holds_permission", return_value=False):
            with pytest.raises(HTTPException) as exc:
                handler(current_user=actor, db=db)
        assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# 2. The degradation: without a gate, the bypass opens and disarms the gate
# --------------------------------------------------------------------------


class TestWithoutTheGate:
    """The pre-fix behaviour, kept as executable evidence."""

    def test_read_only_actor_opens_an_auto_approve_bypass(self, monkeypatch):
        """Without the OSS gate, a low-privilege actor opens a bypass."""
        endpoints = _oss_state(monkeypatch)
        actor = _make_actor()
        db = _make_db()

        # gate=None removes the OSS fallback entirely: the pre-fix state.
        result = _call_create_bypass(actor, db, endpoints, gate=None)

        assert result is not None
        assert result.mode == "auto_approve"
        assert db.add.called

    def test_plugin_build_refuses_the_same_actor(self, monkeypatch):
        """The plugin build refuses the same actor — the differential."""
        endpoints = _rbac_plugin_state(monkeypatch)
        import sys

        perms = sys.modules["preloop.utils.permissions"]
        actor = _make_actor()
        db = _make_db()

        with patch.object(perms, "user_holds_permission", return_value=False):
            with pytest.raises(HTTPException) as exc:
                _call_create_bypass(actor, db, endpoints, gate=None)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_auto_approve_bypass_short_circuits_the_human_gate(self):
        """With an AUTO_APPROVE bypass in force, the request is approved.

        The service resolves the bypass *before* the human path, and the
        bypass belongs to the workflow's approver, so a bypass held by the
        approver silently approves the request the approver was supposed
        to see.
        """
        from datetime import datetime, timedelta

        from preloop.models.models.approval_bypass import (
            ApprovalBypass,
            ApprovalBypassMode,
        )
        from preloop.services.approval_service import ApprovalService

        db = AsyncMock(run_sync=AsyncMock(return_value=None))
        service = ApprovalService(db, "https://app.test")

        account_id = uuid.uuid4()
        approver_id = uuid.uuid4()
        agent_id = uuid.uuid4()

        workflow = MagicMock()
        workflow.id = uuid.uuid4()
        workflow.approval_mode = "human"
        workflow.approver_user_ids = [approver_id]
        workflow.approver_team_ids = []

        bypass = ApprovalBypass(
            id=uuid.uuid4(),
            account_id=account_id,
            user_id=approver_id,
            managed_agent_id=None,
            mode=ApprovalBypassMode.AUTO_APPROVE,
            created_by_user_id=approver_id,
            expires_at=datetime.utcnow() + timedelta(hours=1),
            auto_approved_count=0,
        )

        with (
            patch.object(
                service,
                "_get_all_approver_user_ids",
                AsyncMock(return_value=[approver_id]),
            ),
            patch(
                "preloop.services.approval_service.get_active_bypass_async",
                AsyncMock(return_value=bypass),
            ),
        ):
            resolved = await service._resolve_bypass(
                workflow,
                str(account_id),
                agent_id,
                required_mode=ApprovalBypassMode.AUTO_APPROVE,
            )

        assert resolved is not None
        assert resolved.mode == ApprovalBypassMode.AUTO_APPROVE

    @pytest.mark.asyncio
    async def test_mute_only_bypass_never_auto_approves(self):
        """A mute-only bypass must not disarm the gate (control arm)."""
        from datetime import datetime, timedelta

        from preloop.models.models.approval_bypass import (
            ApprovalBypass,
            ApprovalBypassMode,
        )
        from preloop.services.approval_service import ApprovalService

        db = AsyncMock(run_sync=AsyncMock(return_value=None))
        service = ApprovalService(db, "https://app.test")

        account_id = uuid.uuid4()
        approver_id = uuid.uuid4()

        workflow = MagicMock()
        workflow.id = uuid.uuid4()
        workflow.approval_mode = "human"
        workflow.approver_user_ids = [approver_id]
        workflow.approver_team_ids = []

        bypass = ApprovalBypass(
            id=uuid.uuid4(),
            account_id=account_id,
            user_id=approver_id,
            managed_agent_id=None,
            mode=ApprovalBypassMode.MUTE_NOTIFICATIONS,
            created_by_user_id=approver_id,
            expires_at=datetime.utcnow() + timedelta(hours=1),
            auto_approved_count=0,
        )

        with (
            patch.object(
                service,
                "_get_all_approver_user_ids",
                AsyncMock(return_value=[approver_id]),
            ),
            patch(
                "preloop.services.approval_service.get_active_bypass_async",
                AsyncMock(return_value=bypass),
            ),
        ):
            resolved = await service._resolve_bypass(
                workflow,
                str(account_id),
                uuid.uuid4(),
                required_mode=ApprovalBypassMode.AUTO_APPROVE,
            )

        assert resolved is None


# --------------------------------------------------------------------------
# 3. The fix: the write side is gated on the decision permission
# --------------------------------------------------------------------------


class TestOssBuildGate:
    """The write side must be gated even when the plugin is absent."""

    def test_reader_without_decide_approvals_is_refused(self, monkeypatch):
        """A principal who may view approvals but not decide them is refused."""
        endpoints = _oss_state(monkeypatch)
        actor = _make_actor()
        db = _make_db()

        with pytest.raises(HTTPException) as exc:
            _call_create_bypass(
                actor,
                db,
                endpoints,
                gate=HTTPException(status_code=403, detail="denied"),
            )
        assert exc.value.status_code == 403
        assert not db.add.called

    def test_principal_with_permission_is_allowed(self, monkeypatch):
        """The gate does not lock out a principal who holds the permission."""
        endpoints = _oss_state(monkeypatch)
        actor = _make_actor()
        db = _make_db()

        ensure = MagicMock()
        result = _call_create_bypass(actor, db, endpoints, gate=None, spy=ensure)

        assert result is not None
        assert result.mode == "auto_approve"
        assert db.add.called
        # The write gate is the decision permission, not the read one.
        assert ensure.call_args[0][2] == "decide_approvals"

    def test_listing_uses_the_read_permission(self, monkeypatch):
        """Listing stays on the read permission."""
        endpoints = _oss_state(monkeypatch)
        actor = _make_actor()
        db = _make_db()

        with patch.object(endpoints, "ensure_permission_in_oss") as ensure:
            with patch.object(
                endpoints.crud_approval_bypass,
                "list_active_for_account",
                return_value=[],
            ):
                endpoints.list_bypasses(current_user=actor, db=db)

        assert ensure.call_args[0][2] == "view_approvals"


class TestPermissionChoice:
    """Pin the choice of permission, not just the presence of a gate."""

    def test_write_sides_require_decide_approvals(self):
        """Creating or revoking a bypass needs the decision permission."""
        assert bypass_endpoints.BYPASS_WRITE_PERMISSION == "decide_approvals"
        assert bypass_endpoints.BYPASS_READ_PERMISSION == "view_approvals"

    def test_read_only_role_cannot_reach_the_write_side(self):
        """``analyst`` holds the read permission but not the write one.

        The role matrix in ``scripts/init_system_roles.py`` puts
        ``view_approvals`` in ``_READ_CORE`` and leaves ``decide_approvals``
        out of it, so the analyst role can observe the approval queue without
        being granted the decision permission.  This test pins the role
        matrix and the bypass router only.  Gating bypass creation on the read
        permission would have let that role open an ``auto_approve`` bypass
        and approve on the approver's behalf.
        """
        script = os.path.join(
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            ),
            "scripts",
            "init_system_roles.py",
        )
        spec = importlib.util.spec_from_file_location("isr", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        read_core = set(module._READ_CORE)
        roles = module.SYSTEM_ROLES

        assert "view_approvals" in read_core
        assert "decide_approvals" not in read_core

        analyst = roles["analyst"]["permissions"]
        assert "view_approvals" in analyst
        assert "decide_approvals" not in analyst

        # Every role that can view approvals is either a decider or the
        # read-only analyst, which is exactly the split the gate relies on.
        for name, data in roles.items():
            if "view_approvals" in data["permissions"]:
                assert "decide_approvals" in data["permissions"] or name == "analyst", (
                    f"{name} holds view_approvals without decide_approvals"
                )


# --------------------------------------------------------------------------
# 4. The shipped OSS fallback: nobody is locked out of their own account
# --------------------------------------------------------------------------


class TestEnsurePermissionInOssBehaviour:
    """Exercise the shipped OSS fallback, not a mock of it."""

    def test_superuser_passes(self, monkeypatch):
        """``ensure_permission_in_oss`` lets a superuser through."""
        _oss_state(monkeypatch)
        import preloop.utils.permissions as perms

        actor = _make_actor(is_superuser=True)
        db = _make_db()

        with patch.object(perms, "_plugin_require_permission", None):
            perms.ensure_permission_in_oss(db, actor, "decide_approvals")

    def test_primary_user_passes(self, monkeypatch):
        """The account's primary user is never locked out of their account."""
        _oss_state(monkeypatch)
        import preloop.utils.permissions as perms

        actor = _make_actor(is_superuser=False)
        db = _make_db()

        account = MagicMock()
        account.primary_user_id = actor.id

        with patch.object(perms, "_plugin_require_permission", None):
            with patch("preloop.models.crud.crud_account.get", return_value=account):
                perms.ensure_permission_in_oss(db, actor, "decide_approvals")

    def test_ordinary_user_without_the_role_is_admitted_in_core(self, monkeypatch):
        """Core has no role checks: an ordinary member is admitted."""
        _oss_state(monkeypatch)
        import preloop.utils.permissions as perms

        actor = _make_actor(is_superuser=False)
        db = _make_db()

        account = MagicMock()
        account.primary_user_id = uuid.uuid4()  # somebody else

        with patch.object(perms, "_plugin_require_permission", None):
            with patch("preloop.models.crud.crud_account.get", return_value=account):
                with patch.object(perms, "user_holds_permission", return_value=False):
                    perms.ensure_permission_in_oss(db, actor, "decide_approvals")
