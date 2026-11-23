"""Structured questions: the answer comes back as data, not as prose.

Covers the tool half of the feature (the console half lives in the frontend
tests): ``ask_user`` and ``request_approval`` accept ``items`` and
``input_schema``, refuse a form the console cannot draw, carry the form into
``tool_args`` so every surface renders the same question, and return the
validated JSON answer. The legacy ``options`` path must keep behaving exactly
as it did, since existing presets and agents depend on its wording.
"""

import inspect
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.tools.builtin_defs import ASK_USER_TOOL, REQUEST_APPROVAL_TOOL

WAIVER_SCHEMA = {
    "type": "object",
    "properties": {
        "waived": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": ["CVE-1", "CVE-2"]},
                    "reason": {"type": "string", "minLength": 3},
                },
                "required": ["id", "reason"],
            },
        },
        "approver": {"type": "string", "x-autofill": "author"},
    },
    "required": ["waived"],
}

WAIVER_ITEMS = [
    {"id": "CVE-1", "title": "curl 8.4.0", "severity": "high"},
    {"id": "CVE-2", "title": "requests 2.31.0", "severity": "medium"},
]


def _user_ctx():
    ctx = MagicMock()
    ctx.account_id = str(uuid.uuid4())
    ctx.username = "tester"
    return ctx


def _workflow_patches():
    workflow = SimpleNamespace(id=uuid.uuid4())
    return [
        patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=_user_ctx(),
        ),
        patch(
            "preloop.models.db.session.get_db_session",
            side_effect=lambda: iter([MagicMock()]),
        ),
        patch(
            "preloop.models.crud.crud_approval_workflow.get_default",
            return_value=workflow,
        ),
    ]


def _set_meta(**overrides):
    from preloop.services import approval_helper

    meta = {
        "request_id": "11111111-2222-3333-4444-555555555555",
        "status": "approved",
        "resolved_at": "2026-09-08T12:00:00",
        "responded_by": "dimo@example.com",
        "answer": None,
    }
    meta.update(overrides)
    approval_helper._last_approval_meta_var.set(meta)


async def _tool_fn(name):
    from preloop.services.initialize_mcp import initialize_mcp_with_tools

    tool = await initialize_mcp_with_tools().get_tool(name)
    return tool.fn


@pytest.mark.asyncio
class TestAskUserForm:
    async def test_form_rides_in_tool_args(self):
        """Every surface (console, token page, mobile) reads the question off
        tool_args, so the normalized form has to be stored there."""
        fn = await _tool_fn("ask_user")
        captured = {}

        async def _require(**kwargs):
            captured.update(kwargs)
            return True, "waived CVE-1"

        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(side_effect=_require),
            ),
        ):
            _set_meta(answer={"waived": [{"id": "CVE-1", "reason": "no fix yet"}]})
            await fn(
                question="Which findings do you waive?",
                items=WAIVER_ITEMS,
                input_schema=WAIVER_SCHEMA,
            )

        arguments = captured["arguments"]
        assert arguments["is_question"] is True
        assert [row["id"] for row in arguments["items"]] == ["CVE-1", "CVE-2"]
        assert arguments["input_schema"]["properties"]["waived"]["type"] == "array"

    async def test_dropped_item_keys_ride_in_tool_args(self):
        fn = await _tool_fn("ask_user")
        captured = {}

        async def _require(**kwargs):
            captured.update(kwargs)
            return True, "ok"

        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(side_effect=_require),
            ),
        ):
            _set_meta(answer=None)
            await fn(
                question="Which files did this change?",
                items=[
                    {
                        "id": "F-1",
                        "rank": 1,
                        "path": "/foo",
                        "stacks": ["python"],
                    }
                ],
            )

        arguments = captured["arguments"]
        assert arguments["dropped_item_keys"] == ["rank", "stacks"]
        assert arguments["items"] == [
            {"id": "F-1", "title": "F-1", "fields": {"path": "/foo"}}
        ]

    async def test_answer_round_trips_as_json(self):
        answer = {
            "waived": [{"id": "CVE-1", "reason": "no fix released yet"}],
            "approver": "dimo@example.com",
        }
        fn = await _tool_fn("ask_user")
        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, "Findings to waive: id=CVE-1")),
            ),
        ):
            _set_meta(answer=answer)
            result = await fn(
                question="Which findings do you waive?",
                items=WAIVER_ITEMS,
                input_schema=WAIVER_SCHEMA,
            )

        payload = json.loads(result)
        assert payload["status"] == "answered"
        # The agent applies this directly: no parsing prose, no typed JSON.
        assert payload["answer"] == answer
        assert payload["answer_text"] == "Findings to waive: id=CVE-1"
        assert payload["approval_id"] == "11111111-2222-3333-4444-555555555555"
        assert payload["answered_by"] == "dimo@example.com"
        assert payload["answered_at"] == "2026-09-08T12:00:00"

    async def test_bad_schema_never_reaches_a_human(self):
        """A form the console cannot draw is the agent's mistake: fail while
        the agent can still fix it instead of showing a human a blank page."""
        fn = await _tool_fn("ask_user")
        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(side_effect=AssertionError("must not create a row")),
            ),
        ):
            result = await fn(
                question="Which findings do you waive?",
                input_schema={"type": "object", "properties": {"n": {"type": "date"}}},
            )
        assert result.startswith("Error: ")
        assert "n" in result

    async def test_duplicate_item_ids_are_refused(self):
        fn = await _tool_fn("ask_user")
        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(side_effect=AssertionError("must not create a row")),
            ),
        ):
            result = await fn(
                question="Which findings do you waive?",
                items=[{"id": "CVE-1"}, {"id": "CVE-1"}],
            )
        assert result.startswith("Error: ")
        assert "CVE-1" in result

    async def test_declined_form_question_reports_no_answer(self):
        fn = await _tool_fn("ask_user")
        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(False, "Tool execution declined")),
            ),
        ):
            _set_meta(status="declined", answer=None)
            result = await fn(
                question="Which findings do you waive?",
                input_schema=WAIVER_SCHEMA,
            )
        assert result.startswith("No answer provided: Tool execution declined")


@pytest.mark.asyncio
class TestAskUserLegacyPathUnchanged:
    """Presets and agents already read these exact strings."""

    async def test_options_answer_wording_is_untouched(self):
        fn = await _tool_fn("ask_user")
        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, "blue")),
            ),
        ):
            result = await fn(question="Favourite colour?", options=["blue", "red"])
        assert result == "User answered: blue"

    async def test_options_only_arguments_carry_no_form_keys(self):
        fn = await _tool_fn("ask_user")
        captured = {}

        async def _require(**kwargs):
            captured.update(kwargs)
            return True, "blue"

        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(side_effect=_require),
            ),
        ):
            await fn(question="Favourite colour?", options=["blue", "red"])

        arguments = captured["arguments"]
        assert arguments["options"] == ["blue", "red"]
        assert "items" not in arguments
        assert "input_schema" not in arguments


@pytest.mark.asyncio
class TestRequestApprovalForm:
    async def test_approved_decision_returns_the_form_answer(self):
        answer = {"waived": [{"id": "CVE-2", "reason": "not reachable"}]}
        fn = await _tool_fn("request_approval")
        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            _set_meta(answer=answer)
            result = await fn(
                operation="waive findings",
                context="release audit",
                reasoning="gate failed",
                items=WAIVER_ITEMS,
                input_schema=WAIVER_SCHEMA,
            )

        assert result.startswith("Approval granted for operation: waive findings")
        payload = json.loads(result.splitlines()[-1])
        assert payload["status"] == "approved"
        assert payload["answer"] == answer
        assert payload["answered_by"] == "dimo@example.com"

    async def test_dropped_item_keys_ride_in_tool_args(self):
        fn = await _tool_fn("request_approval")
        captured = {}

        async def _require(**kwargs):
            captured.update(kwargs)
            return True, None

        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(side_effect=_require),
            ),
        ):
            _set_meta()
            await fn(
                operation="waive findings",
                context="release audit",
                reasoning="gate failed",
                items=[{"id": "CVE-1", "rank": 9, "manifests": ["requirements.txt"]}],
            )

        arguments = captured["arguments"]
        assert arguments["dropped_item_keys"] == ["manifests", "rank"]
        assert arguments["items"] == [{"id": "CVE-1", "title": "CVE-1"}]

    async def test_plain_approval_return_is_unchanged(self):
        fn = await _tool_fn("request_approval")
        p1, p2, p3 = _workflow_patches()
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.services.initialize_mcp.require_approval",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            _set_meta()
            result = await fn(
                operation="deploy", context="prod", reasoning="release day"
            )
        assert result == (
            "Approval granted for operation: deploy\n"
            "Caller: User: tester\n"
            "Workflow used: default"
        )


@pytest.mark.asyncio
class TestBuiltinDefsParity:
    """The REST catalog and the FastMCP registration must not drift: an agent
    reads one and calls the other."""

    async def test_ask_user_signature_matches_catalog(self):
        fn = await _tool_fn("ask_user")
        runtime = {name for name in inspect.signature(fn).parameters if name != "ctx"}
        assert runtime == set(ASK_USER_TOOL["schema"]["properties"])
        assert ASK_USER_TOOL["schema"]["required"] == ["question"]

    async def test_both_tools_document_the_same_form_vocabulary(self):
        ask = ASK_USER_TOOL["schema"]["properties"]
        approve = REQUEST_APPROVAL_TOOL["schema"]["properties"]
        for props in (ask, approve):
            assert props["items"]["items"]["required"] == ["id"]
            assert props["input_schema"]["type"] == "object"
        assert ask["items"] == approve["items"]
        assert ask["input_schema"] == approve["input_schema"]

    async def test_rest_catalog_serves_the_same_definitions(self):
        from preloop.api.endpoints.tools import BUILTIN_TOOLS

        catalog = {tool["name"]: tool for tool in BUILTIN_TOOLS}
        for name in ("ask_user", "request_approval"):
            props = catalog[name]["schema"]["properties"]
            assert "items" in props
            assert "input_schema" in props

    async def test_descriptions_match_the_registered_tools(self):
        from preloop.services.initialize_mcp import initialize_mcp_with_tools

        mcp = initialize_mcp_with_tools()
        ask = await mcp.get_tool("ask_user")
        assert ask.description == ASK_USER_TOOL["description"]


# Rows the way an agent sent them on prod (2026-11-22): label instead of
# title plus extra keys the console shows as row fields.
LEAD_ITEMS = [
    {
        "id": "lead-1",
        "label": "Ada Lovelace",
        "description": "CTO, Analytical Engines",
        "linkedin": "https://linkedin.com/in/ada",
        "hubspot": "https://app.hubspot.com/contacts/1",
        "lead_file": "leads/ada/lead.md",
        "score": 7,
    }
]
LEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": ["lead-1"]},
                    "answer": {"type": "string"},
                    "follow_up": {"type": "boolean"},
                },
                "required": ["id"],
            },
        }
    },
}


async def _stored_tool_args(name, **call):
    """Run the tool once and return the tool_args it stores for approval."""
    fn = await _tool_fn(name)
    captured = {}

    async def _require(**kwargs):
        captured.update(kwargs)
        return False, '{"status": "pending_approval"}'

    p1, p2, p3 = _workflow_patches()
    with (
        p1,
        p2,
        p3,
        patch(
            "preloop.services.initialize_mcp.require_approval",
            new=AsyncMock(side_effect=_require),
        ),
    ):
        await fn(**call)
    return captured["arguments"]


async def _replay(name, tool_args, answer):
    """Re-run the approved call the way get_approval_status does."""
    from preloop.services.dynamic_fastmcp import (
        _approved_answer_var,
        _approved_id_var,
        _bypass_approval_var,
    )
    from preloop.services.initialize_mcp import initialize_mcp_with_tools

    tool = await initialize_mcp_with_tools().get_tool(name)
    p1, p2, p3 = _workflow_patches()
    _bypass_approval_var.set(True)
    _approved_answer_var.set(answer)
    _approved_id_var.set("11111111-2222-3333-4444-555555555555")
    halt_db = MagicMock()
    halt_db.run_sync = AsyncMock(return_value=set())
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=halt_db)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    try:
        with (
            p1,
            p2,
            p3,
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=session_cm,
            ),
        ):
            return await tool.run(tool_args)
    finally:
        _bypass_approval_var.set(False)
        _approved_answer_var.set(None)
        _approved_id_var.set(None)


def _result_text(result):
    content = getattr(result, "content", result)
    return "".join(getattr(block, "text", "") for block in content)


@pytest.mark.asyncio
class TestApprovedReplay:
    """get_approval_status re-invokes the tool with the stored tool_args."""

    async def test_form_with_extra_item_keys_returns_validated_answer(self):
        from preloop.services.question_schema import replay_arguments

        stored = await _stored_tool_args(
            "ask_user",
            question="Follow up with these leads?",
            items=LEAD_ITEMS,
            input_schema=LEAD_SCHEMA,
        )
        assert stored["is_question"] is True
        assert stored["dropped_item_keys"] == ["score"]
        # The extra keys the answer and the console need are kept, not dropped.
        assert stored["items"][0]["title"] == "Ada Lovelace"
        assert stored["items"][0]["fields"] == {
            "linkedin": "https://linkedin.com/in/ada",
            "hubspot": "https://app.hubspot.com/contacts/1",
            "lead_file": "leads/ada/lead.md",
        }

        answer = {"rows": [{"id": "lead-1", "answer": "yes", "follow_up": True}]}
        result = await _replay("ask_user", replay_arguments("ask_user", stored), answer)
        payload = json.loads(_result_text(result))
        assert payload["status"] == "answered"
        assert payload["answer"] == answer
        assert payload["approval_id"] == "11111111-2222-3333-4444-555555555555"

    async def test_unstripped_args_are_what_failed_on_prod(self):
        stored = await _stored_tool_args(
            "ask_user", question="Ship it?", items=LEAD_ITEMS
        )
        with pytest.raises(Exception, match="is_question|dropped_item_keys"):
            await _replay("ask_user", stored, None)

    async def test_is_question_only_replay_returns_the_answer(self):
        from preloop.services.question_schema import replay_arguments

        stored = await _stored_tool_args(
            "ask_user", question="Ship it?", options=["yes", "no"]
        )
        assert stored["is_question"] is True
        assert "dropped_item_keys" not in stored
        args = replay_arguments("ask_user", stored)
        assert "is_question" not in args
        result = await _replay("ask_user", args, None)
        assert "validation error" not in _result_text(result).lower()

    async def test_request_approval_form_replays(self):
        from preloop.services.question_schema import replay_arguments

        stored = await _stored_tool_args(
            "request_approval",
            operation="Contact leads",
            context="c",
            reasoning="r",
            items=LEAD_ITEMS,
            input_schema=LEAD_SCHEMA,
        )
        assert "dropped_item_keys" in stored
        answer = {"rows": [{"id": "lead-1", "follow_up": False}]}
        result = await _replay(
            "request_approval", replay_arguments("request_approval", stored), answer
        )
        assert '"answer": {"rows"' in _result_text(result)

    async def test_other_tools_keep_their_arguments(self):
        from preloop.services.question_schema import replay_arguments

        args = {"is_question": True, "dropped_item_keys": ["x"]}
        assert replay_arguments("some_upstream_tool", args) == args

    async def test_normalized_items_renormalize_unchanged(self):
        from preloop.services.question_schema import normalize_items

        once = normalize_items(LEAD_ITEMS)
        dropped: set[str] = set()
        assert normalize_items(once, dropped_keys=dropped) == once
        assert dropped == set()

    @pytest.mark.parametrize("name", ["ask_user", "request_approval"])
    async def test_replay_parameters_match_registered_signature(self, name):
        from preloop.services.question_schema import REPLAY_PARAMETERS

        params = set(inspect.signature(await _tool_fn(name)).parameters) - {"ctx"}
        assert REPLAY_PARAMETERS[name] == params

    async def test_publication_scope_keys_are_not_replayed(self):
        from preloop.services.question_schema import replay_arguments

        stored = {
            "operation": "Publish firmware",
            "caller": "agent",
            "context": "c",
            "reasoning": "r",
            "action": "isolated_publication",
            "candidates": [{"artifact": "fw", "sha": "a" * 40}],
            "dropped_item_keys": ["x"],
        }
        args = replay_arguments("request_approval", stored)
        assert set(args) == {"operation", "caller", "context", "reasoning"}
        result = await _replay("request_approval", args, None)
        assert "Approval granted" in _result_text(result)
