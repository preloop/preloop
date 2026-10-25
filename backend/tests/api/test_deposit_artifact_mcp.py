"""deposit_artifact over the real MCP endpoint (#1081).

A client that knows only the Preloop MCP URL and a session-bound key talks
streamable HTTP to the app in process. The tool must be off until the account
enables it, store through the #1080 deposit service, and answer with an MCP
``resource_link`` plus the descriptor as ``structuredContent``.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import contextlib
import types
from typing import Any
import uuid

import httpx2 as httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_key
from preloop.services import artifact_deposit, artifact_mcp_tools
from preloop.services.artifact_media import ARTIFACT_KINDS
from preloop.tools.builtin_defs import DEPOSIT_ARTIFACT_KINDS, DEPOSIT_ARTIFACT_TOOL
from tests.api.test_artifact_deposit import _session, _token, _vtt

TOOL = "deposit_artifact"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def shared_db(db_session, monkeypatch):
    """Every get_db the MCP stack opens joins the test transaction."""
    from preloop.services.audit import get_audit_service

    monkeypatch.setattr(get_audit_service(), "enabled", True)

    def _write_audit(
        db_factory: Callable[[], Session],
        operation: Callable[..., None],
        *,
        priority: bool = False,
        allow_without_loop: bool = False,
        **fields: Any,
    ) -> None:
        # The protocol fixture owns one uncommitted Connection. Real audit
        # workers own independent connections; sharing this one across threads
        # races its savepoints, while independent workers cannot see its rows.
        # Keep real audit persistence, serialized inside the test transaction.
        operation(db=db_session, **fields)

    monkeypatch.setattr(
        "preloop.services.audit.service.schedule_audit_write", _write_audit
    )

    def _factory():
        session = Session(bind=db_session.connection())
        try:
            yield session
        finally:
            session.close()

    for target in (
        "preloop.models.db.session.get_db_session",
        "preloop.services.mcp_http.get_db",
        "preloop.services.dynamic_fastmcp.get_db",
    ):
        monkeypatch.setattr(target, _factory)
    _drop_async_pool()
    yield db_session
    _drop_async_pool()


def _drop_async_pool():
    """Pooled asyncpg connections belong to the loop that opened them."""
    from preloop.models.db.session import get_async_engine_if_initialized

    engine = get_async_engine_if_initialized()
    if engine is not None:
        engine.sync_engine.dispose(close=False)


@contextlib.asynccontextmanager
async def _mcp(app, token, *, modern=False, path="/mcp/v1"):
    """An MCP client session against either alias of the in-process app."""

    def _client(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers=headers,
            timeout=timeout or httpx.Timeout(30),
            auth=auth,
        )

    async with _mcp_lifespan(app):
        async with (
            _client(headers={"Authorization": f"Bearer {token}"}) as http_client,
            streamable_http_client(
                f"http://localhost{path}", http_client=http_client
            ) as (read, write),
        ):
            async with ClientSession(read, write) as session:
                if modern:
                    await session.discover()
                else:
                    await session.initialize()
                yield session


def _mcp_lifespan(app):
    """A fresh lifespan of the mounted MCP app (the stored one is one-shot)."""
    mount = next(r for r in app.routes if getattr(r, "path", None) == "/mcp")
    inner = mount.app
    while hasattr(inner, "app"):
        inner = inner.app
        if hasattr(inner, "lifespan"):
            return inner.lifespan(inner)
    raise AssertionError("MCP app has no lifespan")


def _enable(db, account_id):
    db.add(
        models.ToolConfiguration(
            account_id=account_id,
            tool_name=TOOL,
            tool_source="builtin",
            is_enabled=True,
        )
    )
    db.flush()


def test_catalog_entry_is_compact_and_default_off():
    assert DEPOSIT_ARTIFACT_TOOL["default_enabled"] is False
    assert tuple(DEPOSIT_ARTIFACT_KINDS) == tuple(ARTIFACT_KINDS)
    assert (
        "visible in the Preloop session timeline"
        in (DEPOSIT_ARTIFACT_TOOL["description"])
    )
    schema = DEPOSIT_ARTIFACT_TOOL["schema"]
    assert schema["required"] == ["content", "name"]
    assert schema["properties"]["name"]["maxLength"] == 255


@pytest.mark.asyncio
@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/v1"])
async def test_document_larger_than_four_mib_through_mcp(
    app, shared_db, test_user, monkeypatch, modern, path
) -> None:
    """The protocol guard must retain the existing artifact size contract."""
    db = shared_db
    runtime = _session(db, test_user.account_id, "large-mcp-document")
    token = _token(db, test_user, runtime_session_id=runtime.id)
    _enable(db, test_user.account_id)
    monkeypatch.setattr(artifact_deposit, "ON_STORED", [])
    text = "sample document\n" * (4 * 1024**2 // 16 + 1)
    assert len(text.encode()) > 4 * 1024**2

    async with _mcp(app, token, modern=modern, path=path) as mcp:
        result = await mcp.call_tool(
            TOOL,
            {
                "name": "large-document.txt",
                "kind": "document",
                "content": {"type": "text", "text": text},
            },
        )
    assert not result.is_error, result.content
    assert result.structured_content["size_bytes"] == len(text.encode())
    assert result.structured_content["kind"] == "document"
    assert result.content[0].type == "resource_link"


@pytest.mark.asyncio
@pytest.mark.parametrize("modern", [False, True])
async def test_transcript_and_summary_through_mcp(
    app, shared_db, test_user, client, modern
):
    db = shared_db
    session = _session(db, test_user.account_id, "mcp-1081")
    token = _token(db, test_user, runtime_session_id=session.id)

    async with _mcp(app, token, modern=modern) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert TOOL not in names

    _enable(db, test_user.account_id)

    vtt = _vtt(4 * 1024)
    async with _mcp(app, token, modern=modern) as mcp:
        listed = {t.name: t for t in (await mcp.list_tools()).tools}
        assert TOOL in listed
        assert listed[TOOL].description == DEPOSIT_ARTIFACT_TOOL["description"]

        transcript = await mcp.call_tool(
            TOOL,
            {
                "name": "standup.vtt",
                "content": {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///standup.vtt",
                        "mimeType": "text/vtt",
                        "text": vtt,
                    },
                },
                "labels": {"source_tool": "whisper", "site": "nord"},
            },
        )
        assert not transcript.is_error, transcript.content
        link = transcript.content[0]
        assert link.type == "resource_link"
        assert link.mime_type == "text/vtt"
        assert link.size == len(vtt.encode())
        assert link.name == "standup.vtt"
        transcript_id = transcript.structured_content["id"]
        assert str(link.uri) == artifact_mcp_tools.absolute_uri(
            artifact_deposit.artifact_uri(session.id, transcript_id)
        )
        assert transcript.structured_content["content_block"]["uri"] == str(link.uri)
        assert transcript.structured_content["kind"] == "transcript"
        assert transcript.structured_content["producer"] == "deposit_mcp"
        assert transcript.structured_content["tool_name"] == "whisper"

        summary = await mcp.call_tool(
            TOOL,
            {
                "name": "standup summary",
                "kind": "document",
                "content": {"type": "text", "text": "Picker 4 is short in aisle 12."},
                "parent_artifact_id": transcript_id,
            },
        )
        assert not summary.is_error, summary.content
        assert summary.structured_content["parent_artifact_id"] == transcript_id
        summary_id = summary.structured_content["id"]

    headers = {"Authorization": f"Bearer {token}"}
    listed = client.get(
        f"/api/v1/runtime-sessions/{session.id}/artifacts", headers=headers
    )
    assert listed.status_code == 200, listed.text
    ids = [row["id"] for row in listed.json()["items"]]
    assert set(ids) >= {transcript_id, summary_id}

    activities = (
        db.query(models.RuntimeSessionActivity)
        .filter(
            models.RuntimeSessionActivity.runtime_session_id == session.id,
            models.RuntimeSessionActivity.activity_type == "artifact",
        )
        .all()
    )
    linked = {a.metadata_["artifact"]["id"] for a in activities}
    assert linked >= {transcript_id, summary_id}


@pytest.mark.asyncio
async def test_relabel_by_resource_link_and_refusals(app, shared_db, test_user):
    db = shared_db
    session = _session(db, test_user.account_id, "mcp-1081-link")
    other = _session(db, test_user.account_id, "mcp-1081-other")
    token = _token(db, test_user, runtime_session_id=session.id)
    unbound = _token(db, test_user)
    _enable(db, test_user.account_id)

    async with _mcp(app, token) as mcp:
        shot = await mcp.call_tool(
            TOOL,
            {
                "name": "aisle.png",
                "content": {
                    "type": "image",
                    "mimeType": "image/png",
                    "data": base64.b64encode(PNG).decode(),
                },
            },
        )
        assert not shot.is_error, shot.content
        assert shot.structured_content["kind"] == "screenshot"
        source_id = shot.structured_content["id"]

        relabel = await mcp.call_tool(
            TOOL,
            {
                "name": "aisle 12 evidence",
                "content": {"type": "resource_link", **_link(shot)},
                "labels": {"case": "short-pick"},
            },
        )
        assert not relabel.is_error, relabel.content
        out = relabel.structured_content
        assert out["parent_artifact_id"] == source_id
        assert out["sha256"] == shot.structured_content["sha256"]
        assert out["labels"] == {"case": "short-pick"}

        foreign = await mcp.call_tool(
            TOOL,
            {
                "name": "x",
                "content": {
                    "type": "resource_link",
                    "uri": artifact_deposit.artifact_uri(other.id, uuid.uuid4()),
                    "name": "x",
                },
            },
        )
        assert foreign.is_error
        assert foreign.content[0].text.startswith("artifact_link_outside_session")

        # Matches the link pattern, names this session, but is not a UUID.
        for malformed in ("-" * 36, "a" * 36):
            odd = await mcp.call_tool(
                TOOL,
                {
                    "name": "x",
                    "content": {
                        "type": "resource_link",
                        "uri": artifact_deposit.artifact_uri(session.id, malformed),
                        "name": "x",
                    },
                },
            )
            assert odd.is_error, malformed
            assert odd.content[0].text.startswith("artifact_link_outside_session")

        # urlparse raises ValueError on an invalid IPv6 netloc.
        unparsable = await mcp.call_tool(
            TOOL,
            {
                "name": "x",
                "content": {"type": "resource_link", "uri": "https://[", "name": "x"},
            },
        )
        assert unparsable.is_error
        assert unparsable.content[0].text.startswith("artifact_link_outside_session")
        assert (
            unparsable.structured_content["error"]["code"]
            == "artifact_link_outside_session"
        )

        bad = await mcp.call_tool(
            TOOL,
            {"name": "x", "content": {"type": "image", "data": "%%%"}},
        )
        assert bad.is_error
        assert (
            bad.content[0].text.split(":")[0] == bad.structured_content["error"]["code"]
        )

    async with _mcp(app, unbound) as mcp:
        refused = await mcp.call_tool(
            TOOL, {"name": "x", "content": {"type": "text", "text": "hi"}}
        )
        assert refused.is_error
        assert refused.content[0].text.startswith("artifact_no_session:")


def _link(result):
    block = result.content[0]
    return {"uri": str(block.uri), "name": block.name, "mimeType": block.mime_type}


def test_auth_context_only_trusts_a_key_of_the_users_own_account(
    db_session, test_user, test_viewer_user
):
    """The key binding is read through CRUD, scoped to the caller's account."""
    own_session = _session(db_session, test_user.account_id, "mcp-1081-own")
    foreign_session = _session(db_session, test_viewer_user.account_id, "mcp-1081-x")
    own_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="own",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"runtime_session_id": str(own_session.id)},
    )
    foreign_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="foreign",
        account_id=test_viewer_user.account_id,
        user_id=test_viewer_user.id,
        context_data={"runtime_session_id": str(foreign_session.id)},
    )

    def ctx(key):
        return types.SimpleNamespace(user_id=str(test_user.id), api_key_id=str(key.id))

    own = artifact_mcp_tools.auth_from_user_context(db_session, ctx(own_key))
    assert str(own.runtime_session_id) == str(own_session.id)
    foreign = artifact_mcp_tools.auth_from_user_context(db_session, ctx(foreign_key))
    assert foreign.api_key is None
    assert foreign.runtime_session_id is None

    with pytest.raises(artifact_deposit.ArtifactDepositError):
        artifact_mcp_tools.auth_from_user_context(
            db_session, types.SimpleNamespace(user_id="not-a-uuid", api_key_id=None)
        )


@pytest.mark.parametrize(
    "code",
    [
        artifact_deposit.ERROR_TOO_LARGE,
        artifact_deposit.ERROR_AUDIO_DISABLED,
        artifact_deposit.ERROR_CONTENT_REQUIRED,
        artifact_deposit.ERROR_ACTIVITY_INVALID,
        "artifact_media_type_invalid",
        "artifact_content_mismatch",
        "storage_budget_exhausted",
        "runtime_session_not_found",
        "runtime_session_binding_mismatch",
        "artifact_parent_invalid",
    ],
)
def test_every_deposit_error_code_is_the_tool_error_code(code, monkeypatch):
    """Whatever #1080 raises surfaces with the same code string."""

    def _raise(*_a, **_k):
        raise artifact_deposit.ArtifactDepositError.from_code(code)

    monkeypatch.setattr(artifact_deposit, "deposit", _raise)
    monkeypatch.setattr(
        artifact_mcp_tools,
        "auth_from_user_context",
        lambda _db, _ctx: type("A", (), {"runtime_session_id": str(uuid.uuid4())}),
    )
    outcome = artifact_mcp_tools.deposit_from_mcp(
        None,
        user_context=None,
        arguments={"name": "n", "content": {"type": "text", "text": "t"}},
    )
    assert outcome.is_error
    assert outcome.text.split(":")[0] == code
    assert outcome.structured["error"]["code"] == code


@pytest.mark.asyncio
async def test_a_flow_opts_in_through_allowed_mcp_tools(app, shared_db, test_user):
    """No account enable: the flow's allow-list alone offers and runs it."""
    db = shared_db
    session = _session(db, test_user.account_id, "mcp-1081-flow")
    token = _token(
        db,
        test_user,
        runtime_session_id=session.id,
        context={"allowed_mcp_tools": [{"name": TOOL}]},
    )
    async with _mcp(app, token) as mcp:
        assert TOOL in {t.name for t in (await mcp.list_tools()).tools}
        result = await mcp.call_tool(
            TOOL, {"name": "note.md", "content": {"type": "text", "text": "# ok"}}
        )
        assert not result.is_error, result.content


@pytest.mark.asyncio
async def test_media_timing_through_mcp(
    app: Any, shared_db: Any, test_user: Any
) -> None:
    import base64

    db = shared_db
    session = _session(db, test_user.account_id, "mcp-recording")
    token = _token(db, test_user, runtime_session_id=session.id)
    _enable(db, test_user.account_id)
    timing = {
        "started_at": "2026-10-01T00:00:00Z",
        "clock": "agent",
        "step_offsets": [{"source_step_id": "step-1", "offset_ms": 2000}],
    }
    async with _mcp(app, token) as mcp:
        result = await mcp.call_tool(
            TOOL,
            {
                "name": "capture.webm",
                "kind": "recording",
                "media_timing": timing,
                "content": {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///capture.webm",
                        "mimeType": "video/webm",
                        "blob": base64.b64encode(
                            b"\x1a\x45\xdf\xa3" + b"\0" * 32
                        ).decode(),
                    },
                },
            },
        )
    assert not result.is_error
    assert (
        result.structured_content["media_timing"]["step_offsets"]
        == timing["step_offsets"]
    )
