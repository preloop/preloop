"""Tests for the Anthropic Claude Code Analytics import (#1413).

The Admin API is replaced by an ``httpx.MockTransport`` that serves recorded
payloads shaped like the public API reference; no live Anthropic call is made.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_anthropic_import_connection,
    crud_anthropic_usage,
    crud_anthropic_user_mapping,
    crud_api_key,
    crud_gateway_subject,
    crud_user,
)
from preloop.models.crud.anthropic_import import ANTHROPIC_CC_PROVIDER
from preloop.services import anthropic_usage_import as svc
from preloop.services.secret_service import get_secret_service

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "anthropic_usage"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
DAY = date(2026, 10, 8)
ADMIN_KEY = "sk-ant-admin01-very-secret-admin-key-7Kq9"
UPSTREAM_KEY = "sk-ant-api03-R2D-upstream-body-igAA"
WINDOW = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))


def _load(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@dataclass
class FakeAnthropic:
    """Programmable Admin API stand-in that records every request."""

    rate_limited_first: int = 0
    report_status: int = 200
    api_keys_status: int = 200
    requests: List[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "api.anthropic.com"
        assert request.headers["x-api-key"] == ADMIN_KEY
        assert request.headers["anthropic-version"] == svc.ANTHROPIC_VERSION
        path = request.url.path
        if path == svc.API_KEYS_PATH:
            if self.api_keys_status != 200:
                return httpx.Response(self.api_keys_status, json={})
            return httpx.Response(200, json=_load("api_keys.json"))
        assert path == svc.CLAUDE_CODE_REPORT_PATH, path
        if self.rate_limited_first > 0:
            self.rate_limited_first -= 1
            return httpx.Response(429, headers={"retry-after": "2"}, json={})
        if self.report_status != 200:
            return httpx.Response(self.report_status, json={"error": {}})
        if request.url.params.get("page"):
            assert request.url.params["page"] == "page_MjAyNi0xMC0wOFQwMDowMDowMFo="
            return httpx.Response(200, json=_load("claude_code_page2.json"))
        return httpx.Response(200, json=_load("claude_code_page1.json"))

    def factory(self):
        return lambda: httpx.Client(transport=httpx.MockTransport(self.handler))

    def report_days(self) -> List[str]:
        return [
            r.url.params["starting_at"]
            for r in self.requests
            if r.url.path == svc.CLAUDE_CODE_REPORT_PATH
            and not r.url.params.get("page")
        ]


def _connect(
    db: Session,
    account_id: Any,
    *,
    last_synced_day: Optional[date] = DAY - __import__("datetime").timedelta(days=1),
    gateway_key_names: Optional[List[str]] = None,
) -> models.AnthropicImportConnection:
    secret = get_secret_service().create_local_secret_reference(
        db,
        account_id=account_id,
        name="admin",
        secret_kind=svc.ANTHROPIC_IMPORT_SECRET_KIND,
        secret_value=ADMIN_KEY,
    )
    return crud_anthropic_import_connection.create(
        db,
        obj_in={
            "account_id": account_id,
            "secret_reference_id": secret.id,
            "key_hint": ADMIN_KEY[-4:],
            "gateway_key_names": gateway_key_names,
            "last_synced_day": last_synced_day,
            "is_active": True,
        },
    )


def _upstream_model(db: Session, account_id: Any) -> models.AIModel:
    model = models.AIModel(
        name="Anthropic upstream",
        provider_name="anthropic",
        model_identifier="claude-upstream",
        api_key=UPSTREAM_KEY,
        account_id=account_id,
    )
    db.add(model)
    db.flush()
    return model


def _rows(db: Session, account_id: Any) -> List[models.ProviderBillingSnapshot]:
    return crud_anthropic_usage.list_rows(
        db,
        account_id=str(account_id),
        provider=ANTHROPIC_CC_PROVIDER,
        start=WINDOW[0],
        end=WINDOW[1],
    )


def _sync(db, connection, fake, **kwargs):
    return svc.sync_connection(
        db,
        connection,
        now=NOW,
        http_client_factory=fake.factory(),
        sleep=kwargs.pop("sleep", lambda _s: None),
        **kwargs,
    )


def test_pages_both_actor_types_and_models_into_rows(db_session, test_user):
    connection = _connect(db_session, test_user.account_id)
    fake = FakeAnthropic()

    result = _sync(db_session, connection, fake)

    assert result["error"] is None
    assert result["days"] == [DAY.isoformat()]
    rows = _rows(db_session, test_user.account_id)
    by_key = {(r.user_login, r.model): r for r in rows}
    assert set(by_key) == {
        ("dev@corp.example", "claude-opus-model"),
        ("dev@corp.example", "claude-haiku-model"),
        ("key:preloop-gateway", "claude-opus-model"),
        ("key:ci-bot", "claude-haiku-model"),
    }
    opus = by_key[("dev@corp.example", "claude-opus-model")]
    assert opus.usage_source == "imported"
    assert opus.line_item == "claude_code_analytics"
    assert opus.granularity == "1d"
    assert opus.cost_basis == "estimated"
    assert opus.cost_amount == pytest.approx(1.13)
    assert opus.uncached_input_tokens == 100000
    assert opus.output_tokens == 35000
    assert opus.cached_input_tokens == 10000
    assert opus.cache_creation_tokens == 5000
    assert opus.raw["source"] == "anthropic_cc_analytics"
    assert opus.raw["report_date"] == DAY.isoformat()
    assert opus.raw["core_metrics"]["num_sessions"] == 5
    assert opus.raw["tool_actions"]["edit_tool"] == {"accepted": 45, "rejected": 5}
    assert opus.raw["terminal_types"] == ["vscode"]
    assert opus.raw["customer_type"] == "subscription"
    assert opus.raw["subscription_type"] == "team"
    assert "prompt must never" not in json.dumps(opus.raw)
    assert set(opus.raw) == {
        "source",
        "report_date",
        "actor_type",
        "core_metrics",
        "tool_actions",
        "terminal_types",
        "customer_type",
        "subscription_type",
        "metered_by_gateway",
        "gateway_match",
    }
    assert db_session.query(models.ApiUsage).count() == 0


def test_resync_of_the_same_day_is_idempotent(db_session, test_user):
    connection = _connect(db_session, test_user.account_id, last_synced_day=DAY)
    fake = FakeAnthropic()
    _sync(db_session, connection, fake)
    first = len(_rows(db_session, test_user.account_id))
    _sync(db_session, connection, fake)
    _sync(db_session, connection, fake)

    assert len(_rows(db_session, test_user.account_id)) == first == 4
    assert fake.report_days() == [DAY.isoformat()] * 3


def test_429_then_success_honours_retry_after(db_session, test_user):
    connection = _connect(db_session, test_user.account_id)
    fake = FakeAnthropic(rate_limited_first=2)
    waits: List[float] = []

    result = _sync(db_session, connection, fake, sleep=waits.append)

    assert result["error"] is None
    assert waits == [2.0, 2.0]
    assert len(_rows(db_session, test_user.account_id)) == 4


def test_rate_limit_retries_are_bounded(db_session, test_user):
    connection = _connect(db_session, test_user.account_id)
    fake = FakeAnthropic(rate_limited_first=100)

    result = _sync(db_session, connection, fake)

    assert "rate limited" in result["error"]
    assert len(fake.report_days()) == svc.RATE_LIMIT_RETRIES + 1


def test_401_marks_the_connection_without_raising(db_session, test_user, caplog):
    connection = _connect(db_session, test_user.account_id)
    fake = FakeAnthropic(report_status=401)

    with caplog.at_level(logging.DEBUG):
        result = _sync(db_session, connection, fake)

    assert "401" in result["error"]
    db_session.refresh(connection)
    assert "401" in connection.last_error
    assert connection.last_synced_day == DAY - __import__("datetime").timedelta(days=1)
    assert _rows(db_session, test_user.account_id) == []
    assert ADMIN_KEY not in caplog.text


def test_catch_up_is_capped_at_seven_days(db_session, test_user):
    connection = _connect(
        db_session, test_user.account_id, last_synced_day=date(2026, 9, 1)
    )
    fake = FakeAnthropic()

    result = _sync(db_session, connection, fake)

    assert len(result["days"]) == 7
    assert result["days"][-1] == DAY.isoformat()


def test_no_connection_is_a_noop(db_session, test_user):
    fake = FakeAnthropic()
    assert (
        svc.ingest_anthropic_usage(
            db_session,
            account_id=str(test_user.account_id),
            now=NOW,
            http_client_factory=fake.factory(),
        )
        == {}
    )
    assert fake.requests == []


# --- overlap rule: never double count gateway-metered usage -----------------


def test_api_actor_on_an_upstream_key_is_flagged_and_excluded(db_session, test_user):
    _upstream_model(db_session, test_user.account_id)
    connection = _connect(db_session, test_user.account_id)
    _sync(db_session, connection, FakeAnthropic())

    rows = {r.user_login: r for r in _rows(db_session, test_user.account_id)}
    assert rows["key:preloop-gateway"].raw["metered_by_gateway"] is True
    assert rows["key:ci-bot"].raw["metered_by_gateway"] is False
    assert rows["dev@corp.example"].raw["metered_by_gateway"] is False

    summary = svc.build_anthropic_summary(
        db_session,
        account_id=str(test_user.account_id),
        start=WINDOW[0],
        end=WINDOW[1],
    )
    # 1.13 + 0.07 (user) + 0.03 (ci-bot); the 5.00 on the gateway key is out.
    assert summary["total_estimated_cost"] == pytest.approx(1.23)
    assert summary["excluded_metered_by_gateway"]["estimated_cost"] == pytest.approx(
        5.0
    )
    assert summary["excluded_metered_by_gateway"]["actors"] == ["key:preloop-gateway"]
    assert "key:preloop-gateway" not in {a["actor"] for a in summary["by_actor"]}
    assert summary["metered_by_gateway"] is False
    assert summary["marker"] == svc.NOT_METERED_MARKER


def test_listed_gateway_key_name_is_excluded_when_key_listing_fails(
    db_session, test_user
):
    connection = _connect(
        db_session, test_user.account_id, gateway_key_names=["ci-bot"]
    )
    result = _sync(db_session, connection, FakeAnthropic(api_keys_status=403))

    assert result["error"] is None
    assert "API keys" in result["warning"]
    rows = {r.user_login: r for r in _rows(db_session, test_user.account_id)}
    assert rows["key:ci-bot"].raw["metered_by_gateway"] is True
    assert rows["key:preloop-gateway"].raw["metered_by_gateway"] is False


def test_hint_matching_needs_prefix_and_suffix():
    assert svc.hint_matches("sk-ant-api03-R2D...igAA", UPSTREAM_KEY)
    assert not svc.hint_matches("sk-ant-api03-R2D...xxxx", UPSTREAM_KEY)
    assert not svc.hint_matches("...igAA", UPSTREAM_KEY)
    assert not svc.hint_matches(None, UPSTREAM_KEY)


# --- identity mapping -------------------------------------------------------


def _summary(db, account_id):
    return svc.build_anthropic_summary(
        db, account_id=str(account_id), start=WINDOW[0], end=WINDOW[1]
    )


def test_email_maps_to_member_and_subject_without_creating_rows(db_session, test_user):
    member = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "dev@corp.example",
            "username": "dev-corp",
            "full_name": "Dev",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="gw",
        account_id=test_user.account_id,
        user_id=test_user.id,
        scopes=[],
    )
    subject = crud_gateway_subject.resolve(
        db_session,
        account_id=test_user.account_id,
        api_key_id=api_key.id,
        external_subject="sub-dev",
        email="DEV@corp.example",
    )
    connection = _connect(db_session, test_user.account_id)
    _sync(db_session, connection, FakeAnthropic())
    users_before = db_session.query(models.User).count()
    subjects_before = db_session.query(models.GatewaySubject).count()

    summary = _summary(db_session, test_user.account_id)

    actors = {a["actor"]: a for a in summary["by_actor"]}
    dev = actors["dev@corp.example"]
    assert dev["user_id"] == member.id
    assert dev["mapping_source"] == "member_email"
    assert dev["gateway_subject_id"] == subject.id
    assert dev["num_sessions"] == 5
    assert dev["estimated_cost"] == pytest.approx(1.20)
    assert actors["key:ci-bot"]["user_id"] is None
    assert db_session.query(models.User).count() == users_before
    assert db_session.query(models.GatewaySubject).count() == subjects_before


def test_unknown_email_stays_unmapped(db_session, test_user):
    connection = _connect(db_session, test_user.account_id)
    _sync(db_session, connection, FakeAnthropic())
    users_before = db_session.query(models.User).count()

    dev = {
        a["actor"]: a for a in _summary(db_session, test_user.account_id)["by_actor"]
    }["dev@corp.example"]

    assert dev["user_id"] is None
    assert dev["gateway_subject_id"] is None
    assert db_session.query(models.User).count() == users_before


def test_explicit_mapping_wins_over_member_email(db_session, test_user):
    crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "dev@corp.example",
            "username": "dev-corp",
            "full_name": "Dev",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    connection = _connect(db_session, test_user.account_id)
    crud_anthropic_user_mapping.upsert(
        db_session,
        connection=connection,
        actor="DEV@corp.example",
        user_id=test_user.id,
    )
    crud_anthropic_user_mapping.upsert(
        db_session, connection=connection, actor="key:ci-bot", user_id=test_user.id
    )
    _sync(db_session, connection, FakeAnthropic())

    actors = {
        a["actor"]: a for a in _summary(db_session, test_user.account_id)["by_actor"]
    }
    assert actors["dev@corp.example"]["user_id"] == test_user.id
    assert actors["dev@corp.example"]["mapping_source"] == "mapping"
    assert actors["key:ci-bot"]["user_id"] == test_user.id


def test_connection_payload_never_carries_the_key(db_session, test_user):
    connection = _connect(db_session, test_user.account_id)
    payload = svc.connection_payload(connection)
    assert ADMIN_KEY not in json.dumps(payload, default=str)
    assert payload["has_key"] is True
    assert payload["key_hint"] == "7Kq9"


def test_editing_listed_names_reclassifies_stored_days(db_session, test_user):
    """A listed name is read from the connection at summary time."""
    connection = _connect(
        db_session, test_user.account_id, gateway_key_names=["ci-bot"]
    )
    _sync(db_session, connection, FakeAnthropic())
    assert _summary(db_session, test_user.account_id)["excluded_metered_by_gateway"][
        "actors"
    ] == ["key:ci-bot"]

    crud_anthropic_import_connection.update(
        db_session, db_obj=connection, obj_in={"gateway_key_names": []}
    )
    summary = _summary(db_session, test_user.account_id)
    assert summary["excluded_metered_by_gateway"]["actors"] == []
    assert "key:ci-bot" in {a["actor"] for a in summary["by_actor"]}

    crud_anthropic_import_connection.update(
        db_session,
        db_obj=connection,
        obj_in={"gateway_key_names": ["preloop-gateway"]},
    )
    summary = _summary(db_session, test_user.account_id)
    assert summary["excluded_metered_by_gateway"]["actors"] == ["key:preloop-gateway"]
    assert summary["total_estimated_cost"] == pytest.approx(1.23)


def test_upstream_match_stays_excluded_after_list_edits(db_session, test_user):
    _upstream_model(db_session, test_user.account_id)
    connection = _connect(db_session, test_user.account_id)
    _sync(db_session, connection, FakeAnthropic())
    crud_anthropic_import_connection.update(
        db_session, db_obj=connection, obj_in={"gateway_key_names": ["other"]}
    )
    rows = {r.user_login: r for r in _rows(db_session, test_user.account_id)}
    assert rows["key:preloop-gateway"].raw["gateway_match"] == "upstream_key"
    assert _summary(db_session, test_user.account_id)["excluded_metered_by_gateway"][
        "actors"
    ] == ["key:preloop-gateway"]


def test_inactive_member_is_not_matched_by_email(db_session, test_user):
    crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "dev@corp.example",
            "username": "dev-gone",
            "full_name": "Gone",
            "is_active": False,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    connection = _connect(db_session, test_user.account_id)
    _sync(db_session, connection, FakeAnthropic())

    dev = {
        a["actor"]: a for a in _summary(db_session, test_user.account_id)["by_actor"]
    }["dev@corp.example"]
    assert dev["user_id"] is None
    assert dev["mapping_source"] is None


def test_member_lookup_is_one_batched_query(db_session, test_user, mocker):
    connection = _connect(db_session, test_user.account_id)
    _sync(db_session, connection, FakeAnthropic())
    batched = mocker.spy(crud_anthropic_usage, "active_members_by_email")
    single = mocker.patch(
        "preloop.models.crud.gateway_subject.CRUDGatewaySubject.find_member_by_email",
        side_effect=AssertionError("per-actor lookup"),
    )

    _summary(db_session, test_user.account_id)

    assert batched.call_count == 1
    single.assert_not_called()
