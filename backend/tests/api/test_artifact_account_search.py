"""``GET /api/v1/artifacts``: account-wide artifact search (#1086).

Artifacts go in through ``POST /runtime-sessions/{id}/artifacts`` with the
warehouse-sim transcripts (#1091), the same ingestion path users have.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from preloop.models import models
from preloop.models.crud import crud_account, crud_api_key, crud_runtime_session
from preloop.models.crud import runtime_session_artifact as crud_artifact
from preloop.services import artifact_search

BASE = "/api/v1/runtime-sessions"
SEARCH = "/api/v1/artifacts"
STARTED = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "fixtures"
    / "warehouse_sim"
    / "transcripts"
)
SUMMARY = (
    "# Nord late shift summary\n\n"
    "- Damaged pallet at dock door 5, delivery 4711, quarantined in lane Q2.\n"
)
PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )
).decode()


def _session(db, account_id, source_id, *, title=None):
    session = crud_runtime_session.upsert_by_source(
        db,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Warehouse agent",
        started_at=STARTED,
        last_activity_at=STARTED,
    )
    if title:
        session.title = title
        db.commit()
    return session


def _headers(db, user, session_id, context=None):
    data = {"runtime_session_id": str(session_id), **(context or {})}
    _key, token = crud_api_key.create_runtime_key(
        db,
        name="Artifact agent",
        account_id=user.account_id,
        user_id=user.id,
        context_data=data,
    )
    return {"Authorization": f"Bearer {token}"}


def _deposit(client, headers, session_id, **body):
    response = client.post(f"{BASE}/{session_id}/artifacts", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _text(name, mime, text):
    return {
        "type": "resource",
        "resource": {"uri": f"file:///{name}", "mimeType": mime, "text": text},
    }


def _agent(db, account_id, name):
    agent_id = uuid.uuid4()
    now = datetime.now(UTC).replace(tzinfo=None)
    db.add(
        models.ManagedAgent(
            id=agent_id,
            account_id=account_id,
            agent_kind="custom",
            session_source_type="custom",
            session_source_id=f"agent-{agent_id}",
            session_reference=f"agent-{agent_id}",
            display_name=name,
            enrolled_via="runtime_session_token",
            lifecycle_state="active",
            lifecycle_updated_at=now,
            last_seen_at=now,
            tags={},
        )
    )
    db.commit()
    return agent_id


@pytest.fixture
def warehouse(client, db_session, test_user):
    """Six transcripts, a Nord summary and a screenshot from a second agent."""
    out = {"sessions": {}, "artifacts": {}}
    agent = _agent(db_session, test_user.account_id, "Nord shift agent")
    out["agent_id"] = str(agent)
    for site in ("nord", "sued"):
        session = _session(
            db_session,
            test_user.account_id,
            f"wh-{site}",
            title=f"{site.title()} shift review",
        )
        context = {"managed_agent_id": str(agent)} if site == "nord" else None
        headers = _headers(db_session, test_user, session.id, context)
        out["sessions"][site] = str(session.id)
        for shift in ("early", "late", "night"):
            text = (FIXTURES / site / f"{shift}.vtt").read_text(encoding="utf-8")
            out["artifacts"][(site, shift)] = _deposit(
                client,
                headers,
                session.id,
                name=f"{site}-{shift}.vtt",
                labels={"site": site, "shift": shift},
                tool_name="transcribe",
                content=_text(f"{site}-{shift}.vtt", "text/vtt", text),
            )
        if site == "nord":
            out["summary"] = _deposit(
                client,
                headers,
                session.id,
                name="nord-late-summary.md",
                kind="document",
                labels={"site": "nord"},
                content=_text("nord-late-summary.md", "text/markdown", SUMMARY),
            )
        else:
            out["screenshot"] = _deposit(
                client,
                headers,
                session.id,
                name="dock-5.png",
                labels={"site": "sued", "tags": ["dock"]},
                tool_name="browser_screenshot",
                content={"type": "image", "data": PNG, "mimeType": "image/png"},
            )
    return out


def _get(client, **params):
    response = client.get(SEARCH, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body):
    return [item["id"] for item in body["items"]]


def test_no_filter_lists_everything_newest_first_with_names(client, warehouse):
    body = _get(client)

    assert len(body["items"]) == 8
    created = [item["created_at"] for item in body["items"]]
    assert created == sorted(created, reverse=True)
    first = body["items"][0]
    assert first["id"] == warehouse["screenshot"]["id"]
    assert first["session_title"] == "Sued shift review"
    assert first["agent_name"] == "Warehouse agent"
    nord = next(i for i in body["items"] if i["id"] == warehouse["summary"]["id"])
    assert nord["agent_name"] == "Nord shift agent"
    assert nord["content_block"]["type"] == "resource_link"
    assert nord["content_block"]["_meta"]["preloop.dev/artifact"]["kind"] == (
        "document"
    )
    assert body["facets"] == {
        "kind": {"transcript": 6, "document": 1, "screenshot": 1},
        "site": {"nord": 4, "sued": 4},
    }
    assert body["facets_truncated"] is False
    assert all(item["excerpt"] is None for item in body["items"])


def test_q_damaged_pallet_returns_transcript_and_summary_with_excerpts(
    client, warehouse
):
    body = _get(client, q="damaged pallet", label="site:nord")

    by_id = {item["id"]: item for item in body["items"]}
    transcript = by_id[warehouse["artifacts"][("nord", "late")]["id"]]
    summary = by_id[warehouse["summary"]["id"]]
    for item in (transcript, summary):
        excerpt = item["excerpt"]
        assert excerpt["highlights"], excerpt
        marked = [excerpt["text"][a:b].lower() for a, b in excerpt["highlights"]]
        assert {"damaged", "pallet"} <= set(marked)
        assert "\x02" not in excerpt["text"] and "\x03" not in excerpt["text"]
    assert transcript["cue_start"] == 0.0
    assert summary["cue_start"] is None
    assert body["facets"]["site"] == {"nord": len(body["items"])}


def test_q_matches_name(client, warehouse):
    assert _ids(_get(client, q="dock-5")) == [warehouse["screenshot"]["id"]]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"kind": "document"}, lambda w: [w["summary"]["id"]]),
        (
            {"kind": ["document", "screenshot"]},
            lambda w: [w["screenshot"]["id"], w["summary"]["id"]],
        ),
        (
            {"label": "shift:night"},
            lambda w: [
                w["artifacts"][("sued", "night")]["id"],
                w["artifacts"][("nord", "night")]["id"],
            ],
        ),
        (
            {"label": ["site:nord", "shift:late"]},
            lambda w: [w["artifacts"][("nord", "late")]["id"]],
        ),
        ({"label": "tags:dock"}, lambda w: [w["screenshot"]["id"]]),
        ({"label": ["site:nord", "site:sued"]}, lambda w: []),
        ({"tool_name": "browser_screenshot"}, lambda w: [w["screenshot"]["id"]]),
        (
            {"producer": "deposit_api", "kind": "screenshot"},
            lambda w: [w["screenshot"]["id"]],
        ),
        ({"producer": "cli"}, lambda w: []),
        ({"held": "true"}, lambda w: []),
        ({"availability": "expired"}, lambda w: []),
    ],
)
def test_each_filter(client, warehouse, params, expected):
    assert _ids(_get(client, **params)) == expected(warehouse)


def test_agent_and_session_filters(client, warehouse):
    nord = _get(client, agent_id=warehouse["agent_id"])
    by_session = _get(client, runtime_session_id=warehouse["sessions"]["sued"])
    combined = _get(
        client,
        agent_id=warehouse["agent_id"],
        kind="transcript",
        label="shift:early",
        q="pallet OR delivery OR recount OR picker",
    )

    assert len(nord["items"]) == 4
    assert {i["runtime_session_id"] for i in nord["items"]} == {
        warehouse["sessions"]["nord"]
    }
    assert len(by_session["items"]) == 4
    assert by_session["facets"]["kind"] == {"transcript": 3, "screenshot": 1}
    assert _ids(combined) in (
        [],
        [warehouse["artifacts"][("nord", "early")]["id"]],
    )
    assert all(i["agent_id"] == warehouse["agent_id"] for i in combined["items"])


def test_date_range_on_created_at(client, warehouse):
    items = _get(client)["items"]
    middle = items[3]["created_at"]

    newer = _get(client, **{"from": middle})
    older = _get(client, to=middle)
    future = _get(
        client, **{"from": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    )

    assert _ids(newer) == _ids(_get(client))[:4]
    assert _ids(older) == _ids(_get(client))[4:]
    assert future["items"] == [] and future["facets"] == {"kind": {}, "site": {}}


def test_held_and_availability_reflect_row_state(client, db_session, warehouse):
    held_id = warehouse["summary"]["id"]
    gone_id = warehouse["artifacts"][("sued", "early")]["id"]
    row = db_session.get(models.RuntimeSessionArtifact, uuid.UUID(held_id))
    row.legal_hold = True
    db_session.commit()
    crud_artifact.mark_unavailable(
        db_session,
        account_id=row.account_id,
        artifact_id=uuid.UUID(gone_id),
        availability="evicted",
    )

    assert _ids(_get(client, held="true")) == [held_id]
    assert held_id not in _ids(_get(client, held="false"))
    assert _ids(_get(client, availability="evicted")) == [gone_id]
    assert gone_id not in _ids(_get(client, q="recount OR pallet OR picker"))


def test_other_accounts_never_appear_in_items_or_facets(
    client, db_session, test_user, warehouse
):
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    foreign = _session(db_session, other.id, "wh-foreign")
    foreign_user = models.User(
        account_id=other.id,
        email="other@example.com",
        username="other-user",
        hashed_password="x",
        is_active=True,
    )
    db_session.add(foreign_user)
    db_session.commit()
    _deposit(
        client,
        _headers(db_session, foreign_user, foreign.id),
        foreign.id,
        name="foreign-damaged-pallet.md",
        kind="document",
        labels={"site": "elsewhere"},
        content=_text("f.md", "text/markdown", "Damaged pallet elsewhere."),
    )

    everything = _get(client)
    searched = _get(client, q="damaged pallet")
    by_foreign_session = _get(client, runtime_session_id=str(foreign.id))

    for body in (everything, searched):
        assert all(i["runtime_session_id"] != str(foreign.id) for i in body["items"])
        assert "elsewhere" not in body["facets"]["site"]
    assert everything["facets"]["kind"]["document"] == 1
    assert by_foreign_session["items"] == []
    assert by_foreign_session["facets"] == {"kind": {}, "site": {}}


def test_cursor_is_stable_under_inserts(client, db_session, test_user, warehouse):
    first = _get(client, limit=3)
    late = _session(db_session, test_user.account_id, "wh-late")
    _deposit(
        client,
        _headers(db_session, test_user, late.id),
        late.id,
        name="arrived-later.txt",
        content={"type": "text", "text": "new"},
    )
    seen = _ids(first)
    cursor = first["next_cursor"]
    while cursor:
        page = _get(client, limit=3, cursor=cursor)
        seen.extend(_ids(page))
        cursor = page["next_cursor"]

    original = [
        warehouse["screenshot"]["id"],
        *[
            warehouse["artifacts"][("sued", s)]["id"]
            for s in ("night", "late", "early")
        ],
        warehouse["summary"]["id"],
        *[
            warehouse["artifacts"][("nord", s)]["id"]
            for s in ("night", "late", "early")
        ],
    ]
    assert seen == original
    assert _get(client, limit=3)["items"][0]["name"] == "arrived-later.txt"


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "video"},
        {"label": "nocolon"},
        {"limit": 0},
        {"limit": 201},
        {"cursor": "nope"},
        {"agent_id": "not-a-uuid"},
        {"runtime_session_id": "nope"},
        {"availability": "gone"},
        {"from": "2026-10-02T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
        {"q": "x" * 501},
    ],
)
def test_bad_parameters_are_422(client, params):
    assert client.get(SEARCH, params=params).status_code == 422


def test_facets_truncate_at_the_cap(db_session, warehouse, test_user):
    out = artifact_search.search(db_session, account_id=test_user.account_id)
    by_kind, by_site, truncated = crud_artifact.search_account_facets(
        db_session, account_id=test_user.account_id, cap=5
    )

    assert out.facets_truncated is False
    assert truncated is True
    assert sum(by_kind.values()) == 5
    assert by_kind["screenshot"] == 1
    assert sum(by_site.values()) == 5


def test_split_highlights_turns_markers_into_offsets():
    excerpt = artifact_search.split_highlights("a \x02damaged\x03 \x02pallet\x03 x")

    assert excerpt.text == "a damaged pallet x"
    assert excerpt.highlights == [(2, 9), (10, 16)]
