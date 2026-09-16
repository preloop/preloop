"""Endpoint tests for saved session searches.

The account bound, the visibility rules and the shape of a run are asserted
here because they are what a caller depends on: who can see a saved search,
who can change it, and what a re-run says about the parts of the question that
no longer hold.
"""

from datetime import datetime, timezone
from uuid import uuid4

from preloop.api.auth import get_current_active_user
from preloop.models.crud import (
    crud_account,
    crud_runtime_session,
    crud_session_search_document,
    crud_user,
)
from preloop.models.crud.session_search_document import SessionSearchChunk
from preloop.models.models.session_search_document import (
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.schemas.session_saved_search import FILTER_UNRESOLVED_FLOW
from preloop.schemas.session_search import DEGRADED_SEMANTIC_NOT_ENABLED

SAVED_URL = "/api/v1/runtime-sessions/search/saved"
BASE_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def _colleague(db_session, account_id, *, email="colleague@example.com"):
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


def _act_as(app, user):
    """Run the next requests as another user in the same account."""
    app.dependency_overrides[get_current_active_user] = lambda: user


def _indexed_session(db_session, account_id, text, *, source_id="message-1"):
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=BASE_AT,
        last_activity_at=BASE_AT,
    )
    crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=source_id,
        occurred_at=BASE_AT,
        chunks=[SessionSearchChunk(content=text, chunk_index=0, role="assistant")],
    )
    db_session.flush()
    return session


def _create(client, **overrides):
    body = {"name": "billing questions", "query": "billing migration"}
    body.update(overrides)
    return client.post(SAVED_URL, json=body)


def test_a_saved_search_is_private_until_it_is_shared(client):
    """Saving never shares on the author's behalf."""
    response = _create(client)

    assert response.status_code == 201
    payload = response.json()
    assert payload["visibility"] == "private"
    assert payload["shared_at"] is None
    assert payload["owned_by_caller"] is True
    assert payload["run_count"] == 0
    assert payload["mode"] == "keyword"


def test_an_unknown_filter_key_is_refused_at_save_time(client):
    """An unrunnable search cannot be stored."""
    response = _create(client, filters={"not_a_filter": "value"})

    assert response.status_code == 422


def test_a_name_cannot_be_reused_by_the_same_author(client):
    """The saved list is chosen from by name, so names are unique per author."""
    assert _create(client).status_code == 201

    duplicate = _create(client)

    assert duplicate.status_code == 409


def test_the_list_holds_mine_and_the_shared_ones_only(
    client, app, db_session, test_user
):
    """A colleague's private search is not in anybody else's list."""
    colleague = _colleague(db_session, test_user.account_id)
    _act_as(app, colleague)
    private = _create(client, name="their private").json()
    shared = _create(client, name="their shared", visibility="account").json()
    _act_as(app, test_user)
    mine = _create(client, name="mine").json()

    response = client.get(SAVED_URL)

    assert response.status_code == 200
    payload = response.json()
    ids = [item["id"] for item in payload["items"]]
    assert payload["total"] == 2
    assert set(ids) == {mine["id"], shared["id"]}
    assert private["id"] not in ids


def test_sharing_stamps_when_it_was_shared_and_unsharing_clears_it(client):
    """ "Shared since" is never a date from a share that was already undone."""
    saved = _create(client).json()

    shared = client.patch(f"{SAVED_URL}/{saved['id']}", json={"visibility": "account"})
    assert shared.status_code == 200
    assert shared.json()["visibility"] == "account"
    assert shared.json()["shared_at"] is not None

    unshared = client.patch(
        f"{SAVED_URL}/{saved['id']}", json={"visibility": "private"}
    )
    assert unshared.status_code == 200
    assert unshared.json()["shared_at"] is None


def test_renaming_keeps_the_question(client):
    """A rename is a label change and nothing else."""
    saved = _create(client).json()

    renamed = client.patch(f"{SAVED_URL}/{saved['id']}", json={"name": "renamed"})

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "renamed"
    assert renamed.json()["query"] == saved["query"]


def test_only_the_author_can_change_a_shared_search(client, app, db_session, test_user):
    """Sharing lets a colleague run it, not edit it."""
    shared = _create(client, visibility="account").json()
    colleague = _colleague(db_session, test_user.account_id)
    _act_as(app, colleague)

    renamed = client.patch(f"{SAVED_URL}/{shared['id']}", json={"name": "not yours"})
    deleted = client.delete(f"{SAVED_URL}/{shared['id']}")

    assert renamed.status_code == 403
    assert deleted.status_code == 403


def test_a_colleague_can_run_a_shared_search(client, app, db_session, test_user):
    """Running somebody else's shared question is the point of sharing it."""
    _indexed_session(db_session, test_user.account_id, "the ledger reconciled")
    shared = _create(client, query="ledger", visibility="account").json()
    colleague = _colleague(db_session, test_user.account_id)
    _act_as(app, colleague)

    response = client.post(f"{SAVED_URL}/{shared['id']}/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["search"]["total"] == 1
    assert payload["saved_search"]["owned_by_caller"] is False


def test_a_colleagues_private_search_is_not_found(client, app, db_session, test_user):
    """A private saved search is not found rather than forbidden."""
    private = _create(client).json()
    colleague = _colleague(db_session, test_user.account_id)
    _act_as(app, colleague)

    read = client.get(f"{SAVED_URL}/{private['id']}")
    run = client.post(f"{SAVED_URL}/{private['id']}/run")

    assert read.status_code == 404
    assert run.status_code == 404


def test_another_accounts_saved_search_is_never_reachable(
    client, app, db_session, test_user
):
    """The account bound holds on every route, including the run."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    outsider = _colleague(db_session, other_account.id, email="outsider@example.com")
    mine = _create(client, visibility="account").json()
    _act_as(app, outsider)

    read = client.get(f"{SAVED_URL}/{mine['id']}")
    run = client.post(f"{SAVED_URL}/{mine['id']}/run")
    listing = client.get(SAVED_URL)

    assert read.status_code == 404
    assert run.status_code == 404
    assert listing.json()["total"] == 0


def test_running_a_saved_search_counts_the_run(client, db_session, test_user):
    """The counters are what say whether saved searches earn their place."""
    _indexed_session(db_session, test_user.account_id, "the ledger reconciled")
    saved = _create(client, query="ledger").json()

    first = client.post(f"{SAVED_URL}/{saved['id']}/run")
    client.post(f"{SAVED_URL}/{saved['id']}/run")
    read = client.get(f"{SAVED_URL}/{saved['id']}").json()

    assert first.status_code == 200
    assert first.json()["saved_search"]["run_count"] == 1
    assert read["run_count"] == 2
    assert read["last_run_at"] is not None


def test_a_run_pages_without_changing_the_saved_question(client, db_session, test_user):
    """Paging is a property of the viewing, not of the saved search."""
    _indexed_session(
        db_session, test_user.account_id, "the ledger reconciled", source_id="one"
    )
    _indexed_session(
        db_session, test_user.account_id, "the ledger reopened", source_id="two"
    )
    saved = _create(client, query="ledger").json()

    first_page = client.post(
        f"{SAVED_URL}/{saved['id']}/run", json={"limit": 1, "offset": 0}
    ).json()
    second_page = client.post(
        f"{SAVED_URL}/{saved['id']}/run", json={"limit": 1, "offset": 1}
    ).json()

    assert first_page["search"]["total"] == 2
    assert len(first_page["search"]["results"]) == 1
    assert len(second_page["search"]["results"]) == 1
    assert (
        first_page["search"]["results"][0]["runtime_session_id"]
        != second_page["search"]["results"][0]["runtime_session_id"]
    )


def test_a_saved_search_naming_a_deleted_flow_still_runs_and_says_so(
    client, db_session, test_user
):
    """It runs, it returns nothing, and it names the filter that is now empty."""
    _indexed_session(db_session, test_user.account_id, "the ledger reconciled")
    saved = _create(client, query="ledger", filters={"flow_id": str(uuid4())}).json()

    response = client.post(f"{SAVED_URL}/{saved['id']}/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["search"]["total"] == 0
    assert [row["field"] for row in payload["unresolved_filters"]] == ["flow_id"]
    assert payload["unresolved_filters"][0]["reason"] == FILTER_UNRESOLVED_FLOW
    assert payload["unresolved_filters"][0]["applied"] is True


def test_a_saved_semantic_search_comes_back_degraded_not_as_an_error(
    client, db_session, test_user
):
    """The saved mode is unavailable, so the answer says so and still answers."""
    _indexed_session(db_session, test_user.account_id, "the ledger reconciled")
    saved = _create(client, query="ledger", mode="hybrid").json()

    response = client.post(f"{SAVED_URL}/{saved['id']}/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["saved_search"]["mode"] == "hybrid"
    assert payload["search"]["effective_mode"] == "keyword"
    assert DEGRADED_SEMANTIC_NOT_ENABLED in payload["search"]["degraded"]["reasons"]
    assert payload["search"]["total"] == 1


def test_a_deleted_saved_search_is_gone_and_its_sessions_are_not(
    client, db_session, test_user
):
    """Deleting a question never deletes what it found."""
    _indexed_session(db_session, test_user.account_id, "the ledger reconciled")
    saved = _create(client, query="ledger").json()

    deleted = client.delete(f"{SAVED_URL}/{saved['id']}")

    assert deleted.status_code == 204
    assert client.get(f"{SAVED_URL}/{saved['id']}").status_code == 404
    still_searchable = client.post(
        "/api/v1/runtime-sessions/search", json={"query": "ledger"}
    )
    assert still_searchable.json()["total"] == 1


def test_an_empty_edit_is_refused(client):
    """A no-op is not reported as a change."""
    saved = _create(client).json()

    response = client.patch(f"{SAVED_URL}/{saved['id']}", json={})

    assert response.status_code in (400, 422)


def test_the_saved_list_path_is_not_read_as_a_session_id(client):
    """The route sits under the search path so the session routes cannot eat it."""
    response = client.get(SAVED_URL)

    assert response.status_code == 200
    assert "items" in response.json()
