"""Session-artifact usage for the account that owns the recordings.

Recording upload itself is #887. This issue gates ``store`` and exposes
usage. ``ValueError("storage_budget_exhausted")`` is what that upload maps
to HTTP 507; the endpoint is not in this tree yet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_runtime_session_artifact as crud
from preloop.services.session_artifact_budget import account_usage

_KIB = 1024


def test_usage_endpoint_matches_rows(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """used, per-kind, and 30-day evicted counts follow the stored rows."""
    budget = 2 * _KIB * _KIB
    monkeypatch.setattr(settings, "runtime_session_artifact_account_max_bytes", budget)
    now = datetime.now(UTC)
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="browser",
        session_source_id=f"session-{uuid4().hex[:12]}",
        started_at=now,
    )
    db_session.add(session)
    db_session.flush()
    screenshot = crud.store(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        kind="screenshot",
        source="browser_use",
        source_ref="step-1",
        content_type="image/png",
        plaintext=b"a" * 100,
        manifest={},
    )
    recording = crud.store(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        kind="recording",
        source="browser_use",
        source_ref="clip-1",
        content_type="video/webm",
        plaintext=b"b" * 250,
        manifest={},
    )
    assert crud.mark_unavailable(
        db_session,
        account_id=test_user.account_id,
        artifact_id=recording.id,
        availability="evicted",
    )

    response = client.get("/api/v1/account/session-artifacts/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["used_bytes"] == screenshot.size_bytes
    assert body["budget_bytes"] == budget
    assert body["by_kind"] == {"screenshot": 100, "recording": 0}
    assert body["evicted_count_30d"] == 1
    assert body == account_usage(db_session, account_id=test_user.account_id)


def test_exhausted_budget_is_the_507_error(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing unheld left to evict raises the error recordings map to 507."""
    monkeypatch.setattr(settings, "runtime_session_artifact_account_max_bytes", 10)
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="browser",
        session_source_id=f"session-{uuid4().hex[:12]}",
        started_at=datetime.now(UTC),
        legal_hold=True,
    )
    db_session.add(session)
    db_session.commit()
    with pytest.raises(ValueError, match="storage_budget_exhausted"):
        crud.store(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            kind="recording",
            source="browser_use",
            source_ref="clip-held",
            content_type="video/webm",
            plaintext=b"0123456789abcdef",
            manifest={},
        )
