"""The Overview's API key count reads a count, not the key list (#1694)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from preloop.models import models
from preloop.models.crud import crud_api_key


def _key(
    db_session: Any,
    user: Any,
    *,
    is_active: bool = True,
    expires_at: Optional[datetime] = None,
) -> None:
    token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    db_session.add(
        models.ApiKey(
            name=f"key-{token[:6]}",
            key=token,
            key_hash=crud_api_key.build_key_hash(token),
            key_prefix=crud_api_key.build_key_prefix(token),
            scopes=[],
            account_id=user.account_id,
            user_id=user.id,
            is_active=is_active,
            expires_at=expires_at,
        )
    )


def test_count_reports_only_usable_keys(client, db_session, test_user) -> None:
    before = client.get("/api/v1/auth/api-keys/count")
    assert before.status_code == 200, before.text
    baseline = before.json()["active"]

    _key(db_session, test_user)
    _key(db_session, test_user, expires_at=datetime.now(UTC) + timedelta(days=3))
    _key(db_session, test_user, is_active=False)
    _key(db_session, test_user, expires_at=datetime.now(UTC) - timedelta(days=1))
    db_session.commit()

    response = client.get("/api/v1/auth/api-keys/count")
    assert response.status_code == 200, response.text
    assert response.json() == {"active": baseline + 2}


def test_count_does_not_depend_on_the_session_timezone(
    client, db_session, test_user
) -> None:
    """A key that expired an hour ago is not usable on a non-UTC session."""
    from sqlalchemy import text

    baseline = client.get("/api/v1/auth/api-keys/count").json()["active"]
    _key(
        db_session,
        test_user,
        expires_at=(datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None),
    )
    db_session.commit()
    db_session.execute(text("SET TIME ZONE 'America/New_York'"))

    count = crud_api_key.count_usable_by_user(db_session, username=test_user.username)
    assert count == baseline
