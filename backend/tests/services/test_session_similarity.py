"""Ranking and honesty of the "sessions like this one" list.

Every test here asserts one of two things: the order is the documented order,
or the answer names what it could not do. No test reaches a provider, and one
of them asserts that on purpose: a similarity list reads vectors that already
exist, so an account whose embedding budget is spent still gets one.
"""

from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_runtime_session,
    crud_session_embedding_setting,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import SessionSearchChunk
from preloop.models.models.session_embedding_setting import PROVIDER_LOCAL
from preloop.models.models.session_search_document import (
    EMBEDDING_DIMENSIONS,
    REDACTION_STATE_WITHHELD,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.schemas.session_search import (
    DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE,
    DEGRADED_SEMANTIC_DISABLED,
    DEGRADED_SEMANTIC_MODEL_MISMATCH,
    DEGRADED_SEMANTIC_NOT_ENABLED,
)
from preloop.schemas.session_similarity import (
    DEGRADED_SIMILAR_NO_COMPARABLE_SESSIONS,
    DEGRADED_SIMILAR_SESSION_NOT_EMBEDDED,
    DEGRADED_SIMILAR_SESSION_SAMPLED,
    DEGRADED_SIMILAR_WINDOW_APPLIED,
)
from preloop.services import session_similarity

BASE_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
MODEL_A = f"{PROVIDER_LOCAL}:test-embed@{EMBEDDING_DIMENSIONS}"
MODEL_B = f"{PROVIDER_LOCAL}:other-embed@{EMBEDDING_DIMENSIONS}"


def _axis(index: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


def _blend(first: int, second: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[first] = 1.0
    vector[second] = 1.0
    return vector


def _opt_in(db_session, account_id, model_identifier="test-embed"):
    return crud_session_embedding_setting.enable(
        db_session,
        account_id=account_id,
        provider=PROVIDER_LOCAL,
        model_identifier=model_identifier,
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
    model_identity=MODEL_A,
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
            db_session,
            vectors=[(rows[0], vector)],
            model_identity=model_identity,
        )
    db_session.flush()
    return rows[0]


def _similar(db_session, account_id, session, **kwargs):
    return session_similarity.similar_sessions(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def test_a_similar_session_comes_back_with_both_ends_of_the_match(
    db_session, test_user
):
    """A result says what matched, in the viewed session and in the other."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed")
    other = _session(db_session, test_user.account_id, "other")
    probe_row = _write(
        db_session,
        test_user.account_id,
        viewed,
        "we rolled the release back after the error rate spiked",
        source_id="viewed-message",
        vector=_axis(3),
    )
    matched = _write(
        db_session,
        test_user.account_id,
        other,
        "the release was rolled back once the error rate rose",
        source_id="other-message",
        vector=_axis(3),
    )

    answer = _similar(db_session, test_user.account_id, viewed)

    assert [str(row.runtime_session_id) for row in answer.results] == [str(other.id)]
    result = answer.results[0]
    assert result.similarity > 0.99
    assert result.band == "close"
    assert result.matched_chunk_count == 1
    assert str(result.matches[0].document_id) == str(matched.id)
    assert str(result.matches[0].probe.document_id) == str(probe_row.id)
    assert result.matches[0].text.startswith("the release was rolled back")
    assert answer.model_identity == MODEL_A
    assert answer.degraded.semantic is True
    assert answer.degraded.reasons == []


def test_the_best_matching_passage_decides_the_order(db_session, test_user):
    """Breadth is a tie breaker, not a ranking of its own."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-order")
    close = _session(db_session, test_user.account_id, "one-close-passage")
    broad = _session(db_session, test_user.account_id, "broadly-related")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the incident we are reading",
        source_id="viewed-order-message",
        vector=_axis(4),
    )
    _write(
        db_session,
        test_user.account_id,
        close,
        "the same incident, said the same way",
        source_id="close-message",
        vector=_axis(4),
    )
    for index in range(4):
        _write(
            db_session,
            test_user.account_id,
            broad,
            f"loosely about the same thing, part {index}",
            source_id=f"broad-message-{index}",
            occurred_at=BASE_AT + timedelta(minutes=index),
            vector=_blend(4, 30 + index),
        )

    answer = _similar(db_session, test_user.account_id, viewed)

    assert [str(row.runtime_session_id) for row in answer.results] == [
        str(close.id),
        str(broad.id),
    ]
    assert answer.results[0].matched_chunk_count == 1
    assert answer.results[1].matched_chunk_count == 4
    # The breadth bonus lifts the broad session, but not past a stronger match.
    assert answer.results[1].score > answer.results[1].similarity


def test_breadth_breaks_a_tie_between_equally_close_sessions(db_session, test_user):
    """Two sessions with the same best pair: the wider overlap wins."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-tie")
    narrow = _session(db_session, test_user.account_id, "narrow")
    wide = _session(db_session, test_user.account_id, "wide")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "first thing the agent did",
        source_id="viewed-tie-a",
        vector=_axis(5),
    )
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "second thing the agent did",
        source_id="viewed-tie-b",
        occurred_at=BASE_AT + timedelta(minutes=1),
        vector=_axis(6),
    )
    _write(
        db_session,
        test_user.account_id,
        narrow,
        "only the first thing",
        source_id="narrow-a",
        vector=_axis(5),
    )
    _write(
        db_session,
        test_user.account_id,
        wide,
        "the first thing",
        source_id="wide-a",
        vector=_axis(5),
    )
    _write(
        db_session,
        test_user.account_id,
        wide,
        "and the second thing",
        source_id="wide-b",
        occurred_at=BASE_AT + timedelta(minutes=1),
        vector=_axis(6),
    )

    answer = _similar(db_session, test_user.account_id, viewed)

    assert [str(row.runtime_session_id) for row in answer.results] == [
        str(wide.id),
        str(narrow.id),
    ]
    assert answer.results[0].similarity == answer.results[1].similarity
    assert answer.results[0].score > answer.results[1].score


def test_the_same_session_is_ranked_the_same_way_twice(db_session, test_user):
    """A list that reshuffles between reloads is not a ranking."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-stable")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the probe",
        source_id="viewed-stable-message",
        vector=_axis(7),
    )
    for index in range(5):
        other = _session(db_session, test_user.account_id, f"tied-{index}")
        _write(
            db_session,
            test_user.account_id,
            other,
            f"identical distance {index}",
            source_id=f"tied-message-{index}",
            vector=_axis(7),
        )

    first = _similar(db_session, test_user.account_id, viewed)
    second = _similar(db_session, test_user.account_id, viewed)

    assert [str(row.runtime_session_id) for row in first.results] == [
        str(row.runtime_session_id) for row in second.results
    ]


def test_a_session_with_no_vectors_says_so_and_returns_nothing(db_session, test_user):
    """Empty is an answer; empty without a reason is a shrug."""
    viewed = _session(db_session, test_user.account_id, "viewed-unembedded")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "indexed, never embedded",
        source_id="viewed-unembedded-message",
    )

    answer = _similar(db_session, test_user.account_id, viewed)

    assert answer.results == []
    assert answer.degraded.semantic is False
    assert DEGRADED_SIMILAR_SESSION_NOT_EMBEDDED in answer.degraded.reasons
    assert DEGRADED_SEMANTIC_NOT_ENABLED in answer.degraded.reasons
    assert DEGRADED_SEMANTIC_BACKFILL_INCOMPLETE in answer.degraded.reasons
    assert answer.degraded.detail


def test_the_deployment_kill_switch_is_named_rather_than_the_opt_in(
    db_session, test_user, monkeypatch
):
    """An account cannot fix an answer by opting in to a disabled deployment."""
    monkeypatch.setattr(
        session_similarity, "embedding_enabled", lambda: False, raising=True
    )
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-killswitch")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "indexed, never embedded",
        source_id="viewed-killswitch-message",
    )

    answer = _similar(db_session, test_user.account_id, viewed)

    assert DEGRADED_SEMANTIC_DISABLED in answer.degraded.reasons
    assert DEGRADED_SEMANTIC_NOT_ENABLED not in answer.degraded.reasons


def test_a_corpus_with_no_other_embedded_session_says_which_empty_this_is(
    db_session, test_user
):
    """ "Nothing was close enough" and "nothing to compare" are different."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-alone")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the only embedded session in this account",
        source_id="viewed-alone-message",
        vector=_axis(8),
    )

    answer = _similar(db_session, test_user.account_id, viewed)

    assert answer.results == []
    assert answer.degraded.semantic is True
    assert DEGRADED_SIMILAR_NO_COMPARABLE_SESSIONS in answer.degraded.reasons


def test_a_distant_corpus_is_empty_without_claiming_there_was_nothing(
    db_session, test_user
):
    """Other sessions exist and were compared; none of them was close."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-distant")
    other = _session(db_session, test_user.account_id, "distant")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "a question about billing",
        source_id="viewed-distant-message",
        vector=_axis(9),
    )
    _write(
        db_session,
        test_user.account_id,
        other,
        "an unrelated conversation",
        source_id="distant-message",
        vector=_axis(10),
    )

    answer = _similar(db_session, test_user.account_id, viewed)

    assert answer.results == []
    assert DEGRADED_SIMILAR_NO_COMPARABLE_SESSIONS not in answer.degraded.reasons
    assert answer.degraded.semantic is True


def test_a_session_embedded_by_another_model_is_compared_in_its_own_space(
    db_session, test_user
):
    """It still answers, in the space it was written in, and says which."""
    _opt_in(db_session, test_user.account_id, model_identifier="other-embed")
    viewed = _session(db_session, test_user.account_id, "viewed-old-model")
    contemporary = _session(db_session, test_user.account_id, "contemporary")
    stranger = _session(db_session, test_user.account_id, "current-model-session")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "embedded before the provider change",
        source_id="viewed-old-model-message",
        vector=_axis(11),
        model_identity=MODEL_A,
    )
    _write(
        db_session,
        test_user.account_id,
        contemporary,
        "embedded before the provider change as well",
        source_id="contemporary-message",
        vector=_axis(11),
        model_identity=MODEL_A,
    )
    _write(
        db_session,
        test_user.account_id,
        stranger,
        "embedded after the provider change",
        source_id="stranger-message",
        vector=_axis(11),
        model_identity=MODEL_B,
    )

    answer = _similar(db_session, test_user.account_id, viewed)

    assert [str(row.runtime_session_id) for row in answer.results] == [
        str(contemporary.id)
    ]
    assert answer.model_identity == MODEL_A
    assert DEGRADED_SEMANTIC_MODEL_MISMATCH in answer.degraded.reasons


def test_a_long_session_says_it_was_compared_from_a_sample(db_session, test_user):
    """The bias is published: how much of the session actually asked."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-sampled")
    other = _session(db_session, test_user.account_id, "other-sampled")
    for index in range(20):
        _write(
            db_session,
            test_user.account_id,
            viewed,
            f"turn {index}",
            source_id=f"viewed-sampled-{index:02d}",
            occurred_at=BASE_AT + timedelta(minutes=index),
            vector=_axis(40 + index),
        )
    _write(
        db_session,
        test_user.account_id,
        other,
        "the same as one of those turns",
        source_id="other-sampled-message",
        vector=_axis(40),
    )

    answer = _similar(db_session, test_user.account_id, viewed)

    assert answer.embedded_chunks == 20
    assert answer.probe_chunks < answer.embedded_chunks
    assert DEGRADED_SIMILAR_SESSION_SAMPLED in answer.degraded.reasons
    assert [str(row.runtime_session_id) for row in answer.results] == [str(other.id)]


def test_a_window_is_applied_and_named(db_session, test_user):
    """A window that hides an older session says that it did."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-window")
    old = _session(db_session, test_user.account_id, "old-window")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the investigation",
        source_id="viewed-window-message",
        occurred_at=NOW - timedelta(days=1),
        vector=_axis(12),
    )
    _write(
        db_session,
        test_user.account_id,
        old,
        "the same investigation, long ago",
        source_id="old-window-message",
        occurred_at=NOW - timedelta(days=200),
        vector=_axis(12),
    )

    unbounded = _similar(db_session, test_user.account_id, viewed)
    windowed = _similar(db_session, test_user.account_id, viewed, window_days=30)

    assert [str(row.runtime_session_id) for row in unbounded.results] == [str(old.id)]
    assert unbounded.window_days is None
    assert windowed.results == []
    assert windowed.window_days == 30
    assert DEGRADED_SIMILAR_WINDOW_APPLIED in windowed.degraded.reasons


def test_an_account_at_its_daily_cap_still_gets_a_list(db_session, test_user):
    """Similarity spends nothing, so a spent budget cannot stop it."""
    setting = _opt_in(db_session, test_user.account_id)
    setting.daily_cap_usd = 0.0
    db_session.flush()
    viewed = _session(db_session, test_user.account_id, "viewed-cap")
    other = _session(db_session, test_user.account_id, "other-cap")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the capped account's session",
        source_id="viewed-cap-message",
        vector=_axis(13),
    )
    _write(
        db_session,
        test_user.account_id,
        other,
        "another session of the capped account",
        source_id="other-cap-message",
        vector=_axis(13),
    )

    answer = _similar(db_session, test_user.account_id, viewed)

    assert [str(row.runtime_session_id) for row in answer.results] == [str(other.id)]
    assert answer.degraded.reasons == []


def test_match_text_can_be_left_in_the_database(db_session, test_user):
    """The ranking is available without moving any captured content."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-notext")
    other = _session(db_session, test_user.account_id, "other-notext")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "a passage nobody should have to move to rank it",
        source_id="viewed-notext-message",
        vector=_axis(14),
    )
    _write(
        db_session,
        test_user.account_id,
        other,
        "the matching passage",
        source_id="other-notext-message",
        vector=_axis(14),
    )

    answer = _similar(
        db_session, test_user.account_id, viewed, include_match_text=False
    )

    assert len(answer.results) == 1
    assert answer.results[0].matches[0].text is None
    assert answer.results[0].matches[0].source_id == "other-notext-message"


def test_a_match_whose_text_was_withheld_is_not_returned_at_all(db_session, test_user):
    """A vector that outlives its text cannot answer with the text."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-withheld")
    other = _session(db_session, test_user.account_id, "other-withheld")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the passage being read",
        source_id="viewed-withheld-message",
        vector=_axis(15),
    )
    hidden = _write(
        db_session,
        test_user.account_id,
        other,
        "the passage that was taken away",
        source_id="other-withheld-message",
        vector=_axis(15),
    )
    hidden.redaction_state = REDACTION_STATE_WITHHELD
    hidden.content = ""
    db_session.flush()

    answer = _similar(db_session, test_user.account_id, viewed)

    assert answer.results == []


def test_the_number_of_results_is_bounded_by_the_limit(db_session, test_user):
    """A detail panel asks for a handful, not for the corpus."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-limit")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the probe",
        source_id="viewed-limit-message",
        vector=_axis(16),
    )
    for index in range(6):
        other = _session(db_session, test_user.account_id, f"limit-other-{index}")
        _write(
            db_session,
            test_user.account_id,
            other,
            f"a near neighbour {index}",
            source_id=f"limit-other-message-{index}",
            vector=_blend(16, 50 + index),
        )

    answer = _similar(db_session, test_user.account_id, viewed, limit=2)

    assert len(answer.results) == 2
    assert answer.limit == 2


def test_matches_per_session_are_bounded_and_strongest_first(db_session, test_user):
    """An entry says what matched without carrying a transcript."""
    _opt_in(db_session, test_user.account_id)
    viewed = _session(db_session, test_user.account_id, "viewed-matches")
    other = _session(db_session, test_user.account_id, "other-matches")
    _write(
        db_session,
        test_user.account_id,
        viewed,
        "the probe passage",
        source_id="viewed-matches-message",
        vector=_axis(17),
    )
    _write(
        db_session,
        test_user.account_id,
        other,
        "the closest passage",
        source_id="other-matches-close",
        vector=_axis(17),
    )
    for index in range(3):
        _write(
            db_session,
            test_user.account_id,
            other,
            f"a weaker passage {index}",
            source_id=f"other-matches-weak-{index}",
            occurred_at=BASE_AT + timedelta(minutes=index + 1),
            vector=_blend(17, 60 + index),
        )

    answer = _similar(
        db_session, test_user.account_id, viewed, max_matches_per_session=2
    )

    matches = answer.results[0].matches
    assert len(matches) == 2
    assert matches[0].similarity >= matches[1].similarity
    assert matches[0].source_id == "other-matches-close"
    assert answer.results[0].matched_chunk_count == 4
