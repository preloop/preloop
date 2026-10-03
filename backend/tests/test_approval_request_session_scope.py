"""Approvals must be listable for ONE agent conversation, not just one account.

A live session view needs to answer "what is *this* session waiting on". Before
this filter the only route there was paging the whole account's approvals and
discarding the rest in the browser, which both leaks other sessions' asks into
the page and misses pending requests that sit past the first page.

These tests pin three things, without a database:

  1. The session filter is ANDed with the caller's account, never a replacement
     for it. Naming another account's session returns nothing, not that
     account's approvals.
  2. Omitting the filter changes nothing for existing callers.
  3. The query parameter is validated, so a typo is a 422 rather than an empty
     list that reads as "this session needs nothing".
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import approval_requests
from preloop.models.crud.approval_request import CRUDApprovalRequest
from preloop.models.db.session import get_db_session
from preloop.models.models.approval_request import ApprovalRequest


@pytest.fixture
def crud():
    return CRUDApprovalRequest(ApprovalRequest)


@pytest.fixture
def recording_session():
    """A Session stand-in that records the filter clauses it is handed.

    ``db.query(...)`` is mocked, but the clause objects are real SQLAlchemy
    expressions built from the mapped model, so they still compile. That is
    what lets these tests assert on the SQL the filter produces rather than on
    the order the CRUD happens to call ``.filter()`` in.
    """
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.offset.return_value = query
    query.limit.return_value = query
    query.all.return_value = []

    session = MagicMock()
    session.query.return_value = query
    session.filters = query
    return session


def filter_clauses(session) -> list[str]:
    return [
        str(
            call.args[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        for call in session.filters.filter.call_args_list
    ]


class TestRuntimeSessionFilter:
    def test_account_filter_is_always_present(self, crud, recording_session):
        """Account scoping is not optional; the session filter never replaces it."""
        account_id = str(uuid.uuid4())
        session_id = uuid.uuid4()

        crud.get_multi_by_account(
            recording_session,
            account_id=account_id,
            runtime_session_id=session_id,
        )

        clauses = filter_clauses(recording_session)
        assert f"approval_request.account_id = '{account_id}'" in clauses
        assert f"approval_request.runtime_session_id = '{session_id}'" in clauses

    def test_omitting_the_session_leaves_the_query_unchanged(
        self, crud, recording_session
    ):
        """Existing account-wide callers must not start filtering on sessions."""
        account_id = str(uuid.uuid4())

        crud.get_multi_by_account(
            recording_session,
            account_id=account_id,
            status="pending",
        )

        assert filter_clauses(recording_session) == [
            f"approval_request.account_id = '{account_id}'",
            "approval_request.status = 'pending'",
        ]

    def test_session_filter_combines_with_status_and_execution(
        self, crud, recording_session
    ):
        """All filters are ANDed, never short-circuited by the newest one."""
        account_id = str(uuid.uuid4())
        session_id = uuid.uuid4()
        execution_id = str(uuid.uuid4())

        crud.get_multi_by_account(
            recording_session,
            account_id=account_id,
            execution_id=execution_id,
            status="pending",
            runtime_session_id=session_id,
        )

        clauses = filter_clauses(recording_session)
        assert len(clauses) == 4
        assert f"approval_request.runtime_session_id = '{session_id}'" in clauses
        assert f"approval_request.execution_id = '{execution_id}'" in clauses
        assert "approval_request.status = 'pending'" in clauses
        assert f"approval_request.account_id = '{account_id}'" in clauses

    def test_string_and_uuid_session_ids_agree(self, crud, recording_session):
        """A JSONB uuid column compares against the string form on both sides.

        Callers reach this from a query string (str) and from resolved rows
        (UUID); normalizing inside the CRUD keeps both producing one clause
        instead of relying on the driver's coercion.
        """
        session_id = uuid.uuid4()

        crud.get_multi_by_account(
            recording_session,
            account_id=str(uuid.uuid4()),
            runtime_session_id=session_id,
        )
        from_uuid = [
            clause
            for clause in filter_clauses(recording_session)
            if "runtime_session" in clause
        ]

        crud.get_multi_by_account(
            recording_session,
            account_id=str(uuid.uuid4()),
            runtime_session_id=str(session_id),
        )
        from_str = [
            clause
            for clause in filter_clauses(recording_session)
            if "runtime_session" in clause
        ][-1:]

        assert from_uuid == [f"approval_request.runtime_session_id = '{session_id}'"]
        assert from_str == from_uuid

    def test_empty_session_id_is_treated_as_no_filter(self, crud, recording_session):
        """An empty value is not a session; filtering on '' would hide everything."""
        crud.get_multi_by_account(
            recording_session,
            account_id=str(uuid.uuid4()),
            runtime_session_id="",
        )

        assert not any(
            "runtime_session" in clause for clause in filter_clauses(recording_session)
        )


class TestListEndpointPassesTheFilter:
    @pytest.fixture
    def user(self):
        user = MagicMock()
        user.id = uuid.uuid4()
        user.account_id = str(uuid.uuid4())
        user.username = "testuser@example.com"
        user.is_active = True
        return user

    def test_session_filter_reaches_the_crud_layer(self, user):
        session_id = uuid.uuid4()
        with patch(
            "preloop.api.endpoints.approval_requests.crud_approval_request"
        ) as crud_layer:
            crud_layer.get_multi_by_account.return_value = []
            approval_requests.list_approval_requests(
                status="pending",
                execution_id=None,
                runtime_session_id=session_id,
                limit=50,
                skip=0,
                current_user=user,
                db=MagicMock(),
            )

        kwargs = crud_layer.get_multi_by_account.call_args.kwargs
        assert kwargs["runtime_session_id"] == session_id
        assert kwargs["account_id"] == user.account_id
        assert kwargs["status"] == "pending"


def _list_client(user):
    app = FastAPI()
    app.include_router(approval_requests.router, prefix="/api/v1")
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    return TestClient(app, raise_server_exceptions=False)


class TestListEndpointValidatesTheSessionId:
    @pytest.fixture
    def user(self):
        user = MagicMock()
        user.id = uuid.uuid4()
        user.account_id = str(uuid.uuid4())
        user.username = "testuser@example.com"
        user.is_active = True
        return user

    def test_malformed_session_id_is_rejected(self, user):
        """A typo must not read as "this session has nothing pending"."""
        with patch(
            "preloop.api.endpoints.approval_requests.crud_approval_request"
        ) as crud_layer:
            crud_layer.get_multi_by_account.return_value = []
            with _list_client(user) as client:
                response = client.get(
                    "/api/v1/approval-requests?runtime_session_id=not-a-uuid"
                )

        assert response.status_code == 422
        crud_layer.get_multi_by_account.assert_not_called()

    def test_well_formed_session_id_is_accepted(self, user):
        session_id = uuid.uuid4()
        with patch(
            "preloop.api.endpoints.approval_requests.crud_approval_request"
        ) as crud_layer:
            crud_layer.get_multi_by_account.return_value = []
            with _list_client(user) as client:
                response = client.get(
                    f"/api/v1/approval-requests?runtime_session_id={session_id}"
                )

        assert response.status_code == 200
        assert (
            crud_layer.get_multi_by_account.call_args.kwargs["runtime_session_id"]
            == session_id
        )
