"""Browser steps derived from Playwright MCP tool calls."""

from __future__ import annotations

import base64
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from mcp import types
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_runtime_session, crud_runtime_session_activity
from preloop.schemas.browser_step import BrowserStepIn
from preloop.services.browser_steps import screenshot_bytes_error
from preloop.services.dynamic_fastmcp import DynamicFastMCP
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.playwright_steps import (
    PLAYWRIGHT_TOOL_ACTIONS,
    derive_step,
    extract_screenshot,
    is_playwright_tool,
)

# A 1x1 PNG: enough for the signature check the ingest path applies.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _derive(tool_name: str, arguments: dict, **overrides) -> BrowserStepIn | None:
    kwargs = {
        "tool_name": tool_name,
        "arguments": arguments,
        "status": "succeeded",
        "correlation_id": "corr-1",
        "step_index": 3,
    }
    kwargs.update(overrides)
    return derive_step(**kwargs)


class TestToolMapping:
    def test_every_pinned_browser_tool_has_an_action(self):
        expected = {
            "browser_navigate": "navigate",
            "browser_navigate_back": "navigate",
            "browser_click": "click",
            "browser_type": "type",
            "browser_press_key": "type",
            "browser_select_option": "select",
            "browser_hover": "other",
            "browser_take_screenshot": "screenshot",
            "browser_snapshot": "extract",
            "browser_wait_for": "wait",
            "browser_close": "done",
        }
        assert PLAYWRIGHT_TOOL_ACTIONS == expected
        assert all(is_playwright_tool(name) for name in expected)

    @pytest.mark.parametrize("name", ["get_issue", "browser", "navigate", ""])
    def test_other_tools_are_not_playwright(self, name):
        assert not is_playwright_tool(name)


class TestDeriveStep:
    def test_navigate_copies_the_url_and_joins_on_the_correlation_id(self):
        step = _derive(
            "browser_navigate",
            {"url": "https://app.example.com/inbox?token=abc"},
            correlation_id="corr-42",
            step_index=7,
        )

        assert step is not None
        assert step.source == "playwright_mcp"
        assert step.source_step_id == "corr-42"
        assert step.step_index == 7
        assert step.action == "navigate"
        assert step.url == "https://app.example.com/inbox?token=abc"
        assert step.target is None
        assert step.reasoning is None
        assert step.status == "success"
        assert step.extra == {"tool": "browser_navigate"}
        assert step.screenshot is None

    def test_click_uses_the_element_description_then_the_ref(self):
        by_element = _derive("browser_click", {"element": "Inbox link", "ref": "e12"})
        by_ref = _derive("browser_click", {"ref": "e12"})

        assert by_element is not None and by_element.target == "Inbox link"
        assert by_ref is not None and by_ref.target == "e12"
        assert by_ref.action == "click"

    def test_typed_text_is_never_copied_only_its_length(self):
        step = _derive(
            "browser_type",
            {"element": "Password", "ref": "e3", "text": "hunter2", "submit": True},
        )

        assert step is not None
        dumped = step.model_dump_json()
        assert "hunter2" not in dumped
        assert step.extra == {"tool": "browser_type", "typed_chars": 7}
        assert step.target == "Password"

    def test_typed_length_is_zero_when_text_is_missing_or_not_a_string(self):
        missing = _derive("browser_type", {"element": "Search", "ref": "e1"})
        wrong_type = _derive("browser_type", {"ref": "e1", "text": ["a", "b"]})

        assert missing is not None and missing.extra["typed_chars"] == 0
        assert wrong_type is not None and wrong_type.extra["typed_chars"] == 0

    def test_key_presses_and_option_values_are_not_copied(self):
        pressed = _derive("browser_press_key", {"key": "s3cr3t-Enter"})
        selected = _derive(
            "browser_select_option",
            {"element": "Plan", "ref": "e9", "values": ["enterprise-sso-token"]},
        )

        assert pressed is not None and pressed.action == "type"
        assert "s3cr3t" not in pressed.model_dump_json()
        assert "typed_chars" not in pressed.extra
        assert selected is not None and selected.action == "select"
        assert "enterprise-sso-token" not in selected.model_dump_json()
        assert selected.target == "Plan"

    def test_every_mapped_tool_derives_its_action(self):
        for tool_name, action in PLAYWRIGHT_TOOL_ACTIONS.items():
            step = _derive(tool_name, {})
            assert step is not None, tool_name
            assert step.action == action
            assert step.extra["tool"] == tool_name

    def test_unmapped_tool_derives_nothing(self):
        assert _derive("get_issue", {"issue": "ABC-1"}) is None
        assert _derive("browser_resize", {"width": 800}) is None

    @pytest.mark.parametrize(
        ("status", "expected"),
        [("succeeded", "success"), ("success", "success"), ("failed", "failed")],
    )
    def test_tool_call_status_maps_to_step_status(self, status, expected):
        step = _derive("browser_snapshot", {}, status=status)
        assert step is not None and step.status == expected

    def test_a_refused_call_derives_nothing(self):
        assert _derive("browser_navigate", {"url": "x"}, status="refused") is None

    def test_arguments_are_bounded_and_tolerate_odd_shapes(self):
        long_url = "https://example.com/" + "a" * 5000
        step = _derive("browser_navigate", {"url": long_url})
        assert step is not None and len(step.url or "") == 2048

        odd = _derive("browser_click", {"element": 12, "ref": "  "})
        assert odd is not None and odd.target is None

        none_args = _derive("browser_navigate", None)  # type: ignore[arg-type]
        assert none_args is not None and none_args.url is None

        negative = _derive("browser_close", {}, step_index=-5)
        assert negative is not None and negative.step_index == 0


class TestExtractScreenshot:
    def test_first_image_is_decoded_with_its_media_type(self):
        result = [
            types.TextContent(type="text", text="Took the viewport screenshot"),
            types.ImageContent(
                type="image",
                data=base64.b64encode(PNG_1X1).decode("ascii"),
                mimeType="image/png",
            ),
            types.ImageContent(
                type="image",
                data=base64.b64encode(b"\xff\xd8\xffsecond").decode("ascii"),
                mimeType="image/jpeg",
            ),
        ]

        assert extract_screenshot(result) == ("image/png", PNG_1X1)

    def test_call_tool_result_shape_is_accepted(self):
        image = types.ImageContent(
            type="image",
            data=base64.b64encode(PNG_1X1).decode("ascii"),
            mimeType="image/png",
        )
        wrapped = types.CallToolResult(content=[image])

        assert extract_screenshot(wrapped) == ("image/png", PNG_1X1)

    def test_missing_media_type_defaults_to_png(self):
        item = SimpleNamespace(
            type="image", data=base64.b64encode(PNG_1X1).decode("ascii"), mimeType=None
        )

        assert extract_screenshot([item]) == ("image/png", PNG_1X1)

    def test_text_only_or_empty_results_have_no_screenshot(self):
        assert extract_screenshot([types.TextContent(type="text", text="ok")]) is None
        assert extract_screenshot([]) is None
        assert extract_screenshot(None) is None
        assert extract_screenshot("a stringified result") is None

    def test_bad_base64_or_empty_payload_is_ignored(self):
        bad = SimpleNamespace(type="image", data="not base64!", mimeType="image/png")
        empty = SimpleNamespace(type="image", data="", mimeType="image/png")

        assert extract_screenshot([bad]) is None
        assert extract_screenshot([empty]) is None

    def test_an_oversized_payload_is_refused_before_it_is_decoded(self, monkeypatch):
        from preloop.config import settings
        from preloop.services import playwright_steps

        monkeypatch.setattr(settings, "runtime_session_screenshot_max_bytes", 64)
        decode = MagicMock(side_effect=AssertionError("must not decode"))
        monkeypatch.setattr(playwright_steps.base64, "b64decode", decode)
        # 128 bytes encode to 172 characters; the 64-byte cap allows 88.
        too_big = SimpleNamespace(
            type="image",
            data=base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 120).decode("ascii"),
            mimeType="image/png",
        )

        assert extract_screenshot([too_big]) is None
        decode.assert_not_called()

    def test_a_payload_at_the_cap_is_still_decoded(self, monkeypatch):
        from preloop.config import settings

        monkeypatch.setattr(settings, "runtime_session_screenshot_max_bytes", 64)
        at_cap = b"\x89PNG\r\n\x1a\n" + b"\0" * 56
        item = SimpleNamespace(
            type="image",
            data=base64.b64encode(at_cap).decode("ascii"),
            mimeType="image/png",
        )

        assert extract_screenshot([item]) == ("image/png", at_cap)


class TestScreenshotBytesError:
    """The firewall validates decoded bytes with the API's rules, no re-encode."""

    def test_accepts_a_declared_image_under_the_cap(self):
        assert screenshot_bytes_error("image/png", PNG_1X1) is None

    def test_refuses_size_type_and_signature_like_the_api(self, monkeypatch):
        from preloop.config import settings

        assert screenshot_bytes_error("image/png", b"not a png") == "screenshot_invalid"
        assert screenshot_bytes_error("image/png", b"") == "screenshot_invalid"
        assert screenshot_bytes_error("image/gif", PNG_1X1) == "screenshot_invalid"
        assert screenshot_bytes_error("image/jpeg", PNG_1X1) == "screenshot_invalid"
        monkeypatch.setattr(
            settings, "runtime_session_screenshot_max_bytes", len(PNG_1X1) - 1
        )
        assert screenshot_bytes_error("image/png", PNG_1X1) == "screenshot_too_large"


class _NoCloseSession:
    """Hand the test session to code that closes its own db handle."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def __contains__(self, obj):
        return obj in self._session

    def close(self):
        return None


def _session(db_session, account_id, source_id):
    started = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Browser Agent",
        started_at=started,
        last_activity_at=started,
    )


def _browser_rows(db_session, session_id):
    return (
        db_session.query(models.RuntimeSessionActivity)
        .filter(
            models.RuntimeSessionActivity.runtime_session_id == session_id,
            models.RuntimeSessionActivity.activity_type == "browser_step",
        )
        .order_by(models.RuntimeSessionActivity.timestamp.asc())
        .all()
    )


class TestNextBrowserStepIndex:
    def test_an_empty_session_starts_at_zero(self, db_session, test_user):
        session = _session(db_session, test_user.account_id, "pw-index-empty")

        assert (
            crud_runtime_session_activity.next_browser_step_index(
                db_session, runtime_session_id=session.id
            )
            == 0
        )

    def test_continues_after_the_highest_stored_index_of_any_source(
        self, db_session, test_user
    ):
        session = _session(db_session, test_user.account_id, "pw-index-mixed")
        other = _session(db_session, test_user.account_id, "pw-index-other")
        for source, step_id, index in (
            ("api", "adapter-1", 0),
            ("api", "adapter-2", 4),
            ("playwright_mcp", "corr-1", 2),
        ):
            crud_runtime_session_activity.log_browser_step(
                db_session,
                account_id=test_user.account_id,
                runtime_session_id=session.id,
                api_key_id=None,
                step=BrowserStepIn(
                    source=source,
                    source_step_id=step_id,
                    step_index=index,
                    action="click",
                ),
                commit=False,
            )
        crud_runtime_session_activity.log_browser_step(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=other.id,
            api_key_id=None,
            step=BrowserStepIn(
                source="api", source_step_id="x", step_index=40, action="click"
            ),
            commit=False,
        )
        db_session.flush()

        assert (
            crud_runtime_session_activity.next_browser_step_index(
                db_session, runtime_session_id=session.id
            )
            == 5
        )

    def test_concurrent_derivations_on_one_session_take_turns(self, db_engine):
        """A second transaction waits for the first to finish before reading.

        Two connections stand in for two concurrent tool calls. The second
        call to ``next_browser_step_index`` must block while the first
        transaction is open, and return once it rolls back. A different
        session is not held up.
        """
        session_id = uuid4()
        other_session_id = uuid4()
        first = Session(bind=db_engine.connect())
        second = Session(bind=db_engine.connect())
        third = Session(bind=db_engine.connect())
        finished = threading.Event()
        result: dict[str, int] = {}

        def read_second():
            result["index"] = crud_runtime_session_activity.next_browser_step_index(
                second, runtime_session_id=session_id
            )
            finished.set()

        try:
            assert (
                crud_runtime_session_activity.next_browser_step_index(
                    first, runtime_session_id=session_id
                )
                == 0
            )
            worker = threading.Thread(target=read_second, daemon=True)
            worker.start()
            # The other session's lock is independent of the held one.
            assert (
                crud_runtime_session_activity.next_browser_step_index(
                    third, runtime_session_id=other_session_id
                )
                == 0
            )
            assert not finished.wait(0.5), "second reader did not wait for the lock"
            first.rollback()
            assert finished.wait(5), "second reader never acquired the lock"
            assert result["index"] == 0
            worker.join(timeout=5)
        finally:
            for session in (first, second, third):
                session.rollback()
                bind = session.get_bind()
                session.close()
                bind.close()


class TestFirewallDerivationAgainstTheDatabase:
    """The firewall hook writes real rows: step, screenshot artifact, index."""

    def _context(self, test_user, session) -> UserContext:
        return UserContext(
            user_id=str(test_user.id),
            account_id=str(test_user.account_id),
            username="browser-agent",
            has_tracker=False,
            enabled_default_tools=[],
            enabled_proxied_tools=[],
            runtime_session_id=str(session.id),
        )

    def _persist(self, db_session, monkeypatch, context, **kwargs):
        monkeypatch.setattr(
            "preloop.services.dynamic_fastmcp.get_db",
            lambda: iter([_NoCloseSession(db_session)]),
        )
        DynamicFastMCP("test-mcp")._persist_playwright_browser_step(context, **kwargs)

    def test_a_screenshot_call_stores_the_step_and_its_image_once(
        self, db_session, test_user, monkeypatch
    ):
        session = _session(db_session, test_user.account_id, "pw-db-screenshot")
        context = self._context(test_user, session)
        correlation_id = str(uuid4())
        upstream = [
            types.TextContent(type="text", text="Took the viewport screenshot"),
            types.ImageContent(
                type="image",
                data=base64.b64encode(PNG_1X1).decode("ascii"),
                mimeType="image/png",
            ),
        ]
        call = {
            "client_tool_name": "browser_take_screenshot",
            "arguments": {"filename": "inbox.png"},
            "status": "succeeded",
            "correlation_id": correlation_id,
            "raw_result": upstream,
        }

        self._persist(db_session, monkeypatch, context, **call)
        # The same call again (a retried derivation) must not add a row.
        self._persist(db_session, monkeypatch, context, **call)

        rows = _browser_rows(db_session, session.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.tool_name == "screenshot"
        assert row.server_name == "playwright_mcp"
        assert row.status == "success"
        metadata = row.metadata_
        assert metadata["source_step_id"] == correlation_id
        assert metadata["step_index"] == 0
        assert metadata["extra"] == {"tool": "browser_take_screenshot"}
        assert metadata["screenshot"]["availability"] == "available"
        assert metadata["screenshot"]["content_type"] == "image/png"
        artifacts = (
            db_session.query(models.RuntimeSessionArtifact)
            .filter(models.RuntimeSessionArtifact.runtime_session_id == session.id)
            .all()
        )
        assert len(artifacts) == 1
        assert str(artifacts[0].id) == metadata["screenshot"]["artifact_id"]
        assert artifacts[0].activity_id == row.id
        assert artifacts[0].kind == "screenshot"

    def test_steps_number_consecutively_and_a_bad_image_keeps_the_step(
        self, db_session, test_user, monkeypatch
    ):
        session = _session(db_session, test_user.account_id, "pw-db-sequence")
        context = self._context(test_user, session)

        self._persist(
            db_session,
            monkeypatch,
            context,
            client_tool_name="browser_navigate",
            arguments={"url": "https://app.example.com/inbox"},
            status="succeeded",
            correlation_id="corr-nav",
            raw_result=[types.TextContent(type="text", text="ok")],
        )
        self._persist(
            db_session,
            monkeypatch,
            context,
            client_tool_name="browser_type",
            arguments={"element": "Search", "ref": "e2", "text": "hunter2"},
            status="failed",
            correlation_id="corr-type",
            raw_result=None,
        )
        # Declared as PNG but not a PNG: the step is kept, the image is not.
        self._persist(
            db_session,
            monkeypatch,
            context,
            client_tool_name="browser_take_screenshot",
            arguments={},
            status="succeeded",
            correlation_id="corr-shot",
            raw_result=[
                types.ImageContent(
                    type="image",
                    data=base64.b64encode(b"not an image").decode("ascii"),
                    mimeType="image/png",
                )
            ],
        )

        rows = _browser_rows(db_session, session.id)
        assert [r.metadata_["step_index"] for r in rows] == [0, 1, 2]
        assert [r.tool_name for r in rows] == ["navigate", "type", "screenshot"]
        assert rows[0].metadata_["url"] == "https://app.example.com/inbox"
        assert rows[1].status == "failed"
        assert "hunter2" not in str(rows[1].metadata_)
        assert rows[1].metadata_["extra"]["typed_chars"] == 7
        assert rows[2].metadata_["screenshot"] is None
        assert (
            db_session.query(models.RuntimeSessionArtifact)
            .filter(models.RuntimeSessionArtifact.runtime_session_id == session.id)
            .count()
            == 0
        )

    def test_a_refused_call_or_a_disabled_setting_writes_nothing(
        self, db_session, test_user, monkeypatch
    ):
        from preloop.config import settings

        session = _session(db_session, test_user.account_id, "pw-db-skip")
        context = self._context(test_user, session)
        base = {
            "client_tool_name": "browser_navigate",
            "arguments": {"url": "https://app.example.com"},
            "raw_result": None,
        }

        self._persist(
            db_session,
            monkeypatch,
            context,
            status="refused",
            correlation_id="corr-refused",
            **base,
        )
        monkeypatch.setattr(settings, "mcp_playwright_derive_browser_steps", False)
        self._persist(
            db_session,
            monkeypatch,
            context,
            status="succeeded",
            correlation_id="corr-off",
            **base,
        )

        assert _browser_rows(db_session, session.id) == []

    def test_a_context_without_a_session_never_opens_the_database(
        self, test_user, monkeypatch
    ):
        get_db = MagicMock()
        monkeypatch.setattr("preloop.services.dynamic_fastmcp.get_db", get_db)
        context = UserContext(
            user_id=str(test_user.id),
            account_id=str(test_user.account_id),
            username="no-session",
            has_tracker=False,
            enabled_default_tools=[],
            enabled_proxied_tools=[],
        )

        DynamicFastMCP("test-mcp")._persist_playwright_browser_step(
            context,
            client_tool_name="browser_navigate",
            arguments={"url": "https://app.example.com"},
            status="succeeded",
            correlation_id="corr-none",
            raw_result=None,
        )

        get_db.assert_not_called()
