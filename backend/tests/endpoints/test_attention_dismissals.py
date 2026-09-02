"""Endpoint tests for the console attention dismissals.

Covers GET/PUT/DELETE ``/api/v1/attention/dismissals``: creating a dismissal,
re-dismissing an item whose fingerprint changed (upsert, not a second row),
snooze expiry, account isolation, restore, and the validation the console
relies on.
"""

from datetime import UTC, datetime, timedelta

from preloop.models.crud import crud_account, crud_attention_dismissal, crud_user

DISMISSALS = "/api/v1/attention/dismissals"


def _make_account_with_user(db, email):
    """A second account with its own user, for isolation tests."""
    account = crud_account.create(
        db, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "email": email,
            "username": email.split("@")[0],
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db.flush()
    return account, user


def test_dismissals_start_empty(client):
    """A fresh account has silenced nothing."""
    response = client.get(DISMISSALS)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_dismissal_is_created_and_listed(client, db_session, test_user):
    """A dismissal comes back with its reason, fingerprint and author."""
    response = client.put(
        f"{DISMISSALS}/agent:agent-1",
        json={"fingerprint": "mcp_proxy_only|passed|none", "reason": "expected"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["item_id"] == "agent:agent-1"
    assert body["reason"] == "expected"
    assert body["fingerprint"] == "mcp_proxy_only|passed|none"
    assert body["snooze_until"] is None
    assert body["dismissed_by_user_id"] == str(test_user.id)
    assert body["dismissed_by_username"] == "Test User"

    listed = client.get(DISMISSALS).json()
    assert listed["total"] == 1
    assert listed["items"][0]["item_id"] == "agent:agent-1"


def test_re_dismissing_replaces_the_row(client, db_session, test_user):
    """A changed fingerprint updates the dismissal instead of duplicating it."""
    client.put(
        f"{DISMISSALS}/flow:flow-1",
        json={"fingerprint": "run-1", "reason": "expected"},
    )
    response = client.put(
        f"{DISMISSALS}/flow:flow-1",
        json={"fingerprint": "run-2", "reason": "fixed"},
    )

    assert response.status_code == 200
    listed = client.get(DISMISSALS).json()
    assert listed["total"] == 1
    assert listed["items"][0]["fingerprint"] == "run-2"
    assert listed["items"][0]["reason"] == "fixed"


def test_snooze_sets_an_expiry(client):
    """A snooze is hidden now and carries the date it comes back."""
    before = datetime.now(UTC)
    response = client.put(
        f"{DISMISSALS}/pricing:catalog",
        json={
            "fingerprint": "catalog:none",
            "reason": "snoozed",
            "snooze_days": 7,
        },
    )

    assert response.status_code == 200
    snooze_until = datetime.fromisoformat(response.json()["snooze_until"])
    assert before + timedelta(days=6) < snooze_until < before + timedelta(days=8)
    assert client.get(DISMISSALS).json()["total"] == 1


def test_snooze_requires_days(client):
    """ "Snoozed" without a duration is a 422, not a permanent mute."""
    response = client.put(
        f"{DISMISSALS}/pricing:catalog",
        json={"fingerprint": "catalog:none", "reason": "snoozed"},
    )

    assert response.status_code == 422


def test_unknown_reason_is_rejected(client):
    """The reason vocabulary is closed."""
    response = client.put(
        f"{DISMISSALS}/pricing:catalog",
        json={"fingerprint": "catalog:none", "reason": "forever"},
    )

    assert response.status_code == 422


def test_expired_snooze_is_excluded_and_collected(client, db_session, test_user):
    """A snooze that ran out neither hides the item nor lingers in the table."""
    crud_attention_dismissal.upsert(
        db_session,
        account_id=test_user.account_id,
        item_id="model:openai/gpt-5",
        fingerprint="2026-08-01T00:00:00Z",
        reason="snoozed",
        snooze_until=datetime.now(UTC) - timedelta(days=1),
        dismissed_by_user_id=test_user.id,
    )

    body = client.get(DISMISSALS).json()

    assert body["total"] == 0
    assert (
        crud_attention_dismissal.get_by_item(
            db_session,
            account_id=test_user.account_id,
            item_id="model:openai/gpt-5",
        )
        is None
    )


def test_dismissals_are_scoped_to_the_account(client, db_session, test_user):
    """One account's silence never quiets another account's console."""
    other_account, _ = _make_account_with_user(db_session, "other@example.com")
    crud_attention_dismissal.upsert(
        db_session,
        account_id=other_account.id,
        item_id="agent:agent-elsewhere",
        fingerprint="incomplete|not_run|none",
        reason="expected",
    )
    client.put(
        f"{DISMISSALS}/agent:agent-mine",
        json={"fingerprint": "incomplete|not_run|none", "reason": "expected"},
    )

    body = client.get(DISMISSALS).json()

    assert [item["item_id"] for item in body["items"]] == ["agent:agent-mine"]
    assert (
        crud_attention_dismissal.get_by_item(
            db_session,
            account_id=other_account.id,
            item_id="agent:agent-elsewhere",
        )
        is not None
    )


def test_delete_restores_the_item(client):
    """Restore removes the row; restoring twice is a 404, not a silent no-op."""
    client.put(
        f"{DISMISSALS}/budget:policy-1",
        json={"fingerprint": "policy-1|2026-09-01", "reason": "expected"},
    )

    response = client.delete(f"{DISMISSALS}/budget:policy-1")

    assert response.status_code == 204
    assert client.get(DISMISSALS).json()["total"] == 0
    assert client.delete(f"{DISMISSALS}/budget:policy-1").status_code == 404


def test_delete_cannot_reach_another_account(client, db_session):
    """Restoring is account-scoped too."""
    other_account, _ = _make_account_with_user(db_session, "other2@example.com")
    crud_attention_dismissal.upsert(
        db_session,
        account_id=other_account.id,
        item_id="agent:agent-elsewhere",
        fingerprint="incomplete|not_run|none",
        reason="expected",
    )

    response = client.delete(f"{DISMISSALS}/agent:agent-elsewhere")

    assert response.status_code == 404
    assert (
        crud_attention_dismissal.get_by_item(
            db_session,
            account_id=other_account.id,
            item_id="agent:agent-elsewhere",
        )
        is not None
    )
