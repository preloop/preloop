"""Sessions similar to this one, through the endpoint.

Every test here is a claim a caller can rely on: the list never leaves the
account, a session is never its own neighbour, a match names the turn it came
from so a console can open it there, and every answer that is narrower than
the question says why. Nothing in this file reaches an embedding provider,
because nothing in this feature does.
"""

from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_account,
    crud_runtime_session,
    crud_session_embedding_setting,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import SessionSearchChunk
from preloop.models.models.session_embedding_setting import PROVIDER_LOCAL
from preloop.models.models.session_search_document import (
    EMBEDDING_DIMENSIONS,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.schemas.session_search import DEGRADED_SEMANTIC_NOT_ENABLED
from preloop.schemas.session_similarity import (
    DEGRADED_SIMILAR_SESSION_NOT_EMBEDDED,
    DEGRADED_SIMILAR_WINDOW_APPLIED,
)

BASE_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
MODEL_IDENTITY = f"{PROVIDER_LOCAL}:test-embed@{EMBEDDING_DIMENSIONS}"


def _url(session_id) -> str:
    return f"/api/v1/runtime-sessions/{session_id}/similar"


def _axis(index: int) -> list:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


def _opt_in(db_session, account_id):
    return crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_LOCAL,
        model_identifier="test-embed",
    )


def _session(db_session, account_id, source_id, *, started_at=BASE_AT):
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=started_at,
        last_activity_at=started_at,
    )


def _write(
    db_session,
    account_id,
    session,
    text,
    *,
    source_id,
    occurred_at=BASE_AT,
    vector=None,
    model_identity=MODEL_IDENTITY,
):
    rows = crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=source_id,
        occurred_at=occurred_at,
        chunks=[SessionSearchChunk(content=text, role="assistant")],
    )
    if vector is not None:
        crud_session_search_document.store_embeddings(
            db_session, vectors=[(rows[0], vector)], model_identity=model_identity
        )
    db_session.flush()
    return rows[0]


def _pair(db_session, account_id):
    """One session being viewed and one session that is like it."""
    viewed = _session(db_session, account_id, "viewed")
    other = _session(db_session, account_id, "other")
    _write(
        db_session,
        account_id,
        viewed,
        "the agent restarted the stuck worker pool",
        source_id="viewed-message",
        vector=_axis(3),
    )
    _write(
        db_session,
        account_id,
        other,
        "the worker pool was restarted after it stuck",
        source_id="other-message",
        vector=_axis(3),
    )
    return viewed, other


def test_a_similar_session_is_returned_with_the_turn_that_matched(
    client, db_session, test_user
):
    """A console entry can open the other session at its matching turn."""
    _opt_in(db_session, test_user.account_id)
    viewed, other = _pair(db_session, test_user.account_id)

    response = client.get(_url(viewed.id))

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime_session_id"] == str(viewed.id)
    assert [row["runtime_session_id"] for row in payload["results"]] == [str(other.id)]
    result = payload["results"][0]
    assert result["band"] == "close"
    assert result["session_reference"] == "other"
    match = result["matches"][0]
    assert match["source_kind"] == SOURCE_KIND_TRANSCRIPT_MESSAGE
    assert match["source_id"] == "other-message"
    assert match["chunk_index"] == 0
    assert match["probe"]["source_id"] == "viewed-message"
    assert payload["degraded"]["reasons"] == []
    assert payload["model_identity"] == MODEL_IDENTITY


def test_the_session_being_viewed_is_never_in_its_own_list(
    client, db_session, test_user
):
    """It is its own nearest neighbour, which is not an answer."""
    _opt_in(db_session, test_user.account_id)
    viewed, _ = _pair(db_session, test_user.account_id)

    payload = client.get(_url(viewed.id)).json()

    assert str(viewed.id) not in [
        row["runtime_session_id"] for row in payload["results"]
    ]


def test_another_accounts_session_is_not_found_rather_than_compared(
    client, db_session, test_user
):
    """A probe cannot tell another account's session from a missing one."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    theirs = _session(db_session, other_account.id, "theirs")
    _write(
        db_session,
        other_account.id,
        theirs,
        "their session content",
        source_id="their-message",
        vector=_axis(4),
    )

    response = client.get(_url(theirs.id))

    assert response.status_code == 404


def test_another_accounts_content_is_never_a_match(client, db_session, test_user):
    """The account bound is in the query behind this route."""
    _opt_in(db_session, test_user.account_id)
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    viewed = _session(db_session, test_user.account_id, "viewed-bound")
    theirs = _session(db_session, other_account.id, "theirs-bound")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "identical content on both sides of the bound",
        source_id="viewed-bound-message",
        vector=_axis(5),
    )
    _write(
        db_session,
        other_account.id,
        theirs,
        "identical content on both sides of the bound",
        source_id="theirs-bound-message",
        vector=_axis(5),
    )

    payload = client.get(_url(viewed.id)).json()

    assert payload["results"] == []


def test_a_missing_session_is_a_404(client, db_session, test_user):
    """A session id that never existed is not an empty similarity list."""
    response = client.get(_url("00000000-0000-0000-0000-000000000000"))

    assert response.status_code == 404


def test_a_malformed_session_id_is_rejected_before_any_query(
    client, db_session, test_user
):
    """The path is a session id, so a non identifier is a validation error."""
    response = client.get(_url("not-a-session-id"))

    assert response.status_code == 422


def test_an_account_that_never_opted_in_gets_an_empty_list_with_a_reason(
    client, db_session, test_user
):
    """Never an error: a list nobody can build still says why."""
    viewed = _session(db_session, test_user.account_id, "viewed-unembedded")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "indexed but never embedded",
        source_id="viewed-unembedded-message",
    )

    response = client.get(_url(viewed.id))

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"] == []
    assert payload["degraded"]["semantic"] is False
    assert DEGRADED_SIMILAR_SESSION_NOT_EMBEDDED in payload["degraded"]["reasons"]
    assert DEGRADED_SEMANTIC_NOT_ENABLED in payload["degraded"]["reasons"]
    assert payload["degraded"]["detail"]


def test_a_window_narrows_the_answer_and_says_so(client, db_session, test_user):
    """An old neighbour hidden by a window is a fact the answer carries."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-window")
    old = _session(db_session, test_user.account_id, "old-window")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the same investigation",
        source_id="viewed-window-message",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=1),
        vector=_axis(6),
    )
    _write(
        db_session,
        test_user.account_id,
        old,
        "the same investigation, long ago",
        source_id="old-window-message",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=200),
        vector=_axis(6),
    )

    unbounded = client.get(_url(viewed.id)).json()
    windowed = client.get(_url(viewed.id), params={"window_days": 30}).json()

    assert [row["runtime_session_id"] for row in unbounded["results"]] == [str(old.id)]
    assert windowed["results"] == []
    assert DEGRADED_SIMILAR_WINDOW_APPLIED in windowed["degraded"]["reasons"]


def test_the_ranking_is_available_without_moving_captured_content(
    client, db_session, test_user
):
    """``include_match_text=false`` returns identity and no transcript."""
    _opt_in(db_session, test_user.account_id)
    viewed, other = _pair(db_session, test_user.account_id)

    payload = client.get(_url(viewed.id), params={"include_match_text": "false"}).json()

    match = payload["results"][0]["matches"][0]
    assert match["text"] is None
    assert match["source_id"] == "other-message"


def test_a_limit_past_the_documented_ceiling_is_rejected(client, db_session, test_user):
    """The ceiling is part of the contract, not a suggestion."""
    _opt_in(db_session, test_user.account_id)
    viewed, _ = _pair(db_session, test_user.account_id)

    assert client.get(_url(viewed.id), params={"limit": 999}).status_code == 422
    assert client.get(_url(viewed.id), params={"limit": 0}).status_code == 422
    assert client.get(_url(viewed.id), params={"window_days": 0}).status_code == 422


def test_the_same_request_twice_returns_the_same_order(client, db_session, test_user):
    """A list that reshuffles between reloads is not a ranking."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-stable")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the probe passage",
        source_id="viewed-stable-message",
        vector=_axis(7),
    )
    for index in range(4):
        other = _session(db_session, test_user.account_id, f"tied-{index}")
        _write(
            db_session,
            test_user.account_id,
            other,
            f"an equally close passage {index}",
            source_id=f"tied-message-{index}",
            vector=_axis(7),
        )

    first = client.get(_url(viewed.id)).json()
    second = client.get(_url(viewed.id)).json()

    assert [row["runtime_session_id"] for row in first["results"]] == [
        row["runtime_session_id"] for row in second["results"]
    ]
    assert len(first["results"]) == 4
