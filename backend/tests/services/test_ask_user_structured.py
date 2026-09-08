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
