"""Vector reads behind "sessions like this one".

These tests pin the properties the list depends on and a refactor could
quietly lose: a session is never its own neighbour, a probe never scores a
vector from another model, a withheld chunk neither probes nor answers, the
probe sample is spread through the session rather than taken from its head,
and two identical corpora produce the same order.
"""

from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_account,
    crud_runtime_session,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import (
    MIN_SIMILAR_SIMILARITY,
    SIMILAR_PREVIEW_CHARS,
    SIMILAR_PROBE_CHUNKS,
    SessionSearchChunk,
)
from preloop.models.models.session_search_document import (
    EMBEDDING_DIMENSIONS,
    REDACTION_STATE_WITHHELD,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)

BASE_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
MODEL_A = "local:model-a@1536"
MODEL_B = "local:model-b@1536"


def _axis(index: int) -> list[float]:
    """A unit vector on one axis, so similarities are exact and obvious."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


def _blend(first: int, second: int) -> list[float]:
    """A vector halfway between two axes: cosine 0.7071 with either."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[first] = 1.0
    vector[second] = 1.0
    return vector


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
    source_id="message-1",
    occurred_at=BASE_AT,
    vector=None,
    model_identity=MODEL_A,
    **chunk_fields,
):
    """Write one chunk and, unless told otherwise, give it a vector."""
    rows = crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=source_id,
        occurred_at=occurred_at,
        chunks=[SessionSearchChunk(content=text, role="assistant", **chunk_fields)],
    )
    if vector is not None:
        crud_session_search_document.store_embeddings(
            db_session,
            vectors=[(rows[0], vector)],
            model_identity=model_identity,
        )
    db_session.flush()
    return rows[0]


def _probes(db_session, account_id, session, *, model_identity=MODEL_A, **kwargs):
    return crud_session_search_document.session_probe_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        embedding_model=model_identity,
        **kwargs,
    )


def _neighbours(db_session, account_id, session, probes, **kwargs):
    return crud_session_search_document.similar_chunks(
        db_session,
        account_id=account_id,
        probes=probes,
        exclude_session_id=session.id,
        **kwargs,
    )


def test_a_session_is_never_its_own_neighbour(db_session, test_user):
    """Otherwise every session's closest match is itself, in every position."""
    viewed = _session(db_session, test_user.account_id, "viewed")
    other = _session(db_session, test_user.account_id, "other")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "restarted the worker pool",
        source_id="viewed-message",
        vector=_axis(3),
    )
    _write(
        db_session,
        test_user.account_id,
        other,
        "restarted the worker pool again",
        source_id="other-message",
        vector=_axis(3),
    )

    probes = _probes(db_session, test_user.account_id, viewed)
    hits = _neighbours(db_session, test_user.account_id, viewed, probes)

    assert len(probes) == 1
    assert [str(hit.runtime_session_id) for hit in hits] == [str(other.id)]


def test_a_probe_never_scores_a_chunk_embedded_with_another_model(
    db_session, test_user
):
    """Both candidates carry the same vector; only the stamp differs."""
    viewed = _session(db_session, test_user.account_id, "viewed-models")
    same = _session(db_session, test_user.account_id, "same-model")
    other = _session(db_session, test_user.account_id, "other-model")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "cleared the stuck queue",
        source_id="viewed-models-message",
        vector=_axis(4),
        model_identity=MODEL_A,
    )
    _write(
        db_session,
        test_user.account_id,
        same,
        "cleared the stuck queue",
        source_id="same-model-message",
        vector=_axis(4),
        model_identity=MODEL_A,
    )
    _write(
        db_session,
        test_user.account_id,
        other,
        "cleared the stuck queue",
        source_id="other-model-message",
        vector=_axis(4),
        model_identity=MODEL_B,
    )

    probes = _probes(db_session, test_user.account_id, viewed)
    hits = _neighbours(db_session, test_user.account_id, viewed, probes)

    assert [str(hit.runtime_session_id) for hit in hits] == [str(same.id)]


def test_a_chunk_from_another_account_is_never_a_neighbour(db_session, test_user):
    """The account bound is in the query, not in a serialiser."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )

    viewed = _session(db_session, test_user.account_id, "viewed-account")
    theirs = _session(db_session, other_account.id, "their-session")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "rotated the signing key",
        source_id="viewed-account-message",
        vector=_axis(5),
    )
    _write(
        db_session,
        other_account.id,
        theirs,
        "rotated the signing key",
        source_id="their-message",
        vector=_axis(5),
    )

    probes = _probes(db_session, test_user.account_id, viewed)
    hits = _neighbours(db_session, test_user.account_id, viewed, probes)

    assert hits == []


def test_a_chunk_withheld_after_embedding_neither_probes_nor_answers(
    db_session, test_user
):
    """A redaction that leaves the vector answering is not a redaction."""
    viewed = _session(db_session, test_user.account_id, "viewed-withheld")
    other = _session(db_session, test_user.account_id, "other-withheld")
    kept = _write(
        db_session,
        test_user.account_id,
        viewed,
        "the incident summary",
        source_id="viewed-withheld-kept",
        vector=_axis(6),
    )
    hidden_probe = _write(
        db_session,
        test_user.account_id,
        viewed,
        "a passage that was taken away",
        source_id="viewed-withheld-hidden",
        occurred_at=BASE_AT + timedelta(minutes=1),
        vector=_axis(7),
    )
    hidden_answer = _write(
        db_session,
        test_user.account_id,
        other,
        "the same passage elsewhere",
        source_id="other-withheld-hidden",
        vector=_axis(6),
    )
    for row in (hidden_probe, hidden_answer):
        row.redaction_state = REDACTION_STATE_WITHHELD
        row.content = ""
    db_session.flush()

    probes = _probes(db_session, test_user.account_id, viewed)
    hits = _neighbours(db_session, test_user.account_id, viewed, probes)

    assert [str(probe.document_id) for probe in probes] == [str(kept.id)]
    assert hits == []


def test_neighbours_below_the_floor_are_not_matches(db_session, test_user):
    """Without a floor every session is similar to every other one."""
    viewed = _session(db_session, test_user.account_id, "viewed-floor")
    near = _session(db_session, test_user.account_id, "near-floor")
    far = _session(db_session, test_user.account_id, "far-floor")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the rollback took twenty minutes",
        source_id="viewed-floor-message",
        vector=_axis(8),
    )
    _write(
        db_session,
        test_user.account_id,
        near,
        "a rollback, again",
        source_id="near-floor-message",
        vector=_blend(8, 9),
    )
    _write(
        db_session,
        test_user.account_id,
        far,
        "lunch plans",
        source_id="far-floor-message",
        vector=_axis(10),
    )

    probes = _probes(db_session, test_user.account_id, viewed)
    hits = _neighbours(db_session, test_user.account_id, viewed, probes)

    assert [str(hit.runtime_session_id) for hit in hits] == [str(near.id)]
    assert hits[0].similarity > MIN_SIMILAR_SIMILARITY


def test_probes_are_spread_through_the_session_not_taken_from_its_head(
    db_session, test_user
):
    """A long session is represented by its beginning, middle and end."""
    viewed = _session(db_session, test_user.account_id, "viewed-long")
    written = []
    for index in range(SIMILAR_PROBE_CHUNKS * 3):
        written.append(
            _write(
                db_session,
                test_user.account_id,
                viewed,
                f"turn number {index}",
                source_id=f"viewed-long-message-{index:02d}",
                occurred_at=BASE_AT + timedelta(minutes=index),
                vector=_axis(index + 20),
            )
        )

    probes = _probes(db_session, test_user.account_id, viewed)

    assert len(probes) == SIMILAR_PROBE_CHUNKS
    positions = [
        [str(row.id) for row in written].index(str(probe.document_id))
        for probe in probes
    ]
    assert positions == sorted(positions)
    # Taken at a stride: the sample reaches the last third of the session,
    # which a head-first sample of the same size never would.
    assert positions[0] == 0
    assert max(positions) >= len(written) * 2 // 3
    assert len(set(positions)) == len(positions)


def test_the_neighbour_ordering_is_the_same_twice(db_session, test_user):
    """Equally close neighbours come back in the same order every time."""
    viewed = _session(db_session, test_user.account_id, "viewed-ties")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "identical distance probe",
        source_id="viewed-ties-message",
        vector=_axis(12),
    )
    for index in range(5):
        other = _session(db_session, test_user.account_id, f"tied-{index}")
        _write(
            db_session,
            test_user.account_id,
            other,
            f"identical distance chunk {index}",
            source_id=f"tied-message-{index}",
            vector=_axis(12),
        )

    probes = _probes(db_session, test_user.account_id, viewed)
    first = _neighbours(db_session, test_user.account_id, viewed, probes)
    second = _neighbours(db_session, test_user.account_id, viewed, probes)

    assert len(first) == 5
    assert [str(hit.document_id) for hit in first] == [
        str(hit.document_id) for hit in second
    ]


def test_a_window_excludes_content_older_than_it(db_session, test_user):
    """The window is a filter in the query, not a note in the response."""
    viewed = _session(db_session, test_user.account_id, "viewed-window")
    recent = _session(db_session, test_user.account_id, "recent-window")
    old = _session(db_session, test_user.account_id, "old-window")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the same investigation",
        source_id="viewed-window-message",
        vector=_axis(13),
    )
    _write(
        db_session,
        test_user.account_id,
        recent,
        "the same investigation, recently",
        source_id="recent-window-message",
        vector=_axis(13),
    )
    _write(
        db_session,
        test_user.account_id,
        old,
        "the same investigation, long ago",
        source_id="old-window-message",
        occurred_at=BASE_AT - timedelta(days=200),
        vector=_axis(13),
    )

    probes = _probes(db_session, test_user.account_id, viewed)
    unbounded = _neighbours(db_session, test_user.account_id, viewed, probes)
    windowed = _neighbours(
        db_session,
        test_user.account_id,
        viewed,
        probes,
        start_date=BASE_AT - timedelta(days=30),
    )

    assert {str(hit.runtime_session_id) for hit in unbounded} == {
        str(recent.id),
        str(old.id),
    }
    assert [str(hit.runtime_session_id) for hit in windowed] == [str(recent.id)]


def test_the_state_of_a_session_says_which_space_it_can_be_compared_in(
    db_session, test_user
):
    """The account's current model wins when the session has vectors of it."""
    viewed = _session(db_session, test_user.account_id, "viewed-state")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "embedded with the old model",
        source_id="viewed-state-old",
        vector=_axis(14),
        model_identity=MODEL_A,
    )
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "embedded with the current model",
        source_id="viewed-state-new",
        occurred_at=BASE_AT + timedelta(minutes=1),
        vector=_axis(15),
        model_identity=MODEL_B,
    )
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "not embedded at all",
        source_id="viewed-state-pending",
        occurred_at=BASE_AT + timedelta(minutes=2),
    )

    preferred = crud_session_search_document.session_embedding_state(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=viewed.id,
        preferred_model=MODEL_B,
    )
    assert preferred.model_identity == MODEL_B
    assert preferred.model_chunks == 1
    assert preferred.embedded == 2
    assert preferred.pending == 1
    assert preferred.models == 2

    # A model this session has nothing in cannot be the space it is compared
    # in, so the majority model is used instead and the caller is told.
    absent = crud_session_search_document.session_embedding_state(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=viewed.id,
        preferred_model="local:model-c@1536",
    )
    assert absent.model_identity in (MODEL_A, MODEL_B)
    assert absent.model_chunks == 1


def test_a_preview_is_truncated_and_a_withheld_chunk_has_none(db_session, test_user):
    """The stored body never travels: the projection cuts it in the database."""
    session = _session(db_session, test_user.account_id, "preview")
    long_row = _write(
        db_session,
        test_user.account_id,
        session,
        "x" * (SIMILAR_PREVIEW_CHARS * 2),
        source_id="preview-long",
        vector=_axis(16),
    )
    hidden = _write(
        db_session,
        test_user.account_id,
        session,
        "a passage that was taken away",
        source_id="preview-hidden",
        occurred_at=BASE_AT + timedelta(minutes=1),
        vector=_axis(17),
    )
    hidden.redaction_state = REDACTION_STATE_WITHHELD
    hidden.content = ""
    db_session.flush()

    previews = crud_session_search_document.chunk_previews(
        db_session,
        account_id=test_user.account_id,
        document_ids=[long_row.id, hidden.id],
    )

    assert len(previews[str(long_row.id)]) == SIMILAR_PREVIEW_CHARS
    assert previews[str(hidden.id)] is None
