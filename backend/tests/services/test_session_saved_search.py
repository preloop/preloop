"""Service tests for saving, restoring and re-running a saved session search.

The cases here are the ones the issue asks about by name: a filter that no
longer resolves, a filter schema that moved under a saved payload, a saved
mode that cannot run today, and ranking constants that are no longer the ones
the search was saved under.
"""

from datetime import UTC, datetime
from uuid import uuid4

from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
    crud_flow,
    crud_runtime_session,
    crud_session_saved_search,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import SessionSearchChunk
from preloop.models.models.session_search_document import (
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.schemas.session_saved_search import (
    FILTER_UNRESOLVED_API_KEY,
    FILTER_UNRESOLVED_FLOW,
    FILTER_UNRESOLVED_SOURCE_KIND,
    FILTER_UNRESOLVED_UNKNOWN_FIELD,
    SessionSavedSearchRunRequest,
)
from preloop.schemas.session_search import (
    DEGRADED_SEMANTIC_NOT_ENABLED,
    SessionSearchFilters,
)
from preloop.services import session_saved_search as service
from preloop.services import session_search_fusion
from preloop.services.session_search_fusion import ranking_identity

BASE_AT = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def _flow(db_session, account_id, *, name="Saved Search Flow"):
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"{name} Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5.4",
            "api_key": "provider-secret",
        },
        account_id=account_id,
    )
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            ai_model_id=ai_model.id,
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=account_id,
        ),
        account_id=account_id,
    )


def _session_with_text(db_session, account_id, text, *, source_id="message-1"):
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


def _save(db_session, *, account_id, owner_user_id, name="saved", **overrides):
    values = {
        "query": "ledger",
        "mode": "keyword",
        "filters": {},
        "ranking_identity": ranking_identity(),
        "max_snippets_per_session": 3,
        "include_snippet_text": True,
    }
    values.update(overrides)
    return crud_session_saved_search.create_for_user(
        db_session,
        account_id=account_id,
        owner_user_id=owner_user_id,
        name=name,
        **values,
    )


def test_a_validated_filter_object_round_trips_through_storage():
    """What is stored is the validated object, not an opaque blob."""
    flow_id = uuid4()
    filters = SessionSearchFilters(
        flow_id=flow_id, source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE
    )

    payload = service.filters_payload(filters)
    restored, unresolved = service.restore_filters(payload)

    assert payload == {
        "flow_id": str(flow_id),
        "source_kind": SOURCE_KIND_TRANSCRIPT_MESSAGE,
    }
    assert restored.flow_id == flow_id
    assert restored.source_kind == SOURCE_KIND_TRANSCRIPT_MESSAGE
    assert unresolved == []


def test_a_key_the_schema_no_longer_defines_is_named_not_dropped():
    """A saved search that outlived a schema change says which key went."""
    restored, unresolved = service.restore_filters(
        {"provider_name": "openai", "retired_key": "value"}
    )

    assert restored.provider_name == "openai"
    assert [(row.field, row.reason, row.applied) for row in unresolved] == [
        ("retired_key", FILTER_UNRESOLVED_UNKNOWN_FIELD, False)
    ]


def test_a_stored_value_the_schema_now_rejects_is_named_and_the_rest_survives():
    """One bad value does not make the whole saved search unrunnable."""
    restored, unresolved = service.restore_filters(
        {"source_kind": "a_kind_this_build_stopped_writing", "provider_name": "openai"}
    )

    assert restored.provider_name == "openai"
    assert restored.source_kind is None
    assert [(row.field, row.reason) for row in unresolved] == [
        ("source_kind", FILTER_UNRESOLVED_SOURCE_KIND)
    ]


def test_a_deleted_flow_is_reported_and_the_filter_is_still_applied(
    db_session, test_user
):
    """Widening a search the caller did not widen would be the wrong answer."""
    filters = SessionSearchFilters(flow_id=uuid4())

    unresolved = service.unresolved_references(
        db_session, account_id=test_user.account_id, filters=filters
    )

    assert [(row.field, row.reason, row.applied) for row in unresolved] == [
        ("flow_id", FILTER_UNRESOLVED_FLOW, True)
    ]


def test_a_flow_that_still_exists_is_not_reported(db_session, test_user):
    """Nothing is reported while the saved search still names live rows."""
    flow = _flow(db_session, test_user.account_id)

    unresolved = service.unresolved_references(
        db_session,
        account_id=test_user.account_id,
        filters=SessionSearchFilters(flow_id=flow.id),
    )

    assert unresolved == []


def test_another_accounts_flow_reads_as_missing(db_session, test_user):
    """Resolution is account scoped, so a foreign id never resolves."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    theirs = _flow(db_session, other_account.id, name="Their Flow")

    unresolved = service.unresolved_references(
        db_session,
        account_id=test_user.account_id,
        filters=SessionSearchFilters(flow_id=theirs.id),
    )

    assert [row.reason for row in unresolved] == [FILTER_UNRESOLVED_FLOW]


def test_a_rotated_api_key_is_reported(db_session, test_user):
    """The other reference a saved filter can hold is an api key."""
    unresolved = service.unresolved_references(
        db_session,
        account_id=test_user.account_id,
        filters=SessionSearchFilters(api_key_id=uuid4()),
    )

    assert [(row.field, row.reason) for row in unresolved] == [
        ("api_key_id", FILTER_UNRESOLVED_API_KEY)
    ]


def test_a_live_api_key_is_not_reported(db_session, test_user):
    """A key that still exists resolves, and is account scoped like the flow."""
    api_key, _token = crud_api_key.create_runtime_key(
        db_session,
        name="saved-search-key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        commit=False,
    )
    db_session.flush()

    unresolved = service.unresolved_references(
        db_session,
        account_id=test_user.account_id,
        filters=SessionSearchFilters(api_key_id=api_key.id),
    )

    assert unresolved == []


def test_running_a_saved_search_answers_the_saved_question(db_session, test_user):
    """The run is the search endpoint's answer to the stored question."""
    _session_with_text(db_session, test_user.account_id, "the ledger reconciled")
    saved = _save(
        db_session, account_id=test_user.account_id, owner_user_id=test_user.id
    )

    response = service.run_saved_search(
        db_session,
        account_id=test_user.account_id,
        caller_user_id=test_user.id,
        saved=saved,
        paging=SessionSavedSearchRunRequest(),
    )

    assert response.search.query == "ledger"
    assert response.search.total == 1
    assert response.unresolved_filters == []
    assert response.saved_search.owned_by_caller is True


def test_a_run_is_counted_even_when_it_returns_nothing(db_session, test_user):
    """An empty answer is still somebody asking the question."""
    saved = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        query="nothing will match this",
    )

    service.run_saved_search(
        db_session,
        account_id=test_user.account_id,
        caller_user_id=test_user.id,
        saved=saved,
        paging=SessionSavedSearchRunRequest(),
        now=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
    )

    assert saved.run_count == 1
    assert saved.last_run_at is not None


def test_a_saved_semantic_search_degrades_instead_of_failing(db_session, test_user):
    """A saved mode the deployment cannot serve is answered, not refused."""
    _session_with_text(db_session, test_user.account_id, "the ledger reconciled")
    saved = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        mode="hybrid",
    )

    response = service.run_saved_search(
        db_session,
        account_id=test_user.account_id,
        caller_user_id=test_user.id,
        saved=saved,
        paging=SessionSavedSearchRunRequest(),
    )

    assert response.search.mode == "hybrid"
    assert response.search.effective_mode == "keyword"
    assert DEGRADED_SEMANTIC_NOT_ENABLED in response.search.degraded.reasons
    assert response.search.total == 1


def test_a_run_applies_the_saved_filters(db_session, test_user):
    """The saved filters are what the run searches with, not a wider set."""
    _session_with_text(db_session, test_user.account_id, "the ledger reconciled")
    saved = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        filters={"flow_id": str(uuid4())},
    )

    response = service.run_saved_search(
        db_session,
        account_id=test_user.account_id,
        caller_user_id=test_user.id,
        saved=saved,
        paging=SessionSavedSearchRunRequest(),
    )

    assert response.search.total == 0
    assert [row.reason for row in response.unresolved_filters] == [
        FILTER_UNRESOLVED_FLOW
    ]
    assert response.unresolved_filters[0].applied is True


def test_moved_ranking_constants_are_reported_on_the_saved_search(
    db_session, test_user, monkeypatch
):
    """Nothing is pinned, so a changed ordering is stated instead."""
    saved = _save(
        db_session,
        account_id=test_user.account_id,
        owner_user_id=test_user.id,
        ranking_identity="rrf-k=30;kw=1;sem=1;floor=0.2",
    )

    read = service.to_read(saved, caller_user_id=test_user.id)

    assert read.ranking_changed is True

    monkeypatch.setattr(service, "ranking_identity", lambda: saved.ranking_identity)
    assert service.to_read(saved, caller_user_id=test_user.id).ranking_changed is False


def test_a_colleague_running_a_shared_search_is_not_its_owner(db_session, test_user):
    """``owned_by_caller`` is about who may edit it, not who may run it."""
    saved = _save(
        db_session, account_id=test_user.account_id, owner_user_id=test_user.id
    )

    read = service.to_read(saved, caller_user_id=uuid4())

    assert read.owned_by_caller is False


def test_the_ranking_identity_moves_when_a_ranking_constant_moves(monkeypatch):
    """Derived, not hand versioned, so tuning a constant cannot go unnoticed."""
    before = ranking_identity()

    monkeypatch.setattr(session_search_fusion, "RRF_K", 30.0)

    assert ranking_identity() != before
