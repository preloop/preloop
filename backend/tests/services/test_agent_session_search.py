"""What an agent is allowed to see when it searches the session corpus (#658).

The ranking itself is pinned in the CRUD tests. What is pinned here is the
part that is about the agent rather than the query: that the default scope is
its own sessions and not the account's, that asking for the account is refused
rather than narrowed, that the answer cannot grow past the context budget, and
that an honest empty answer stays an empty answer.
"""

import json
from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_account,
    crud_audit_log,
    crud_runtime_session,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import SessionSearchChunk
from preloop.models.models.session_search_document import (
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)
from preloop.schemas.session_search import DEGRADED_SEMANTIC_NOT_ENABLED
from preloop.services import agent_session_search, session_search_audit
from preloop.services.agent_session_search import (
    ACCOUNT_SCOPE_GRANT,
    MAX_RESPONSE_CHARS,
    REFUSAL_ACCOUNT_SCOPE_NOT_GRANTED,
    REFUSAL_INVALID_REQUEST,
    REFUSAL_NO_AGENT_IDENTITY,
    REFUSAL_UNKNOWN_SCOPE,
    account_scope_granted,
    search_for_agent,
)
from preloop.services.subject_governance import (
    SUBJECT_GOVERNANCE_KEY,
    SUBJECT_TYPE_MANAGED_AGENTS,
)

BASE_AT = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)

CALLER = "agent-caller"
OTHER = "agent-other"


def _session(db_session, account_id, source_id, principal, *, started_at=BASE_AT):
    return crud_runtime_session.upsert_by_source(
        db_session,
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


def _write(
    db_session,
    account_id,
    session,
    *texts,
    principal,
    source_id="message-1",
    occurred_at=BASE_AT,
):
    return crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=source_id,
        occurred_at=occurred_at,
        chunks=[
            SessionSearchChunk(
                content=text,
                chunk_index=index,
                role="assistant",
                runtime_principal_id=principal,
            )
            for index, text in enumerate(texts)
        ],
    )


def _search(db_session, account_id, query, **kwargs):
    kwargs.setdefault("runtime_principal_id", CALLER)
    kwargs.setdefault("subject_context", {"managed_agent_id": "agent-row-1"})
    return search_for_agent(db_session, account_id=account_id, query=query, **kwargs)


def _references(answer):
    return {result["session_reference"] for result in answer["results"]}


# --- the default scope is the calling agent, and only it -------------------


def test_default_scope_returns_only_the_calling_agents_sessions(db_session, test_user):
    """Another agent ran the same migration; that is not this agent's history."""
    account_id = str(test_user.account_id)
    mine = _session(db_session, account_id, "mine", CALLER)
    theirs = _session(db_session, account_id, "theirs", OTHER)
    _write(
        db_session,
        account_id,
        mine,
        "ran the vacuum migration on the reporting database",
        principal=CALLER,
    )
    _write(
        db_session,
        account_id,
        theirs,
        "ran the vacuum migration on the billing database",
        principal=OTHER,
        source_id="message-2",
    )

    answer = _search(db_session, account_id, "vacuum migration")

    assert "refused" not in answer
    assert _references(answer) == {"mine"}
    assert answer["total"] == 1
    assert answer["scope"] == "own"
    assert str(theirs.id) not in json.dumps(answer)


def test_another_accounts_sessions_are_never_reachable(db_session, test_user):
    """The account bound is the query's, and the agent scope narrows inside it."""
    account_id = str(test_user.account_id)
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    mine = _session(db_session, account_id, "mine", CALLER)
    theirs = _session(db_session, str(other_account.id), "theirs", CALLER)
    _write(
        db_session,
        account_id,
        mine,
        "restarted the ingest worker after the deploy",
        principal=CALLER,
    )
    _write(
        db_session,
        str(other_account.id),
        theirs,
        "restarted the ingest worker after the deploy",
        principal=CALLER,
        source_id="message-2",
    )

    answer = _search(db_session, account_id, "ingest worker")

    assert _references(answer) == {"mine"}


def test_a_caller_without_an_agent_identity_is_refused_not_widened(
    db_session, test_user
):
    """No identity means no 'own' set, which is a refusal and not everything."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id, "mine", CALLER)
    _write(db_session, account_id, session, "the deploy finished", principal=CALLER)

    answer = _search(db_session, account_id, "deploy", runtime_principal_id=None)

    assert answer["refused"] is True
    assert answer["reason"] == REFUSAL_NO_AGENT_IDENTITY
    assert answer["results"] == []


# --- the account wide scope is a grant, not a parameter --------------------


def test_account_scope_without_the_grant_is_refused_with_a_reason(
    db_session, test_user
):
    """Refused, not narrowed: an empty answer would read as 'never happened'."""
    account_id = str(test_user.account_id)
    theirs = _session(db_session, account_id, "theirs", OTHER)
    _write(
        db_session,
        account_id,
        theirs,
        "rotated the signing key last Tuesday",
        principal=OTHER,
    )

    answer = _search(db_session, account_id, "signing key", scope="account")

    assert answer["refused"] is True
    assert answer["reason"] == REFUSAL_ACCOUNT_SCOPE_NOT_GRANTED
    assert ACCOUNT_SCOPE_GRANT in answer["detail"]
    assert "scope 'own'" in answer["detail"]
    assert answer["results"] == []
    assert "total" not in answer


def test_the_grant_widens_the_scope_without_a_code_change(db_session, test_user):
    """Widening is configuration: the same call answers once the grant exists."""
    account_id = str(test_user.account_id)
    mine = _session(db_session, account_id, "mine", CALLER)
    theirs = _session(db_session, account_id, "theirs", OTHER)
    _write(
        db_session,
        account_id,
        mine,
        "rotated the signing key on the gateway",
        principal=CALLER,
    )
    _write(
        db_session,
        account_id,
        theirs,
        "rotated the signing key on the runner",
        principal=OTHER,
        source_id="message-2",
    )
    crud_account.update(
        db_session,
        db_obj=crud_account.get(db_session, id=account_id),
        obj_in={
            "meta_data": {
                SUBJECT_GOVERNANCE_KEY: {
                    SUBJECT_TYPE_MANAGED_AGENTS: {
                        "agent-row-1": {
                            "tool_grants": {ACCOUNT_SCOPE_GRANT: True},
                        }
                    }
                }
            }
        },
    )

    answer = _search(db_session, account_id, "signing key", scope="account")

    assert "refused" not in answer
    assert _references(answer) == {"mine", "theirs"}
    assert answer["scope"] == "account"


def test_the_grant_is_read_per_subject_and_the_most_specific_wins():
    """A revoked agent stays revoked under a permissive account default."""
    meta_data = {
        SUBJECT_GOVERNANCE_KEY: {
            SUBJECT_TYPE_MANAGED_AGENTS: {
                "agent-row-1": {"tool_grants": {ACCOUNT_SCOPE_GRANT: False}}
            },
            "account_defaults": {"tool_grants": {ACCOUNT_SCOPE_GRANT: True}},
        }
    }

    assert not account_scope_granted(
        meta_data, subject_context={"managed_agent_id": "agent-row-1"}
    )
    assert account_scope_granted(
        meta_data, subject_context={"managed_agent_id": "agent-row-2"}
    )
    assert not account_scope_granted(None, subject_context={})


def test_an_unknown_scope_is_refused_rather_than_guessed(db_session, test_user):
    """A scope nobody defined is a refusal, never the widest reading of it."""
    answer = _search(
        db_session, str(test_user.account_id), "anything", scope="everything"
    )

    assert answer["refused"] is True
    assert answer["reason"] == REFUSAL_UNKNOWN_SCOPE


# --- the answer is capped, honest and compact ------------------------------


def test_the_response_is_capped_so_a_search_cannot_flood_the_context(
    db_session, test_user
):
    """Twenty fat sessions match; what comes back still fits in a turn."""
    account_id = str(test_user.account_id)
    filler = " ".join(f"detail{index:03d}" for index in range(120))
    for index in range(20):
        session = _session(
            db_session,
            account_id,
            f"session-{index:02d}",
            CALLER,
            started_at=BASE_AT + timedelta(minutes=index),
        )
        _write(
            db_session,
            account_id,
            session,
            f"checkpoint restore rehearsal {filler}",
            principal=CALLER,
            source_id=f"message-{index:02d}",
            occurred_at=BASE_AT + timedelta(minutes=index),
        )

    answer = _search(db_session, account_id, "checkpoint restore", limit=20)

    assert answer["total"] == 20
    assert len(json.dumps(answer["results"])) <= MAX_RESPONSE_CHARS
    assert answer["truncated"] is True
    assert answer["results_omitted"] == 20 - answer["returned"]
    assert answer["returned"] < 20
    for result in answer["results"]:
        assert len(result["snippet"]) <= agent_session_search.MAX_SNIPPET_CHARS


def test_a_result_says_where_when_and_why_it_matched(db_session, test_user):
    """Compact by design: a reference, a time, a snippet and a reason."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id, "mine", CALLER)
    _write(
        db_session,
        account_id,
        session,
        "took the nightly snapshot before the schema change",
        "the nightly snapshot restored cleanly",
        principal=CALLER,
    )

    answer = _search(db_session, account_id, "nightly snapshot")

    result = answer["results"][0]
    assert set(result) == {
        "runtime_session_id",
        "session_reference",
        "occurred_at",
        "snippet",
        "match_reason",
    }
    assert result["session_reference"] == "mine"
    assert "<mark>" in result["snippet"]
    assert result["match_reason"].startswith("matched in 2 turns")
    assert SOURCE_KIND_TRANSCRIPT_MESSAGE in result["match_reason"]
    assert result["occurred_at"] is not None


def test_degraded_markers_reach_the_agent(db_session, test_user):
    """A keyword miss is not proof the work was never done, and it says so."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id, "mine", CALLER)
    _write(
        db_session,
        account_id,
        session,
        "pruned the stale feature branches",
        principal=CALLER,
    )

    answer = _search(db_session, account_id, "stale branches", mode="semantic")

    assert answer["mode"] == "semantic"
    assert answer["effective_mode"] == "keyword"
    assert answer["degraded"]["semantic"] is False
    assert DEGRADED_SEMANTIC_NOT_ENABLED in answer["degraded"]["reasons"]
    assert answer["degraded"]["detail"]


def test_no_match_is_an_empty_result_set_not_an_error(db_session, test_user):
    """Nothing found is an answer. The agent needs it to stop looking."""
    account_id = str(test_user.account_id)
    session = _session(db_session, account_id, "mine", CALLER)
    _write(
        db_session,
        account_id,
        session,
        "resized the ingest queue",
        principal=CALLER,
    )

    answer = _search(db_session, account_id, "hovercraft")

    assert "refused" not in answer
    assert answer["results"] == []
    assert answer["total"] == 0
    assert answer["truncated"] is False
    assert answer["degraded"]["keyword"] is True


def test_the_time_range_narrows_the_search(db_session, test_user):
    """The agent asks about last week and does not get last quarter."""
    account_id = str(test_user.account_id)
    old = _session(
        db_session,
        account_id,
        "old",
        CALLER,
        started_at=BASE_AT - timedelta(days=90),
    )
    recent = _session(db_session, account_id, "recent", CALLER)
    _write(
        db_session,
        account_id,
        old,
        "upgraded the runner image",
        principal=CALLER,
        occurred_at=BASE_AT - timedelta(days=90),
    )
    _write(
        db_session,
        account_id,
        recent,
        "upgraded the runner image",
        principal=CALLER,
        source_id="message-2",
    )

    answer = _search(
        db_session,
        account_id,
        "runner image",
        start_date=(BASE_AT - timedelta(days=1)).isoformat(),
    )

    assert _references(answer) == {"recent"}


def test_a_time_without_an_offset_is_refused_with_the_reason(db_session, test_user):
    """Same instant everywhere or no search: a naive bound is ambiguous."""
    answer = _search(
        db_session,
        str(test_user.account_id),
        "anything",
        start_date="2026-09-01T00:00:00",
    )

    assert answer["refused"] is True
    assert answer["reason"] == REFUSAL_INVALID_REQUEST
    assert "timezone offset" in answer["detail"]


def test_the_limit_is_clamped_to_the_documented_maximum(db_session, test_user):
    """A caller asking for a hundred gets the maximum, not an error."""
    account_id = str(test_user.account_id)
    for index in range(3):
        session = _session(db_session, account_id, f"session-{index}", CALLER)
        _write(
            db_session,
            account_id,
            session,
            "tidied the orphaned artifacts",
            principal=CALLER,
            source_id=f"message-{index}",
        )

    answer = _search(db_session, account_id, "orphaned artifacts", limit=100)

    assert "refused" not in answer
    assert answer["returned"] == 3


# --- every agent search is on the record (#688) ----------------------------


def _audit_rows(db_session, account_id):
    return crud_audit_log.get_by_account(
        db_session,
        account_id=str(account_id),
        action=session_search_audit.AUDIT_ACTION,
        resource_type=session_search_audit.AUDIT_RESOURCE_TYPE,
    )


def test_an_agent_search_is_audited_with_the_agent_as_the_actor(db_session, test_user):
    """An operator has to be able to tell an agent's grep from a person's."""
    account_id = str(test_user.account_id)
    mine = _session(db_session, account_id, "mine", CALLER)
    _write(
        db_session,
        account_id,
        mine,
        "rotated the signing key on the build host",
        principal=CALLER,
    )

    _search(
        db_session,
        account_id,
        "signing key",
        subject_context={"managed_agent_id": "agent-row-1", "api_key_id": "key-1"},
    )

    rows = _audit_rows(db_session, account_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == session_search_audit.STATUS_SUCCESS
    assert row.user_id is None
    assert row.details["actor_type"] == session_search_audit.ACTOR_MANAGED_AGENT
    assert row.details["actor_managed_agent_id"] == "agent-row-1"
    assert row.details["actor_runtime_principal_id"] == CALLER
    assert row.details["source"] == session_search_audit.SOURCE_MCP
    assert row.details["scope"] == "own"
    assert row.details["result_count"] == 1
    assert row.details["query_hash"] == session_search_audit.query_hash("signing key")
    assert "signing key" not in json.dumps(row.details)


def test_a_refused_account_scope_search_is_recorded_as_denied(db_session, test_user):
    """The attempted read is the row a reviewer wants most."""
    account_id = str(test_user.account_id)

    answer = _search(db_session, account_id, "billing incident", scope="account")

    assert answer["reason"] == REFUSAL_ACCOUNT_SCOPE_NOT_GRANTED
    row = _audit_rows(db_session, account_id)[0]
    assert row.status == session_search_audit.STATUS_DENIED
    assert row.details["reason"] == REFUSAL_ACCOUNT_SCOPE_NOT_GRANTED
    assert row.details["scope"] == "account"
    assert row.details["result_count"] == 0


def test_a_search_with_no_agent_identity_is_recorded_as_denied(db_session, test_user):
    account_id = str(test_user.account_id)

    answer = _search(db_session, account_id, "anything", runtime_principal_id=None)

    assert answer["reason"] == REFUSAL_NO_AGENT_IDENTITY
    row = _audit_rows(db_session, account_id)[0]
    assert row.status == session_search_audit.STATUS_DENIED
    assert row.details["reason"] == REFUSAL_NO_AGENT_IDENTITY


def test_an_unknown_scope_and_an_invalid_request_are_both_recorded(
    db_session, test_user
):
    account_id = str(test_user.account_id)

    _search(db_session, account_id, "anything", scope="everything")
    _search(db_session, account_id, "anything", start_date="2026-09-01T00:00:00")

    reasons = {row.details["reason"] for row in _audit_rows(db_session, account_id)}
    assert reasons == {REFUSAL_UNKNOWN_SCOPE, REFUSAL_INVALID_REQUEST}


def test_an_audit_write_failure_leaves_the_agent_answer_unchanged(
    db_session, test_user, monkeypatch
):
    account_id = str(test_user.account_id)
    mine = _session(db_session, account_id, "mine", CALLER)
    _write(
        db_session,
        account_id,
        mine,
        "restarted the ingest worker",
        principal=CALLER,
    )

    def boom(*args, **kwargs):
        raise RuntimeError("audit table is read only")

    monkeypatch.setattr(crud_audit_log, "log_action", boom)

    answer = _search(db_session, account_id, "ingest worker")

    assert _references(answer) == {"mine"}
    assert _audit_rows(db_session, account_id) == []
