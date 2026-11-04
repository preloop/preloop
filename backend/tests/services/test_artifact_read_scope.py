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
    assert artifacts.get(db_session, **args) is not None


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
    )
    result = search(db_session, caller=caller, arguments={})
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
