"""Per-flow approval policy for unattended persistent runs (#1671)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.services import flow_approval_policy as policy_mod
from preloop.services.flow_approval_policy import (
    FlowApprovalGrant,
    resolve_flow_grant,
    tool_call_within_policy,
    validate_approval_policy,
)

WORKSPACE = "/srv/work/project"


COMMANDS = (
    ("git",),
    ("git", "remote"),
    ("git", "status"),
    ("git", "add"),
    ("git", "commit"),
    ("git", "diff"),
    ("ls",),
    ("pytest",),
    ("head",),
)


def _grant(tools: tuple[str, ...] = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")):
    return FlowApprovalGrant(
        flow_id=uuid4(),
        flow_name="Nightly",
        execution_id=str(uuid4()),
        tools=frozenset(tools),
        paths=(WORKSPACE,),
        commands=COMMANDS,
    )


class TestValidate:
    def test_default_is_ask(self) -> None:
        assert validate_approval_policy({}) == {
            "mode": "ask",
            "tools": [],
            "paths": [],
            "commands": [],
        }

    def test_auto_allow(self) -> None:
        assert validate_approval_policy(
            {
                "mode": "auto_allow",
                "tools": ["Bash"],
                "paths": ["<workspace>", "/a/b/"],
                "commands": ["git  commit", "pytest"],
            }
        ) == {
            "mode": "auto_allow",
            "tools": ["Bash"],
            "paths": ["<workspace>", "/a/b"],
            "commands": ["git commit", "pytest"],
        }

    @pytest.mark.parametrize(
        "value",
        [
            "auto_allow",
            {"mode": "always"},
            {"mode": "auto_allow", "tools": ["WebFetch"], "paths": ["/a"]},
            {"mode": "auto_allow", "tools": ["Bash"], "paths": ["relative"]},
            {"mode": "auto_allow", "tools": ["Bash"], "paths": ["/"]},
            {"mode": "auto_allow", "tools": [], "paths": ["/a"]},
            {"mode": "auto_allow", "tools": ["Bash"], "paths": []},
            {"mode": "auto_allow", "tools": ["Bash"], "paths": ["/a"], "x": 1},
            {"mode": "auto_allow", "tools": ["Bash"], "paths": ["/a"]},
            {
                "mode": "auto_allow",
                "tools": ["Bash"],
                "paths": ["/a"],
                "commands": ["git $(x)"],
            },
            {
                "mode": "auto_allow",
                "tools": ["Bash"],
                "paths": ["/a"],
                "commands": [""],
            },
            {
                "mode": "auto_allow",
                "tools": ["Bash"],
                "paths": ["/a"],
                "commands": ["sh"],
            },
            {
                "mode": "auto_allow",
                "tools": ["Bash"],
                "paths": ["/a"],
                "commands": ["python3 -c"],
            },
            {
                "mode": "auto_allow",
                "tools": ["Bash"],
                "paths": ["/a"],
                "commands": ["env"],
            },
        ],
    )
    def test_rejects(self, value: Any) -> None:
        with pytest.raises(ValueError):
            validate_approval_policy(value)

    def test_flow_schema_validates_policy(self) -> None:
        from pydantic import ValidationError

        from preloop.models.schemas.flow import FlowCreate

        with pytest.raises(ValidationError, match="approval_policy"):
            FlowCreate(
                name="n",
                trigger_event_source="schedule",
                prompt_template="p",
                agent_type="codex",
                agent_config={"approval_policy": {"mode": "auto_allow"}},
            )


class TestToolCalls:
    @pytest.mark.parametrize(
        "command",
        [
            "git status",
            "git add -A && git commit -m 'nightly'",
            f"ls {WORKSPACE}/src",
            "pytest -q tests",
            'git commit -m "docs: link https://example.com"',
            "git diff | head -n 50",
            'git commit -m "document curl usage"',
            'git commit -m "use wget instead; rotate ssh keys; gh cli notes"',
            "git add -u",
            "git status -u",
            "git remote -v",
            "git remote show origin",
            "git remote get-url origin",
        ],
    )
    def test_bash_allowed_in_workspace(self, command: str) -> None:
        assert tool_call_within_policy(
            _grant(), tool_name="Bash", tool_input={"command": command}, cwd=WORKSPACE
        )

    @pytest.mark.parametrize(
        "command",
        [
            "git push origin main",
            "git -C . push",
            "curl https://example.com",
            "cat /etc/passwd",
            "cat ../other/secret",
            "cat ~/.ssh/id_rsa",
            "sudo ls",
            "gh pr create",
            "echo x; wget http://x",
            "echo 'unterminated",
            'git "push" origin main',
            "GIT=git; $GIT push",
            "sh -c 'cat /etc/passwd'",
            "python3 -c \"open('/etc/passwd').read()\"",
            'git commit -m "$(cat msg)"',
            "git status > /tmp/out",
            "(git status)",
            "git status &",
            "rm -rf build",
            "git status\nrm -rf x",
            "git -c alias.p=push p",
            "git remote add origin http://evil",
            "git remote set-url origin http://evil",
            "git remote -v add origin http://evil",
            "git remote --verbose add origin http://evil",
            "git remote -v set-url origin http://evil",
            "git remote update",
            "git remote prune origin",
            "git remote remove origin",
            "git remote rename origin x",
            "git remote set-head origin -a",
            "git remote set-branches origin main",
            "git ls-remote origin",
            "git submodule update --init",
            "git -c alias.x='!curl http://evil' x",
            "git -C . status",
            "git commit --exec-path=/tmp/x",
        ],
    )
    def test_bash_outside_policy(self, command: str) -> None:
        assert not tool_call_within_policy(
            _grant(), tool_name="Bash", tool_input={"command": command}, cwd=WORKSPACE
        )

    def test_bash_cwd_outside_workspace(self) -> None:
        assert not tool_call_within_policy(
            _grant(), tool_name="Bash", tool_input={"command": "ls"}, cwd="/srv/other"
        )
        assert not tool_call_within_policy(
            _grant(), tool_name="Bash", tool_input={"command": "ls"}, cwd=None
        )

    def test_file_tools(self) -> None:
        grant = _grant()
        assert tool_call_within_policy(
            grant,
            tool_name="Edit",
            tool_input={"file_path": f"{WORKSPACE}/a.py"},
            cwd=WORKSPACE,
        )
        for path in ("/srv/work/project-other/a.py", f"{WORKSPACE}/../x", "a.py"):
            assert not tool_call_within_policy(
                grant, tool_name="Write", tool_input={"file_path": path}, cwd=WORKSPACE
            )
        assert not tool_call_within_policy(
            grant, tool_name="Read", tool_input={}, cwd=WORKSPACE
        )

    def test_search_tools(self) -> None:
        grant = _grant()
        assert tool_call_within_policy(
            grant,
            tool_name="Grep",
            tool_input={"pattern": "x", "path": "src"},
            cwd=WORKSPACE,
        )
        assert not tool_call_within_policy(
            grant,
            tool_name="Glob",
            tool_input={"pattern": "*", "path": "/etc"},
            cwd=WORKSPACE,
        )

    def test_bash_needs_commands(self) -> None:
        grant = FlowApprovalGrant(
            flow_id=uuid4(),
            flow_name="Nightly",
            execution_id=str(uuid4()),
            tools=frozenset({"Bash"}),
            paths=(WORKSPACE,),
        )
        assert not tool_call_within_policy(
            grant, tool_name="Bash", tool_input={"command": "ls"}, cwd=WORKSPACE
        )

    def test_tool_not_listed(self) -> None:
        assert not tool_call_within_policy(
            _grant(("Read",)),
            tool_name="Bash",
            tool_input={"command": "ls"},
            cwd=WORKSPACE,
        )
        assert not tool_call_within_policy(
            _grant(), tool_name="WebFetch", tool_input={"url": "x"}, cwd=WORKSPACE
        )


def _patch_crud(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account_id: str,
    status: str = "RUNNING",
    agent_config: Any = None,
    binding: Any = None,
) -> MagicMock:
    from preloop.models.crud import crud_flow, crud_flow_execution

    account_id_ = account_id
    execution = SimpleNamespace(
        id=uuid4(), account_id=account_id, status=status, flow_id=uuid4()
    )
    flow = SimpleNamespace(
        id=execution.flow_id,
        name="Nightly",
        agent_config=agent_config
        if agent_config is not None
        else {
            "execution_path": "persistent",
            "approval_policy": {
                "mode": "auto_allow",
                "tools": ["Bash"],
                "paths": ["<workspace>"],
                "commands": ["git commit"],
            },
        },
    )
    monkeypatch.setattr(crud_flow_execution, "get", lambda db, id: execution)
    monkeypatch.setattr(
        crud_flow,
        "get",
        lambda db, id, account_id=None: (
            flow if str(account_id) == account_id_ else None
        ),
    )
    bind = MagicMock(
        return_value=binding
        if binding is not None
        else {"approval_session_id": "s-1", "approval_cwd": WORKSPACE}
    )
    monkeypatch.setattr(crud_flow_execution, "bind_approval_session", bind)
    return bind


def _resolve(account_id: str, **overrides: Any):
    kwargs = {
        "account_id": account_id,
        "managed_agent_id": uuid4(),
        "execution_id": str(uuid4()),
        "session_id": "s-1",
        "cwd": WORKSPACE,
    }
    kwargs.update(overrides)
    return resolve_flow_grant(MagicMock(), **kwargs)


class TestResolveGrant:
    def test_grant_binds_to_session_and_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        account_id = str(uuid4())
        bind = _patch_crud(monkeypatch, account_id=account_id)
        grant = _resolve(account_id)
        assert grant is not None
        assert grant.paths == (WORKSPACE,)
        assert grant.tools == frozenset({"Bash"})
        assert grant.commands == (("git", "commit"),)
        assert bind.call_args.kwargs["session_id"] == "s-1"

    def test_other_session_gets_no_grant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        account_id = str(uuid4())
        _patch_crud(
            monkeypatch,
            account_id=account_id,
            binding={"approval_session_id": "s-first", "approval_cwd": WORKSPACE},
        )
        assert _resolve(account_id, session_id="s-second") is None

    @pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT"])
    def test_expires_at_execution_end(
        self, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        account_id = str(uuid4())
        bind = _patch_crud(monkeypatch, account_id=account_id, status=status)
        assert _resolve(account_id) is None
        bind.assert_not_called()

    def test_other_account_gets_no_grant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_crud(monkeypatch, account_id=str(uuid4()))
        assert _resolve(str(uuid4())) is None

    @pytest.mark.parametrize(
        "agent_config",
        [
            {"execution_path": "persistent"},
            {"execution_path": "persistent", "approval_policy": {"mode": "ask"}},
            {
                "approval_policy": {
                    "mode": "auto_allow",
                    "tools": ["Bash"],
                    "paths": ["/srv"],
                    "commands": ["git commit"],
                }
            },
            {"execution_path": "persistent", "approval_policy": {"mode": "bogus"}},
        ],
    )
    def test_no_auto_allow_policy(
        self, monkeypatch: pytest.MonkeyPatch, agent_config: Any
    ) -> None:
        account_id = str(uuid4())
        _patch_crud(monkeypatch, account_id=account_id, agent_config=agent_config)
        assert _resolve(account_id) is None

    def test_requires_session_and_valid_execution_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        account_id = str(uuid4())
        _patch_crud(monkeypatch, account_id=account_id)
        assert _resolve(account_id, session_id="") is None
        assert _resolve(account_id, execution_id="not-a-uuid") is None
        assert _resolve(account_id, managed_agent_id=None) is None

    def test_workspace_path_kind_wins_over_cwd(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        account_id = str(uuid4())
        _patch_crud(
            monkeypatch,
            account_id=account_id,
            agent_config={
                "execution_path": "persistent",
                "workspace": {"kind": "path", "path": "/srv/declared"},
                "approval_policy": {
                    "mode": "auto_allow",
                    "tools": ["Bash"],
                    "paths": ["<workspace>"],
                    "commands": ["git commit"],
                },
            },
        )
        grant = _resolve(account_id)
        assert grant is not None and grant.paths == ("/srv/declared",)


@pytest.mark.asyncio
async def test_request_agent_permission_records_flow_policy_approval() -> None:
    from preloop.services.agent_permission_service import request_agent_permission

    account_id = str(uuid4())
    workflow = MagicMock(id=uuid4(), timeout_seconds=300)
    tool_config = MagicMock(id=uuid4())
    approval = MagicMock(id=uuid4(), status="approved", approver_comment="ok")
    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=tool_config),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.agent_permission_service.native_tool_approvals_disabled",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "preloop.services.agent_permission_service.resolve_workflow",
            new=AsyncMock(return_value=workflow),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = AsyncMock()
        service = AsyncMock()
        service.create_and_notify = AsyncMock(return_value=approval)
        mock_service_cls.return_value = service
        decision, _reason, request_id, timed_out = await request_agent_permission(
            base_url="http://localhost",
            account_id=account_id,
            user_id=uuid4(),
            managed_agent_id=uuid4(),
            runtime_session_id=None,
            managed_agent_name="Claude Code",
            source="claude_code",
            tool_name="Bash",
            tool_input={"command": "git status"},
            agent_reasoning=None,
            client_decision="ask",
            flow_id=uuid4(),
            flow_policy_label="Nightly",
        )
    assert (decision, request_id, timed_out) == ("allow", str(approval.id), False)
    kwargs = service.create_and_notify.await_args.kwargs
    assert (
        kwargs["standing_bypass_reason"]
        == models.AutoApprovedReason.FLOW_APPROVAL_POLICY
    )
    assert kwargs["standing_bypass_text"] == (
        "Auto-approved by the approval policy of flow Nightly"
    )


@pytest.mark.asyncio
async def test_require_approval_rule_still_asks_under_flow_policy() -> None:
    from preloop.services.agent_permission_service import request_agent_permission

    workflow = MagicMock(id=uuid4(), timeout_seconds=300)
    approval = MagicMock(id=uuid4(), status="declined", approver_comment="no")
    with (
        patch(
            "preloop.services.agent_permission_service.get_async_db_session"
        ) as mock_get_session,
        patch("preloop.services.approval_service.ApprovalService") as mock_service_cls,
        patch(
            "preloop.models.crud.tool_configuration."
            "get_tool_config_by_name_and_source_async",
            new=AsyncMock(return_value=MagicMock(id=uuid4())),
        ),
        patch(
            "preloop.services.agent_permission_service.apply_native_access_rules",
            new=AsyncMock(return_value=("require_approval", "rule", None, {"r": 1})),
        ),
        patch(
            "preloop.services.agent_permission_service.native_tool_approvals_disabled",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "preloop.services.agent_permission_service.resolve_workflow",
            new=AsyncMock(return_value=workflow),
        ),
    ):
        mock_get_session.return_value.__aenter__.return_value = AsyncMock()
        service = AsyncMock()
        service.create_and_notify = AsyncMock(return_value=approval)
        mock_service_cls.return_value = service
        decision, *_ = await request_agent_permission(
            base_url="http://localhost",
            account_id=str(uuid4()),
            user_id=uuid4(),
            managed_agent_id=uuid4(),
            runtime_session_id=None,
            managed_agent_name="Claude Code",
            source="claude_code",
            tool_name="Bash",
            tool_input={"command": "git status"},
            agent_reasoning=None,
            client_decision="ask",
            flow_policy_label="Nightly",
        )
    assert decision == "deny"
    kwargs = service.create_and_notify.await_args.kwargs
    assert kwargs["standing_bypass_reason"] is None
    assert kwargs["standing_bypass_text"] is None


@pytest.mark.asyncio
async def test_endpoint_applies_flow_policy_to_bound_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.api.endpoints import agent_permission as endpoint

    identity = endpoint.PermissionIdentity(
        account_id=str(uuid4()),
        user_id=uuid4(),
        api_key_id=uuid4(),
        managed_agent_id=uuid4(),
        runtime_session_id=None,
        managed_agent_name="Claude Code",
    )
    grant = _grant(("Bash",))
    # identity, agent's open session, origin session, flow grant, note claim.
    monkeypatch.setattr(
        endpoint,
        "run_db_off_loop",
        AsyncMock(side_effect=[identity, None, None, grant, None, None]),
    )
    monkeypatch.setattr(endpoint, "_permission_check_base_url", lambda: "https://e")
    service = AsyncMock(return_value=("allow", "policy", "req-1", False))
    monkeypatch.setattr(endpoint, "request_agent_permission", service)
    await endpoint.agent_permission_check(
        endpoint.AgentPermissionCheckRequest(
            tool_name="Bash",
            tool_input={"command": "git commit -m x"},
            source="claude_code",
            session_id="s-1",
            flow_execution_id=grant.execution_id,
        ),
        authorization="Bearer synthetic-token",
    )
    kwargs = service.await_args.kwargs
    assert kwargs["flow_policy_label"] is None  # no cwd reported: outside policy
    assert kwargs["flow_id"] == grant.flow_id


@pytest.mark.asyncio
async def test_endpoint_without_execution_id_skips_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.api.endpoints import agent_permission as endpoint

    identity = endpoint.PermissionIdentity(
        account_id=str(uuid4()),
        user_id=uuid4(),
        api_key_id=uuid4(),
        managed_agent_id=uuid4(),
        runtime_session_id=None,
        managed_agent_name="Claude Code",
    )
    monkeypatch.setattr(
        endpoint, "run_db_off_loop", AsyncMock(side_effect=[identity, None, None, None])
    )
    monkeypatch.setattr(endpoint, "_permission_check_base_url", lambda: "https://e")
    service = AsyncMock(return_value=("allow", "", None, False))
    monkeypatch.setattr(endpoint, "request_agent_permission", service)
    await endpoint.agent_permission_check(
        endpoint.AgentPermissionCheckRequest(
            tool_name="Bash", source="claude_code", session_id="s-1"
        ),
        authorization="Bearer synthetic-token",
    )
    assert service.await_args.kwargs["flow_policy_label"] is None
    assert service.await_args.kwargs["flow_id"] is None


def test_module_reason_matches_model() -> None:
    assert (
        policy_mod.FLOW_APPROVAL_POLICY_REASON
        == models.AutoApprovedReason.FLOW_APPROVAL_POLICY
    )
