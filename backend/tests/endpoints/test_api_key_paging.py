"""Settings > API keys reads one page of 25, not every key (#1694)."""

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
    name: Optional[str] = None,
    is_active: bool = True,
    expires_at: Optional[datetime] = None,
    created_at: Optional[datetime] = None,
) -> None:
    token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    key = models.ApiKey(
        name=name or f"key-{token[:6]}",
        key=token,
        key_hash=crud_api_key.build_key_hash(token),
        key_prefix=crud_api_key.build_key_prefix(token),
        scopes=[],
        account_id=user.account_id,
        user_id=user.id,
        is_active=is_active,
        expires_at=expires_at,
    )
    if created_at is not None:
        key.created_at = created_at
    db_session.add(key)


def _baseline(client: Any) -> tuple[int, int]:
    response = client.get("/api/v1/auth/api-keys?limit=1")
    assert response.status_code == 200, response.text
    return (
        int(response.headers["X-Total-Count"]),
        int(response.headers["X-Retired-Count"]),
    )


def test_limit_and_offset_page_the_list_with_a_total(
    client, db_session, test_user
) -> None:
    total0, _ = _baseline(client)
    now = datetime.now(UTC).replace(tzinfo=None)
    for index in range(30):
        _key(
            db_session,
            test_user,
            name=f"paged-{index:02d}",
            created_at=now + timedelta(minutes=index),
        )
    db_session.commit()

    first = client.get("/api/v1/auth/api-keys?limit=25&offset=0")
    assert first.status_code == 200, first.text
    assert len(first.json()) == 25
    assert int(first.headers["X-Total-Count"]) == total0 + 30
    # Newest first by default.
    assert first.json()[0]["name"] == "paged-29"

    second = client.get("/api/v1/auth/api-keys?limit=25&offset=25")
    names = {row["name"] for row in second.json()}
    assert not names & {row["name"] for row in first.json()}


def test_unpaged_call_still_returns_every_key(client, db_session, test_user) -> None:
    total0, _ = _baseline(client)
    _key(db_session, test_user)
    db_session.commit()
    response = client.get("/api/v1/auth/api-keys")
    assert response.status_code == 200
    assert len(response.json()) == total0 + 1


def test_retired_keys_are_counted_and_can_be_excluded(
    client, db_session, test_user
) -> None:
    total0, retired0 = _baseline(client)
    _key(db_session, test_user, name="live-one")
    _key(db_session, test_user, name="revoked-one", is_active=False)
    _key(
        db_session,
        test_user,
        name="expired-one",
        expires_at=(datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None),
    )
    db_session.commit()

    response = client.get("/api/v1/auth/api-keys?limit=100&include_retired=false")
    assert response.status_code == 200, response.text
    names = {row["name"] for row in response.json()}
    assert "live-one" in names
    assert not names & {"revoked-one", "expired-one"}
    assert int(response.headers["X-Total-Count"]) == total0 - retired0 + 1
    assert int(response.headers["X-Retired-Count"]) == retired0 + 2


def test_search_and_sort_run_on_the_server(client, db_session, test_user) -> None:
    _key(db_session, test_user, name="Zebra-search-x")
    _key(db_session, test_user, name="alpha-search-x")
    db_session.commit()

    response = client.get("/api/v1/auth/api-keys?limit=25&q=SEARCH-X&sort=name")
    assert response.status_code == 200, response.text
    assert [row["name"] for row in response.json()] == [
        "alpha-search-x",
        "Zebra-search-x",
    ]
    assert response.headers["X-Total-Count"] == "2"

    desc = client.get("/api/v1/auth/api-keys?limit=25&q=search-x&sort=-name")
    assert [row["name"] for row in desc.json()][0] == "Zebra-search-x"


def test_unknown_sort_is_rejected(client) -> None:
    response = client.get("/api/v1/auth/api-keys?limit=25&sort=key_hash")
    assert response.status_code == 422
