"""Tests for the batched AI models overview endpoint.

The Models page used to ask the API for a usage summary and a session list
per model. With enough models that burst emptied the connection pool, which
is what took the API down on 2026-09-03. These tests hold the replacement to
its promise: the same numbers, in a fixed number of queries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import List
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.models.models.ai_model import AIModel
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.model_price_override import ModelPriceOverride
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.models.user import User

WINDOW_START = datetime.now(UTC) - timedelta(days=7)


class _SelectCounter:
    """Count SELECT statements issued while it is attached."""

    def __init__(self) -> None:
        self.count = 0

    def __enter__(self) -> "_SelectCounter":
        event.listen(Engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc_info: object) -> None:
        event.remove(Engine, "before_cursor_execute", self._on_execute)

    def _on_execute(
        self,
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            self.count += 1


def _make_model(db_session: Session, user: User, name: str) -> AIModel:
    """Create one account-owned model configuration."""
    model = AIModel(
        name=name,
        provider_name="openai",
        model_identifier="gpt-4o",
        account_id=user.account_id,
        meta_data={"gateway": {"model_alias": name}},
    )
    db_session.add(model)
    db_session.flush()
    return model


def _make_session(
    db_session: Session, user: User, *, reference: str, ended: bool
) -> RuntimeSession:
    """Create one runtime session, open or already ended."""
    now = datetime.now(UTC)
    runtime_session = RuntimeSession(
        id=uuid4(),
        account_id=user.account_id,
        session_source_type="openclaw",
        session_source_id=reference,
        session_reference=reference,
        runtime_principal_type="openclaw",
        runtime_principal_id=reference,
        started_at=now - timedelta(hours=1),
        last_activity_at=now,
        ended_at=now if ended else None,
    )
    db_session.add(runtime_session)
    db_session.flush()
    return runtime_session


def _make_usage(
    db_session: Session,
    user: User,
    *,
    model: AIModel,
    alias: str,
    status_code: int = 200,
    minutes_ago: int = 10,
    runtime_session: RuntimeSession | None = None,
    total_tokens: int = 30,
    estimated_cost: float = 0.5,
) -> ApiUsage:
    """Record one gateway request against a model."""
    usage = ApiUsage(
        id=uuid4(),
        user_id=user.id,
        account_id=user.account_id,
        endpoint="/api/v1/gateway/chat/completions",
        method="POST",
        status_code=status_code,
        duration=0.2,
        action_type="model_gateway",
        ai_model_id=model.id,
        model_alias=alias,
        provider_name="openai",
        prompt_tokens=total_tokens // 3,
        completion_tokens=total_tokens - total_tokens // 3,
        total_tokens=total_tokens,
        estimated_cost=estimated_cost,
        runtime_session_id=runtime_session.id if runtime_session else None,
        timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    db_session.add(usage)
    db_session.flush()
    return usage


def _overview(client, models_expected: int) -> List[dict]:
    """Call the endpoint and return its rows, newest window."""
    response = client.get(
        "/api/v1/ai-models/overview",
        params={"start_date": WINDOW_START.isoformat()},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["models"]) == models_expected
    return payload["models"]


def test_overview_reports_usage_sessions_and_pricing(
    client, db_session: Session, test_user: User
) -> None:
    """One call answers what the page used to ask per model."""
    busy = _make_model(db_session, test_user, "busy-model")
    quiet = _make_model(db_session, test_user, "quiet-model")

    open_session = _make_session(
        db_session, test_user, reference="open-session", ended=False
    )
    closed_session = _make_session(
        db_session, test_user, reference="closed-session", ended=True
    )

    _make_usage(
        db_session,
        test_user,
        model=busy,
        alias="busy-model",
        runtime_session=open_session,
        minutes_ago=30,
    )
    _make_usage(
        db_session,
        test_user,
        model=busy,
        alias="busy-model-renamed",
        runtime_session=open_session,
        minutes_ago=5,
    )
    _make_usage(
        db_session,
        test_user,
        model=busy,
        alias="busy-model",
        status_code=500,
        runtime_session=closed_session,
        minutes_ago=20,
    )
    db_session.commit()

    rows = {row["ai_model_id"]: row for row in _overview(client, 2)}

    busy_row = rows[str(busy.id)]
    assert busy_row["total_requests"] == 3
    assert busy_row["failed_requests"] == 1
    assert busy_row["successful_requests"] == 2
    # Aliases of the same model are summed into the one row the page shows.
    assert busy_row["token_usage"]["total_tokens"] == 90
    assert busy_row["estimated_cost"] == pytest.approx(1.5)
    # Only the session that is still open counts as active.
    assert busy_row["active_session_count"] == 1
    assert busy_row["last_request_at"] is not None
    assert busy_row["pricing_source"] in {"catalog", "model_config", "none"}
    assert busy_row["model_alias"] == "busy-model"

    quiet_row = rows[str(quiet.id)]
    assert quiet_row["total_requests"] == 0
    assert quiet_row["active_session_count"] == 0
    assert quiet_row["last_request_at"] is None


def test_overview_reports_an_active_price_override(
    client, db_session: Session, test_user: User
) -> None:
    """A model priced by an override says so, without a per-model call."""
    model = _make_model(db_session, test_user, "priced-model")
    db_session.add(
        ModelPriceOverride(
            id=uuid4(),
            account_id=test_user.account_id,
            ai_model_id=model.id,
            model_alias="priced-model",
            provider_name="openai",
            input_price_per_1k=0.002,
            output_price_per_1k=0.004,
            currency="USD",
            is_active=True,
        )
    )
    db_session.commit()

    rows = _overview(client, 1)

    assert rows[0]["pricing_source"] == "override"


def test_overview_query_count_does_not_grow_with_model_count(
    client, db_session: Session, test_user: User
) -> None:
    """The point of the endpoint: N models must not mean N queries."""
    for index in range(2):
        _make_model(db_session, test_user, f"small-fleet-{index}")
    db_session.commit()

    with _SelectCounter() as small:
        _overview(client, 2)

    for index in range(8):
        _make_model(db_session, test_user, f"large-fleet-{index}")
    db_session.commit()

    with _SelectCounter() as large:
        _overview(client, 10)

    assert large.count == small.count
    # Ceiling as well as invariance: the page is worth a handful of queries
    # (models, usage aggregate, active sessions, price overrides), not a
    # handful per model.
    assert small.count <= 8


def test_overview_excludes_other_accounts(
    client, db_session: Session, test_user: User
) -> None:
    """Another account's models never appear on this account's page."""
    from preloop.models.crud import crud_account

    _make_model(db_session, test_user, "own-model")
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    db_session.add(
        AIModel(
            name="other-account-model",
            provider_name="openai",
            model_identifier="gpt-4o",
            account_id=other_account.id,
        )
    )
    db_session.commit()

    rows = _overview(client, 1)

    assert rows[0]["model_name"] == "own-model"
