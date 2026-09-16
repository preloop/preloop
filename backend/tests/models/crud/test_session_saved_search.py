"""CRUD tests for saved session searches.

The properties asserted here are the ones a serialiser cannot be trusted with:
the account bound, the visibility bound and the name uniqueness rule all have
to hold in the query itself.
"""

from datetime import UTC, datetime

import pytest

from preloop.models.crud import crud_account, crud_session_saved_search, crud_user
from preloop.models.crud.session_saved_search import SessionSavedSearchNameConflictError
from preloop.models.models.session_saved_search import (
    VISIBILITY_ACCOUNT,
    VISIBILITY_PRIVATE,
)

RANKING = "rrf-k=60;kw=1;sem=1;floor=0.2"


def _user(db_session, account_id, *, email):
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": account_id,
            "email": email,
            "username": email.split("@")[0],
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        },
    )
    db_session.flush()
    return user


def _save(db_session, *, account_id, owner_user_id, name="billing", **overrides):
    values = {
        "query": "billing migration",
        "mode": "keyword",
        "filters": {},
        "ranking_identity": RANKING,
        "max_snippets_per_session": 3,
        "include_snippet_text": True,
        "visibility": VISIBILITY_PRIVATE,
    }
    values.update(overrides)
    return crud_session_saved_search.create_for_user(
        db_session,
        account_id=account_id,
        owner_user_id=owner_user_id,
        name=name,
        **values,
    )


def test_a_saved_search_stores_the_question_and_no_results(db_session, test_user):
    """Everything stored is the question; nothing is an answer."""
    saved = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        filters={"source_kind": "tool_call"},
        mode="hybrid",
    )

    assert saved.query == "billing migration"
    assert saved.mode == "hybrid"
    assert saved.filters == {"source_kind": "tool_call"}
    assert saved.visibility == VISIBILITY_PRIVATE
    assert saved.shared_at is None
    assert saved.run_count == 0
    assert saved.last_run_at is None
    assert not hasattr(saved, "results")


def test_another_accounts_saved_search_is_never_loaded(db_session, test_user):
    """The account bound is in the query, not in what the caller asks for."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    other_user = _user(db_session, other_account.id, email="other@example.com")
    theirs = _save(
        db_session,
        account_id=other_account.id,
        owner_user_id=other_user.id,
        visibility=VISIBILITY_ACCOUNT,
    )

    assert (
        crud_session_saved_search.get_visible(
            db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            saved_search_id=theirs.id,
        )
        is None
    )
    rows, total = crud_session_saved_search.list_visible(
        db_session, account_id=test_user.account_id, user_id=test_user.id
    )
    assert rows == [] and total == 0


def test_a_private_search_of_a_colleague_is_not_visible(db_session, test_user):
    """Private means private, including inside one account."""
    colleague = _user(db_session, test_user.account_id, email="colleague@example.com")
    theirs = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=colleague.id,
        name="their private search",
    )

    assert (
        crud_session_saved_search.get_visible(
            db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            saved_search_id=theirs.id,
        )
        is None
    )


def test_a_shared_search_of_a_colleague_is_visible(db_session, test_user):
    """Sharing is what makes a colleague's saved search readable."""
    colleague = _user(db_session, test_user.account_id, email="colleague@example.com")
    theirs = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=colleague.id,
        name="shared search",
        visibility=VISIBILITY_ACCOUNT,
    )

    found = crud_session_saved_search.get_visible(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        saved_search_id=theirs.id,
    )
    assert found is not None and found.id == theirs.id
    assert (
        crud_session_saved_search.get_owned(
            db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            saved_search_id=theirs.id,
        )
        is None
    )


def test_the_list_is_mine_plus_the_shared_ones(db_session, test_user):
    """The visible list is the union, counted the same way it is paged."""
    colleague = _user(db_session, test_user.account_id, email="colleague@example.com")
    mine = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        name="mine",
    )
    shared = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=colleague.id,
        name="shared",
        visibility=VISIBILITY_ACCOUNT,
    )
    _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=colleague.id,
        name="theirs",
    )

    rows, total = crud_session_saved_search.list_visible(
        db_session, account_id=test_user.account_id, user_id=test_user.id
    )

    assert total == 2
    assert {row.id for row in rows} == {mine.id, shared.id}


def test_the_most_recently_run_search_is_first(db_session, test_user):
    """A list of saved questions is most useful in the order people ask them."""
    first = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        name="first",
    )
    second = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        name="second",
    )
    crud_session_saved_search.record_run(
        db_session, saved=second, now=datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
    )

    rows, _ = crud_session_saved_search.list_visible(
        db_session, account_id=test_user.account_id, user_id=test_user.id
    )

    assert [row.id for row in rows] == [second.id, first.id]


def test_one_author_cannot_reuse_a_name(db_session, test_user):
    """A saved name is a label the author picks from, so it has to be unique."""
    _save(db_session, account_id=test_user.account_id, owner_user_id=test_user.id)

    with pytest.raises(SessionSavedSearchNameConflictError):
        _save(db_session, account_id=test_user.account_id, owner_user_id=test_user.id)


def test_two_authors_may_use_the_same_name(db_session, test_user):
    """Names are unique per author, not per account."""
    colleague = _user(db_session, test_user.account_id, email="colleague@example.com")
    mine = _save(
        db_session, account_id=test_user.account_id, owner_user_id=test_user.id
    )
    theirs = _save(
        db_session, account_id=test_user.account_id, owner_user_id=colleague.id
    )

    assert mine.name == theirs.name
    assert mine.id != theirs.id


def test_renaming_onto_an_existing_name_is_refused(db_session, test_user):
    """The uniqueness rule holds on an edit as well as on a save."""
    _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        name="taken",
    )
    other = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        name="free",
    )

    with pytest.raises(SessionSavedSearchNameConflictError):
        crud_session_saved_search.update_owned(
            db_session, saved=other, values={"name": "taken"}
        )


def test_an_update_cannot_reach_columns_that_are_not_editable(db_session, test_user):
    """The account and the author are not fields an edit can move."""
    saved = _save(
        db_session, account_id=test_user.account_id, owner_user_id=test_user.id
    )
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )

    with pytest.raises(ValueError):
        crud_session_saved_search.update_owned(
            db_session, saved=saved, values={"account_id": other_account.id}
        )


def test_recording_a_run_counts_it_and_stamps_it(db_session, test_user):
    """Run counters are the evidence that saving searches was worth shipping."""
    saved = _save(
        db_session, account_id=test_user.account_id, owner_user_id=test_user.id
    )
    moment = datetime(2026, 9, 16, 10, 30, tzinfo=UTC)

    crud_session_saved_search.record_run(db_session, saved=saved, now=moment)
    crud_session_saved_search.record_run(db_session, saved=saved, now=moment)

    assert saved.run_count == 2
    assert saved.last_run_at is not None


def test_deleting_removes_only_the_saved_question(db_session, test_user):
    """Deleting a saved search deletes a question, never a session."""
    saved = _save(
        db_session, account_id=test_user.account_id, owner_user_id=test_user.id
    )
    saved_id = saved.id

    crud_session_saved_search.delete_owned(db_session, saved=saved)

    assert (
        crud_session_saved_search.get_visible(
            db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            saved_search_id=saved_id,
        )
        is None
    )
