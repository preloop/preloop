"""Query budget of the gateway hot path and of password sign in.

The account hierarchy hooks (``preloop.plugins.account_hooks``) sit on both
paths. With no hook registered they must not cost a query, so these tests
pin the number of statements each path issues. The numbers were measured on
the code before the hooks existed; a change here means a path got more
expensive and has to be justified, not just re-pinned.

This module deliberately does not import the hooks module, so it runs
unchanged against code without it.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_password_hash
from preloop.api.endpoints.openai_gateway import get_model_gateway_auth_context
from preloop.models.crud import crud_account, crud_ai_model, crud_user
from preloop.services.model_gateway_auth import ModelGatewayAuthContext

PASSWORD = "query-budget-pass-1"
#: Statements issued by one password sign in (``/token/json``).
LOGIN_STATEMENTS = 6
#: Statements issued by one warm, priced chat completion without policies.
GATEWAY_STATEMENTS = 23


@contextmanager
def _count_statements(db: Session) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        text = statement.lstrip().upper()
        if text.startswith("SAVEPOINT") or text.startswith("RELEASE SAVEPOINT"):
            return
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.fixture
def anon_client(db_session: Session) -> Iterator[TestClient]:
    from preloop.api.app import create_app
    from preloop.models.db.session import get_db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        yield client


def test_password_sign_in_query_count(
    db_session: Session, anon_client: TestClient
) -> None:
    account = crud_account.create(
        db_session, obj_in={"organization_name": f"Org {uuid.uuid4().hex[:6]}"}
    )
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "username": f"budget{uuid.uuid4().hex[:8]}",
            "email": f"budget{uuid.uuid4().hex[:8]}@example.com",
            "full_name": "Budget",
            "is_active": True,
            "email_verified": True,
            "hashed_password": get_password_hash(PASSWORD),
            "user_source": "local",
        },
    )
    db_session.flush()

    with _count_statements(db_session) as statements:
        response = anon_client.post(
            "/api/v1/auth/token/json",
            json={"username": user.username, "password": PASSWORD},
        )

    assert response.status_code == 200, response.text
    assert len(statements) == LOGIN_STATEMENTS, statements


def test_gateway_chat_completion_query_count(
    app, client, db_session: Session, test_user
) -> None:
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
                    "provider_adapter": "preloop",
                    "responses_api": "transcode",
                },
                "pricing": {"input_price_per_1k": 1, "output_price_per_1k": 1},
            },
        },
        account_id=test_user.account_id,
    )
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=test_user)
    )
    body = {"model": "openai/gpt-5", "messages": [{"role": "user", "content": "x"}]}
    completion = {
        "id": "chatcmpl_budget",
        "created": 1710000000,
        "choices": [
            {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }
    with patch(
        "preloop.services.openai_gateway.litellm.completion", return_value=completion
    ):
        # Warm process caches (kill switch, pricing) so the count is the
        # steady state a busy gateway sees.
        warm = client.post("/openai/v1/chat/completions", json=body)
        assert warm.status_code == 200, warm.text
        with _count_statements(db_session) as statements:
            response = client.post("/openai/v1/chat/completions", json=body)

    assert response.status_code == 200, response.text
    assert len(statements) == GATEWAY_STATEMENTS, statements
