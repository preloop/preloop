"""Endpoint tests for ``/api/v1/attention/spend-outliers`` (#960)."""

from datetime import UTC, date, datetime, timedelta

from preloop.models import models
from preloop.services.spend_outliers import evaluate_daily_rules

BASE = "/api/v1/attention/spend-outliers"


def _spend(db, user, day: date, cost: float) -> None:
    db.add(
        models.ApiUsage(
            user_id=user.id,
            account_id=user.account_id,
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="model_gateway",
            model_alias="standard-model",
            estimated_cost=cost,
            timestamp=datetime(day.year, day.month, day.day, 12, 0),
        )
    )
    db.flush()


def test_list_is_empty_without_findings(client):
    """No findings, no cards."""
    response = client.get(BASE)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_list_returns_open_finding_with_numbers(client, db_session, test_user):
    """The card carries who, which rule, the numbers and the day."""
    now = datetime.now(UTC)
    yesterday = now.date() - timedelta(days=1)
    for offset in range(1, 11):
        _spend(db_session, test_user, yesterday - timedelta(days=offset), 10.0)
    _spend(db_session, test_user, yesterday, 40.0)
    evaluate_daily_rules(db_session, test_user.account_id, now)

    body = client.get(BASE).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["rule"] == "daily_spend"
    assert item["rule_label"] == "Daily spend spike"
    assert item["user_id"] == str(test_user.id)
    assert item["user_name"] == "Test User"
    assert item["day"] == yesterday.isoformat()
    assert item["item_id"] == f"spend:daily_spend:{test_user.id}"
    assert item["details"]["median_usd"] == 10.0
    assert item["details"]["multiple"] == 4.0


def test_settings_default_until_configured(client):
    """A fresh account reads the documented defaults, session rule off."""
    body = client.get(f"{BASE}/settings").json()

    assert body == {
        "daily_multiple": 3.0,
        "min_history_days": 7,
        "top_tier_model_prefixes": [],
        "top_tier_share": 0.5,
        "session_cost_threshold_usd": None,
        "configured": False,
    }


def test_settings_round_trip_and_clean_prefixes(client):
    """Prefixes are trimmed, lowercased and deduplicated."""
    response = client.put(
        f"{BASE}/settings",
        json={
            "daily_multiple": 4,
            "min_history_days": 10,
            "top_tier_model_prefixes": [" Premium-", "premium-", "", "vendor/big"],
            "top_tier_share": 0.6,
            "session_cost_threshold_usd": 25,
        },
    )

    assert response.status_code == 200
    body = client.get(f"{BASE}/settings").json()
    assert body["configured"] is True
    assert body["daily_multiple"] == 4.0
    assert body["min_history_days"] == 10
    assert body["top_tier_model_prefixes"] == ["premium-", "vendor/big"]
    assert body["top_tier_share"] == 0.6
    assert body["session_cost_threshold_usd"] == 25.0

    # Clearing the threshold turns the session rule off again.
    cleared = client.put(
        f"{BASE}/settings", json={"session_cost_threshold_usd": None}
    ).json()
    assert cleared["session_cost_threshold_usd"] is None


def test_settings_reject_out_of_range_values(client):
    """Nonsense thresholds are a 422, not a rule that never or always fires."""
    for payload in (
        {"daily_multiple": 0.5},
        {"min_history_days": 0},
        {"min_history_days": 29},
        {"top_tier_share": 1.0},
        {"top_tier_share": 0},
        {"session_cost_threshold_usd": 0},
        {"session_cost_threshold_usd": -1},
    ):
        response = client.put(f"{BASE}/settings", json=payload)
        assert response.status_code == 422, payload
