"""search_artifacts and get_artifact (#1104): scope, window, truncation, audit.

Service-level tests call :mod:`preloop.services.agent_artifact_read` with the
identity the MCP layer derives from the credential; the last test drives both
tools over the real MCP endpoint.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from preloop.models import models
from preloop.models.crud import crud_account, crud_runtime_session
from preloop.models.crud import runtime_session_artifact as crud_artifact
from preloop.services import agent_artifact_read as reads
from preloop.services import artifact_shapes as shapes
from preloop.services.session_search_index import index_artifact_text
from preloop.tools.builtin_defs import GET_ARTIFACT_TOOL, SEARCH_ARTIFACTS_TOOL
from tests.api.test_deposit_artifact_mcp import shared_db  # noqa: F401

NOW = datetime.now(UTC).replace(microsecond=0)
TODAY = NOW - timedelta(hours=1)
YESTERDAY = NOW - timedelta(days=1)
GRANT = "artifact_search.account_scope"


def _session(db, account_id, source_id, principal, started_at=TODAY):
    return crud_runtime_session.upsert_by_source(
        db,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id=principal,
        runtime_principal_name=principal,
        started_at=started_at,
        last_activity_at=started_at,
    )


def _artifact(
    db,
    session,
    *,
    name,
    created_at,
    kind="transcript",
    content_type="text/vtt",
    body=None,
    labels=None,
):
    body = (
        body
        if body is not None
        else (
            f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n{name}: damaged pallet in aisle 12\n"
        ).encode()
    )
    row = crud_artifact.store(
        db,
        account_id=session.account_id,
        runtime_session_id=session.id,
        kind=kind,
        source="test",
        source_ref=None,
        content_type=content_type,
        plaintext=body,
        manifest={},
        name=name,
        labels=labels,
        producer="deposit_api",
        commit=False,
    )
    row.created_at = created_at
    db.flush()
    index_artifact_text(db, row, commit=False)
    db.flush()
    return row


def _caller(account_id, principal, *, api_key_id=None):
    return reads.Caller(
        account_id=account_id,
        runtime_principal_id=principal,
        api_key_id=api_key_id,
        managed_agent_id=None,
    )


def _grant(db, account_id, value=True):
    account = crud_account.get(db, id=account_id)
    meta = dict(account.meta_data or {})
    store = dict(meta.get("subject_governance") or {})
    store["account_defaults"] = {"tool_grants": {GRANT: value}}
    meta["subject_governance"] = store
    account.meta_data = meta
    db.flush()


@pytest.fixture
def corpus(db_session, test_user):
    db = db_session
    account_id = test_user.account_id
    a_yesterday = _session(db, account_id, "a-1", "agent-a", YESTERDAY)
    a_today = _session(db, account_id, "a-2", "agent-a", TODAY)
    b_today = _session(db, account_id, "b-1", "agent-b", TODAY)
    rows = {
        "a_yesterday": _artifact(
            db,
            a_yesterday,
            name="a-yesterday.vtt",
            created_at=YESTERDAY,
            labels={"site": "nord"},
        ),
        "a_today": _artifact(
            db,
            a_today,
            name="a-today.vtt",
            created_at=TODAY,
            labels={"site": "sued"},
        ),
        "b_today": _artifact(
            db,
            b_today,
            name="b-today.vtt",
            created_at=TODAY,
            labels={"site": "nord"},
        ),
    }
    return rows


def _names(outcome):
    assert not outcome.is_error, outcome.text
    return sorted(item["name"] for item in outcome.structured["items"])


def _audit_rows(db, account_id, action):
    return (
        db.query(models.AuditLog)
        .filter(
            models.AuditLog.account_id == account_id,
            models.AuditLog.resource_type == reads.AUDIT_RESOURCE_TYPE,
            models.AuditLog.action == action,
        )
        .all()
    )


def _uses_known_governance_key():
    """The grant has to sit where subject_governance reads defaults."""
    from preloop.services.subject_governance import get_account_governance_defaults

    meta = {"subject_governance": {"account_defaults": {"tool_grants": {GRANT: True}}}}
    return get_account_governance_defaults(meta)


def test_grant_fixture_matches_the_governance_store():
    assert _uses_known_governance_key()["tool_grants"][GRANT] is True


def test_catalog_entries_are_default_off():
    assert SEARCH_ARTIFACTS_TOOL["default_enabled"] is False
    assert GET_ARTIFACT_TOOL["default_enabled"] is False
    assert SEARCH_ARTIFACTS_TOOL["schema"]["properties"]["limit"]["maximum"] == 50
    assert GET_ARTIFACT_TOOL["schema"]["required"] == ["artifact_id"]


class TestScope:
    def test_own_scope_sees_own_sessions_across_runs_only(
        self, db_session, test_user, corpus
    ):
        outcome = reads.search(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"kind": ["transcript"]},
        )
        assert _names(outcome) == ["a-today.vtt", "a-yesterday.vtt"]
        assert outcome.structured["scope"] == "own"
        assert all(b["type"] == "resource_link" for b in outcome.blocks)
        assert {b["_meta"][shapes.META_KEY]["artifact_id"] for b in outcome.blocks} == {
            str(corpus["a_today"].id),
            str(corpus["a_yesterday"].id),
        }

    def test_account_scope_refused_without_grant_naming_it(
        self, db_session, test_user, corpus
    ):
        outcome = reads.search(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"scope": "account"},
        )
        assert outcome.is_error
        assert outcome.text.startswith("account_scope_not_granted")
        assert GRANT in outcome.text
        denied = [
            r
            for r in _audit_rows(db_session, test_user.account_id, "query")
            if r.status == "denied"
        ]
        assert denied and denied[-1].details["reason"] == "account_scope_not_granted"
        assert denied[-1].details["source"] == "mcp"

    def test_account_scope_with_grant_sees_both_agents(
        self, db_session, test_user, corpus
    ):
        _grant(db_session, test_user.account_id)
        outcome = reads.search(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"scope": "account"},
        )
        assert _names(outcome) == ["a-today.vtt", "a-yesterday.vtt", "b-today.vtt"]

    def test_explicit_false_on_the_key_beats_the_account_default(
        self, db_session, test_user, corpus
    ):
        _grant(db_session, test_user.account_id)
        key_id = str(uuid.uuid4())
        account = crud_account.get(db_session, id=test_user.account_id)
        meta = dict(account.meta_data)
        store = dict(meta["subject_governance"])
        store["api_keys"] = {key_id: {"tool_grants": {GRANT: False}}}
        meta["subject_governance"] = store
        account.meta_data = meta
        db_session.flush()
        from preloop.services.agent_session_search import account_scope_granted

        assert not account_scope_granted(
            meta, subject_context={"api_key_id": key_id}, grant=GRANT
        )

    def test_session_search_grant_does_not_open_artifacts(
        self, db_session, test_user, corpus
    ):
        account = crud_account.get(db_session, id=test_user.account_id)
        account.meta_data = {
            "subject_governance": {
                "account_defaults": {
                    "tool_grants": {"session_search.account_scope": True}
                }
            }
        }
        db_session.flush()
        outcome = reads.search(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"scope": "account"},
        )
        assert outcome.text.startswith("account_scope_not_granted")

    def test_no_agent_identity_is_refused_for_own_scope(
        self, db_session, test_user, corpus
    ):
        outcome = reads.search(
            db_session, caller=_caller(test_user.account_id, None), arguments={}
        )
        assert outcome.text.startswith("no_agent_identity")

    def test_other_account_never_visible(self, db_session, test_user, corpus):
        other = crud_account.create(
            db_session, obj_in={"organization_name": f"other-{uuid.uuid4().hex[:6]}"}
        )
        session = _session(db_session, other.id, "x-1", "agent-a")
        _artifact(db_session, session, name="foreign.vtt", created_at=TODAY)
        _grant(db_session, test_user.account_id)
        outcome = reads.search(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"scope": "account"},
        )
        assert "foreign.vtt" not in _names(outcome)


class TestFilters:
    def test_window_since_inclusive_until_exclusive(
        self, db_session, test_user, corpus
    ):
        caller = _caller(test_user.account_id, "agent-a")
        exact = reads.search(
            db_session,
            caller=caller,
            arguments={
                "since": TODAY.isoformat(),
                "until": (TODAY + timedelta(seconds=1)).isoformat(),
            },
        )
        assert _names(exact) == ["a-today.vtt"]
        ends_at = reads.search(
            db_session,
            caller=caller,
            arguments={
                "since": YESTERDAY.isoformat(),
                "until": TODAY.isoformat(),
            },
        )
        assert _names(ends_at) == ["a-yesterday.vtt"]
        just_after = reads.search(
            db_session,
            caller=caller,
            arguments={"since": (TODAY + timedelta(microseconds=1)).isoformat()},
        )
        assert _names(just_after) == []

    def test_naive_timestamp_is_refused(self, db_session, test_user, corpus):
        outcome = reads.search(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"since": "2026-10-04T00:00:00"},
        )
        assert outcome.text.startswith("invalid_request")
        assert "offset" in outcome.text

    def test_labels_and_empty_label_value(self, db_session, test_user, corpus):
        caller = _caller(test_user.account_id, "agent-a")
        nord = reads.search(
            db_session, caller=caller, arguments={"labels": {"site": "nord"}}
        )
        assert _names(nord) == ["a-yesterday.vtt"]
        any_site = reads.search(
            db_session, caller=caller, arguments={"labels": {"site": ""}}
        )
        assert _names(any_site) == ["a-today.vtt", "a-yesterday.vtt"]

    def test_kind_filter_and_unknown_kind(self, db_session, test_user, corpus):
        caller = _caller(test_user.account_id, "agent-a")
        assert (
            _names(
                reads.search(
                    db_session, caller=caller, arguments={"kind": ["document"]}
                )
            )
            == []
        )
        bad = reads.search(db_session, caller=caller, arguments={"kind": ["memo"]})
        assert bad.text.startswith("invalid_request")

    def test_query_matches_text_with_excerpt(self, db_session, test_user, corpus):
        outcome = reads.search(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"q": "pallet"},
        )
        items = outcome.structured["items"]
        assert sorted(i["name"] for i in items) == ["a-today.vtt", "a-yesterday.vtt"]
        assert all("**pallet**" in (i["excerpt"] or "") for i in items)

    def test_cursor_pages_without_overlap(self, db_session, test_user, corpus):
        caller = _caller(test_user.account_id, "agent-a")
        first = reads.search(db_session, caller=caller, arguments={"limit": 1})
        assert first.structured["next_cursor"]
        second = reads.search(
            db_session,
            caller=caller,
            arguments={"limit": 1, "cursor": first.structured["next_cursor"]},
        )
        assert _names(first) == ["a-today.vtt"]
        assert _names(second) == ["a-yesterday.vtt"]
        assert second.structured["next_cursor"] is None

    def test_limit_is_clamped(self, db_session, test_user, corpus):
        outcome = reads.search(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"limit": 500},
        )
        assert outcome.structured["returned"] == 2

    def test_every_search_is_audited(self, db_session, test_user, corpus):
        reads.search(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"kind": ["transcript"]},
        )
        row = [
            r
            for r in _audit_rows(db_session, test_user.account_id, "query")
            if r.status == "success"
        ][-1]
        assert row.details["result_count"] == 2
        assert row.details["actor_runtime_principal_id"] == "agent-a"
        assert row.details["scope"] == "own"


class TestGetArtifact:
    def test_large_transcript_returns_first_64_kib_marked_truncated(
        self, db_session, test_user
    ):
        session = _session(db_session, test_user.account_id, "big", "agent-a")
        cue = "00:00:01.000 --> 00:00:02.000\nPicker 4 short in aisle 12.\n\n"
        body = ("WEBVTT\n\n" + cue * ((3 * 1024**2) // len(cue) + 1)).encode()
        assert len(body) >= 3 * 1024**2
        row = _artifact(
            db_session, session, name="big.vtt", created_at=TODAY, body=body
        )
        outcome = reads.get(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"artifact_id": str(row.id)},
        )
        assert not outcome.is_error, outcome.text
        (block,) = outcome.blocks
        assert block["type"] == "resource"
        assert block["resource"]["mimeType"] == "text/vtt"
        assert len(block["resource"]["text"].encode()) == 64 * 1024
        meta = block["_meta"][shapes.META_KEY]
        assert meta["truncated"] is True
        assert meta["sha256"] == row.sha256
        assert meta["size_bytes"] == len(body)
        assert outcome.structured["truncated"] is True

    def test_small_text_is_whole_and_not_truncated(self, db_session, test_user, corpus):
        outcome = reads.get(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"artifact_id": str(corpus["a_today"].id), "max_bytes": 4096},
        )
        block = outcome.blocks[0]
        assert block["resource"]["text"].startswith("WEBVTT")
        assert block["_meta"][shapes.META_KEY]["truncated"] is False

    def test_small_binary_inline_large_binary_linked(self, db_session, test_user):
        session = _session(db_session, test_user.account_id, "bin", "agent-a")
        pdf_small = b"%PDF-1.4\n" + b"0" * 1000
        small = _artifact(
            db_session,
            session,
            name="s.pdf",
            created_at=TODAY,
            kind="document",
            content_type="application/pdf",
            body=pdf_small,
        )
        large = _artifact(
            db_session,
            session,
            name="l.pdf",
            created_at=TODAY,
            kind="document",
            content_type="application/pdf",
            body=b"%PDF-1.4\n" + b"0" * (2 * 1024**2),
        )
        caller = _caller(test_user.account_id, "agent-a")
        inline = reads.get(
            db_session, caller=caller, arguments={"artifact_id": str(small.id)}
        ).blocks[0]
        assert inline["type"] == "resource"
        assert base64.b64decode(inline["resource"]["blob"]) == pdf_small
        linked = reads.get(
            db_session, caller=caller, arguments={"artifact_id": str(large.id)}
        ).blocks[0]
        assert linked["type"] == "resource_link"
        assert linked["uri"].endswith(f"/artifacts/{large.id}")

    def test_other_agents_artifact_is_not_found_without_grant(
        self, db_session, test_user, corpus
    ):
        caller = _caller(test_user.account_id, "agent-a")
        args = {"artifact_id": str(corpus["b_today"].id)}
        refused = reads.get(db_session, caller=caller, arguments=args)
        assert refused.text.startswith("artifact_not_found")
        assert GRANT in refused.text
        _grant(db_session, test_user.account_id)
        allowed = reads.get(db_session, caller=caller, arguments=args)
        assert not allowed.is_error
        rows = _audit_rows(db_session, test_user.account_id, "read")
        assert [r.status for r in rows][-2:] == ["denied", "success"]
        assert rows[-1].details["scope"] == "account"

    def test_bad_id_is_invalid_request(self, db_session, test_user):
        outcome = reads.get(
            db_session,
            caller=_caller(test_user.account_id, "agent-a"),
            arguments={"artifact_id": "nope"},
        )
        assert outcome.text.startswith("invalid_request")


@pytest.mark.asyncio
async def test_both_tools_over_mcp(app, test_user, shared_db):  # noqa: F811
    from tests.api.test_artifact_deposit import _token
    from tests.api.test_deposit_artifact_mcp import _mcp

    db = shared_db
    session = _session(db, test_user.account_id, "mcp-a", "agent-a")
    row = _artifact(db, session, name="mcp.vtt", created_at=TODAY)
    token = _token(
        db,
        test_user,
        runtime_session_id=session.id,
        context={"runtime_principal": {"type": "agent", "id": "agent-a"}},
    )
    async with _mcp(app, token) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert "search_artifacts" not in names and "get_artifact" not in names

    for tool in ("search_artifacts", "get_artifact"):
        db.add(
            models.ToolConfiguration(
                account_id=test_user.account_id,
                tool_name=tool,
                tool_source="builtin",
                is_enabled=True,
            )
        )
    db.flush()

    async with _mcp(app, token) as mcp:
        found = await mcp.call_tool(
            "search_artifacts",
            {
                "kind": ["transcript"],
                "since": (TODAY - timedelta(minutes=1)).isoformat(),
            },
        )
        assert not found.isError, found.content
        assert [b.type for b in found.content] == ["resource_link"]
        assert found.structuredContent["items"][0]["id"] == str(row.id)
        read = await mcp.call_tool("get_artifact", {"artifact_id": str(row.id)})
        assert not read.isError, read.content
        block = read.content[0]
        assert block.type == "resource"
        assert block.resource.text.startswith("WEBVTT")
        assert block.meta[shapes.META_KEY]["truncated"] is False
        refused = await mcp.call_tool("search_artifacts", {"scope": "account"})
        assert refused.isError
        assert GRANT in refused.content[0].text
