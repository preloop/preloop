"""SQL enforcement covers every artifact reader, including facets and counts."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.models.crud import runtime_session_artifact as artifacts
from preloop.plugins.base import get_plugin_manager
from preloop.services import artifact_access


@pytest.fixture
def scoped(db_session, test_user, monkeypatch):
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id=str(uuid4()),
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()
    rows = []
    for site, kind in [
        ("north", "transcript"),
        ("south", "transcript"),
        ("north", "document"),
    ]:
        rows.append(
            artifacts.store(
                db_session,
                account_id=test_user.account_id,
                runtime_session_id=session.id,
                kind=kind,
                source="synthetic",
                source_ref=None,
                content_type="text/plain",
                plaintext=b"Synthetic text",
                manifest={},
                labels={"site": site},
            )
        )
    scopes = [{"labels": {"site": ["north"]}, "kinds": ["transcript"]}]
    audits = []

    class Policy:
        def resolve(self, db, *, account_id, subject):
            return scopes

        def audit(self, db, *, account_id, subject, artifacts):
            audits.extend(artifacts)

    monkeypatch.setitem(
        get_plugin_manager()._services, "artifact_read_policy", Policy()
    )
    artifact_access.bind_reader(
        db_session, account_id=test_user.account_id, user_id=test_user.id
    )
    return session, rows, scopes, audits


def test_all_reads_and_facets_intersect_scope(db_session, test_user, scoped):
    session, rows, _, audits = scoped
    account_id = test_user.account_id
    allowed, denied, wrong_kind = rows
    assert (
        artifacts.get(db_session, account_id=account_id, artifact_id=denied.id) is None
    )
    assert (
        artifacts.get(db_session, account_id=account_id, artifact_id=wrong_kind.id)
        is None
    )
    assert (
        artifacts.get(db_session, account_id=account_id, artifact_id=allowed.id).id
        == allowed.id
    )
    assert [
        r.id
        for r in artifacts.list_for_session(
            db_session, account_id=account_id, runtime_session_id=session.id
        )
    ] == [allowed.id]
    assert [
        r.id
        for r in artifacts.list_page_for_session(
            db_session, account_id=account_id, runtime_session_id=session.id, limit=1
        )
    ] == [allowed.id]
    assert [
        r[0].id
        for r in artifacts.search_page(db_session, account_id=account_id, limit=1)
    ] == [allowed.id]
    assert [
        r[0].id
        for r in artifacts.search_account(db_session, account_id=account_id, limit=1)
    ] == [allowed.id]
    assert artifacts.search_account_facets(db_session, account_id=account_id) == (
        {"transcript": 1},
        {"north": 1},
        False,
    )
    assert artifacts.available_counts_by_session(
        db_session, account_id=account_id, runtime_session_ids=[session.id]
    ) == {str(session.id): {"transcript": 1}}
    assert audits and {r.id for r in audits} == {allowed.id}


def test_revocation_and_malformed_scope_deny_immediately(db_session, test_user, scoped):
    _, rows, scopes, _ = scoped
    args = dict(account_id=test_user.account_id, artifact_id=rows[0].id)
    assert artifacts.get(db_session, **args) is not None
    for invalid in [
        None,
        {},
        {"labels": {}, "kinds": []},
        {"labels": {"site": "north"}, "kinds": ["transcript"]},
    ]:
        scopes[:] = [invalid]
        assert artifacts.get(db_session, **args) is None
    scopes[:] = []
    assert artifacts.get(db_session, **args) is None


def test_account_binding_cannot_be_reused(db_session, scoped):
    with pytest.raises(ValueError, match="account"):
        artifacts.get(db_session, account_id=uuid4(), artifact_id=uuid4())


def test_byte_route_returns_404_and_tools_hide_nonmatching(
    db_session, test_user, scoped
):
    from fastapi import HTTPException
    from preloop.api.endpoints.account import get_account_session_artifact
    from preloop.models.crud import crud_account
    from preloop.services.agent_artifact_read import Caller, get, search

    session, rows, _, _ = scoped
    session.runtime_principal_id = "synthetic-agent"
    db_session.flush()
    account = crud_account.get(db_session, id=test_user.account_id)
    with pytest.raises(HTTPException) as error:
        get_account_session_artifact(
            runtime_session_id=str(session.id),
            artifact_id=str(rows[1].id),
            account=account,
            current_user=test_user,
            db=db_session,
        )
    assert error.value.status_code == 404
    caller = Caller(
        account_id=account.id,
        runtime_principal_id="synthetic-agent",
        api_key_id=None,
        managed_agent_id=None,
        user_id=str(test_user.id),
        runtime_session_id=str(session.id),
    )
    result = search(db_session, caller=caller, arguments={})
    assert (
        db_session.info["artifact_reader"]["runtime_principal_id"] == "synthetic-agent"
    )
    assert db_session.info["artifact_reader"]["runtime_session_id"] == str(session.id)
    assert not result.is_error
    assert str(rows[0].id) in str(result.structured)
    assert str(rows[1].id) not in str(result.structured)
    assert (
        get(
            db_session, caller=caller, arguments={"artifact_id": str(rows[1].id)}
        ).structured["error"]["code"]
        == "artifact_not_found"
    )
    assert not get(
        db_session, caller=caller, arguments={"artifact_id": str(rows[0].id)}
    ).is_error


def test_scope_intersection_values_and_export(db_session, test_user, scoped):
    from datetime import timedelta
    from preloop.models.crud.media_sync import recording_rows

    session, rows, scopes, _ = scoped
    args = {"account_id": test_user.account_id}
    scopes[0]["labels"]["site"] = ["north", "south"]
    assert len(artifacts.search_account(db_session, **args, limit=10)) == 2
    scopes.append({"labels": {"site": ["north"]}, "kinds": ["transcript"]})
    exported = artifacts.export_rows(
        db_session,
        **args,
        start=datetime.now(UTC) - timedelta(days=1),
        end=datetime.now(UTC) + timedelta(days=1),
        limit=10,
    )
    assert [row.id for row, _ in exported] == [rows[0].id]
    assert recording_rows(db_session, **args, session_id=session.id) == []
    scopes.append({"labels": {"missing": ["value"]}, "kinds": ["transcript"]})
    assert artifacts.search_account(db_session, **args, limit=10) == []


def test_session_corpus_cannot_bypass_artifact_scope(db_session, test_user, scoped):
    from preloop.models.crud import crud_session_search_document
    from preloop.services.session_search_index import index_artifact_text

    _, rows, scopes, audits = scoped
    for row in rows:
        index_artifact_text(db_session, row, commit=False)
    db_session.flush()
    hits = crud_session_search_document.search_account_hits(
        db_session, account_id=test_user.account_id, query="Synthetic"
    )
    assert {hit.source_id for hit in hits} == {str(rows[0].id)}
    ranked, count = crud_session_search_document.search_sessions_ranked(
        db_session, account_id=test_user.account_id, query="Synthetic"
    )
    assert count == 1
    assert {hit.source_id for session in ranked for hit in session.snippets} == {
        str(rows[0].id)
    }
    assert audits and {row.id for row in audits} == {rows[0].id}
    scopes[:] = [None]
    assert (
        crud_session_search_document.search_account_hits(
            db_session, account_id=test_user.account_id, query="Synthetic"
        )
        == []
    )


def test_deposit_link_cannot_copy_hidden_artifact(
    db_session, test_user, scoped, monkeypatch
):
    from types import SimpleNamespace
    from preloop.services.artifact_mcp_tools import deposit_from_mcp

    session, rows, scopes, _ = scoped
    policy = get_plugin_manager().get_service("artifact_read_policy")
    monkeypatch.setattr(
        policy,
        "resolve",
        lambda db, *, account_id, subject: scopes if subject.get("user_id") else None,
    )
    db_session.info.pop("artifact_reader")
    result = deposit_from_mcp(
        db_session,
        user_context=SimpleNamespace(
            user_id=str(test_user.id), runtime_session_id=str(session.id)
        ),
        arguments={
            "name": "copy.txt",
            "kind": "transcript",
            "content": {
                "type": "resource_link",
                "uri": f"/api/v1/runtime-sessions/{session.id}/artifacts/{rows[1].id}",
            },
        },
    )
    assert result.is_error
    assert result.structured["error"]["code"] == "artifact_link_outside_session"


def test_unassigned_policy_preserves_existing_reads(
    db_session, test_user, scoped, monkeypatch
):
    _, rows, _, _ = scoped
    monkeypatch.setattr(
        get_plugin_manager().get_service("artifact_read_policy"),
        "resolve",
        lambda *args, **kwargs: None,
    )
    assert (
        artifacts.get(
            db_session, account_id=test_user.account_id, artifact_id=rows[1].id
        )
        is not None
    )


def test_media_and_session_presence_hide_excluded_labels(db_session, test_user, scoped):
    from preloop.models.crud.media_sync import recording_rows

    session, rows, scopes, _ = scoped
    hidden_session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id=str(uuid4()),
        started_at=datetime.now(UTC),
    )
    db_session.add(hidden_session)
    db_session.flush()
    # These queries use descriptors only, so change kind without replacing bytes.
    rows[0].kind = rows[1].kind = "recording"
    rows[1].runtime_session_id = hidden_session.id
    db_session.flush()
    scopes[0]["kinds"] = ["recording"]
    assert [
        r.id
        for r in recording_rows(
            db_session, account_id=test_user.account_id, session_id=session.id
        )
    ] == [rows[0].id]
    assert (
        recording_rows(
            db_session, account_id=test_user.account_id, session_id=hidden_session.id
        )
        == []
    )
    assert {
        r[0]
        for r in artifacts.sessions_with_available_artifacts(
            db_session, account_id=test_user.account_id
        ).all()
    } == {session.id}
    scopes[:] = [None]
    assert (
        artifacts.sessions_with_available_artifacts(
            db_session, account_id=test_user.account_id
        ).all()
        == []
    )


def test_deposit_parent_read_binds_authenticated_subject(
    db_session, test_user, scoped, monkeypatch
):
    from preloop.services import artifact_deposit, artifact_shapes
    from preloop.services.model_gateway_auth import (
        ModelGatewayAuthContext,
        NO_BEARER_TOKEN,
    )

    session, rows, scopes, _ = scoped
    monkeypatch.setattr(
        get_plugin_manager().get_service("artifact_read_policy"),
        "resolve",
        lambda db, *, account_id, subject: scopes if subject.get("user_id") else None,
    )
    db_session.info.pop("artifact_reader")
    with pytest.raises(
        artifact_deposit.ArtifactDepositError, match="artifact_parent_invalid"
    ):
        artifact_deposit.deposit(
            db_session,
            auth=ModelGatewayAuthContext(token=NO_BEARER_TOKEN, user=test_user),
            runtime_session_id=str(session.id),
            payload=artifact_shapes.make_payload(
                kind="transcript",
                name="child.txt",
                content_type="text/plain",
                data=b"New text",
            ),
            name="child.txt",
            parent_artifact_id=str(rows[1].id),
        )


def test_idempotent_deposit_cannot_reveal_hidden_descriptor(
    db_session, test_user, scoped
):
    from preloop.services import artifact_deposit, artifact_shapes
    from preloop.services.model_gateway_auth import (
        ModelGatewayAuthContext,
        NO_BEARER_TOKEN,
    )

    session, rows, _, _ = scoped
    rows[1].source = artifact_deposit.SOURCE_DEPOSIT
    rows[1].source_ref = "scoped-replay"
    db_session.commit()
    with pytest.raises(
        artifact_deposit.ArtifactDepositError, match="artifact_not_found"
    ):
        artifact_deposit.deposit(
            db_session,
            auth=ModelGatewayAuthContext(token=NO_BEARER_TOKEN, user=test_user),
            runtime_session_id=str(session.id),
            payload=artifact_shapes.make_payload(
                kind="transcript",
                name="new.txt",
                content_type="text/plain",
                data=b"New text",
            ),
            name="new.txt",
            idempotency_key="scoped-replay",
        )


@pytest.mark.asyncio
async def test_registered_session_search_keeps_user_scope(
    db_session, test_user, scoped, monkeypatch
):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from preloop.services.initialize_mcp import initialize_mcp_with_tools
    from preloop.services.session_search_index import index_artifact_text

    session, rows, scopes, audits = scoped
    hidden_session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id=str(uuid4()),
        started_at=datetime.now(UTC),
    )
    db_session.add(hidden_session)
    db_session.flush()
    for row in rows[1:]:
        row.runtime_session_id = hidden_session.id
    for row in rows:
        index_artifact_text(db_session, row, commit=False)
    db_session.flush()
    monkeypatch.setattr(
        get_plugin_manager().get_service("artifact_read_policy"),
        "resolve",
        lambda db, *, account_id, subject: scopes
        if subject.get("user_id") == str(test_user.id)
        else None,
    )
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(
            account_id=test_user.account_id,
            user_id=str(test_user.id),
            runtime_principal_id="synthetic-agent",
            runtime_session_id=str(session.id),
        ),
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        "preloop.services.agent_session_search.account_scope_granted",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([db_session])
    )
    monkeypatch.setattr(db_session, "close", lambda: None)
    tool = await initialize_mcp_with_tools().get_tool("search_sessions")
    result = json.loads(
        await tool.fn(query="Synthetic", scope="account", mode="keyword")
    )
    assert result["total"] == 1
    assert audits and {row.id for row in audits} == {rows[0].id}
    identity = db_session.info["artifact_reader"]
    assert identity["runtime_principal_id"] == "synthetic-agent"
    assert identity["runtime_session_id"] == str(session.id)


def test_gateway_list_preserves_runtime_scope(
    db_session, test_user, scoped, monkeypatch
):
    from preloop.models.crud import crud_api_key
    from preloop.api.endpoints.runtime_session_artifacts import (
        list_runtime_session_artifacts,
    )
    from preloop.services.model_gateway_auth import (
        ModelGatewayAuthContext,
        NO_BEARER_TOKEN,
    )

    session, rows, scopes, _ = scoped
    key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Artifact reader",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "runtime_session_id": str(session.id),
            "runtime_principal": {"id": "synthetic-agent", "type": "agent"},
        },
    )
    monkeypatch.setattr(
        get_plugin_manager().get_service("artifact_read_policy"),
        "resolve",
        lambda db, *, account_id, subject: scopes
        if subject.get("runtime_principal_id") == "synthetic-agent"
        else None,
    )
    result = list_runtime_session_artifacts(
        runtime_session_id=str(session.id),
        kind=None,
        label=[],
        limit=20,
        cursor=None,
        db=db_session,
        auth=ModelGatewayAuthContext(
            token=NO_BEARER_TOKEN, user=test_user, api_key=key
        ),
    )
    assert {str(row.id) for row in result.items} == {str(rows[0].id)}


def test_corpus_always_offers_returned_artifacts_to_audit_sink(
    db_session, test_user, scoped, monkeypatch
):
    from preloop.models.crud import crud_session_search_document
    from preloop.services.session_search_index import index_artifact_text

    _, rows, _, audits = scoped
    for row in rows:
        index_artifact_text(db_session, row, commit=False)
    db_session.flush()
    monkeypatch.setattr(
        get_plugin_manager().get_service("artifact_read_policy"),
        "resolve",
        lambda *args, **kwargs: None,
    )
    hits = crud_session_search_document.search_account_hits(
        db_session, account_id=test_user.account_id, query="Synthetic"
    )
    assert len(hits) == 3
    assert {row.id for row in audits} == {row.id for row in rows}


def test_corpus_vectors_previews_and_coverage_share_artifact_scope(
    db_session, test_user, scoped, monkeypatch
):
    from datetime import timedelta
    from unittest.mock import Mock
    from preloop.models.crud import crud_session_search_document as corpus
    from preloop.models.models.session_search_document import EMBEDDING_DIMENSIONS
    from preloop.services.session_search_index import index_artifact_text

    session, rows, _, audits = scoped
    hidden_session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id=str(uuid4()),
        started_at=datetime.now(UTC),
    )
    db_session.add(hidden_session)
    db_session.flush()
    rows[1].runtime_session_id = hidden_session.id
    for row in rows:
        index_artifact_text(db_session, row, commit=False)
    db_session.flush()
    documents = (
        db_session.query(models.SessionSearchDocument)
        .filter(models.SessionSearchDocument.account_id == test_user.account_id)
        .all()
    )
    by_artifact = {row.source_id: row for row in documents}
    allowed = by_artifact[str(rows[0].id)]
    by_artifact[str(rows[1].id)].occurred_at = allowed.occurred_at - timedelta(days=3)
    by_artifact[str(rows[2].id)].occurred_at = allowed.occurred_at + timedelta(days=3)
    vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
    model = "synthetic:scope-test"
    corpus.store_embeddings(
        db_session, vectors=[(row, vector) for row in documents], model_identity=model
    )
    db_session.flush()
    args = {"account_id": test_user.account_id}
    hits = corpus.search_vector_chunks(
        db_session, **args, embedding=vector, embedding_model=model
    )
    assert {hit.source_id for hit in hits} == {str(rows[0].id)}
    probes = corpus.session_probe_chunks(
        db_session, **args, runtime_session_id=session.id, embedding_model=model
    )
    assert {hit.source_id for hit in probes} == {str(rows[0].id)}
    assert (
        corpus.session_probe_chunks(
            db_session,
            **args,
            runtime_session_id=hidden_session.id,
            embedding_model=model,
        )
        == []
    )
    policy = get_plugin_manager().get_service("artifact_read_policy")
    resolve = Mock(wraps=policy.resolve)
    monkeypatch.setattr(policy, "resolve", resolve)
    # A synthetic external probe session keeps both allowed and hidden candidates
    # eligible for this query's session boundary; only the label predicate differs.
    similar = corpus.similar_chunks(
        db_session, **args, probes=probes * 3, exclude_session_id=uuid4()
    )
    assert {hit.source_id for hit in similar} == {str(rows[0].id)}
    assert resolve.call_count == 1
    ids = [row.id for row in documents]
    assert set(
        corpus.snippet_text_for_documents(
            db_session, **args, document_ids=ids, query="Synthetic"
        )
    ) == {str(allowed.id)}
    assert set(corpus.chunk_previews(db_session, **args, document_ids=ids)) == {
        str(allowed.id)
    }
    assert set(
        corpus.artifact_excerpts(
            db_session, **args, artifact_ids=[row.id for row in rows], query="Synthetic"
        )
    ) == {str(rows[0].id)}
    coverage = corpus.embedding_coverage(db_session, **args, embedding_model=model)
    assert (coverage.vectors, coverage.model_vectors, coverage.pending) == (1, 1, 0)
    assert (
        corpus.session_embedding_state(
            db_session, **args, runtime_session_id=session.id
        ).chunks
        == 1
    )
    assert (
        corpus.session_embedding_state(
            db_session, **args, runtime_session_id=hidden_session.id
        ).chunks
        == 0
    )
    assert corpus.earliest_occurred_at(db_session, **args) == allowed.occurred_at
    assert corpus.indexed_through(db_session, **args) == allowed.occurred_at
    assert audits and {row.id for row in audits} == {rows[0].id}
