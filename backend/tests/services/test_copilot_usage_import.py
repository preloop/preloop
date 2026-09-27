"""Tests for the GitHub Copilot usage import (issue #788).

GitHub is replaced by an ``httpx.MockTransport`` so every request the importer
makes is recorded and checked against the routes the issue allows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_api_usage,
    crud_copilot_import_connection,
    crud_copilot_usage,
)
from preloop.models.crud.copilot_import import (
    LINE_ITEM_PREMIUM_REQUEST,
    LINE_ITEM_SEAT,
    LINE_ITEM_USAGE_METRICS,
)
from preloop.services import copilot_usage_import as svc
from preloop.services.secret_service import get_secret_service

ORG = "example-org"
ENTERPRISE = "example-ent"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
DAY = date(2026, 9, 24)
ORG_TOKEN = "org-token-value"
ENT_TOKEN = "enterprise-token-value"
DOWNLOAD_URL = "https://reports.example.test/signed/users-1-day.ndjson"

ORG_PREMIUM = f"/organizations/{ORG}/settings/billing/premium_request/usage"
ENT_PREMIUM = f"/enterprises/{ENTERPRISE}/settings/billing/premium_request/usage"
METRICS = f"/orgs/{ORG}/copilot/metrics/reports/users-1-day"
ALLOWED_PATHS = {
    f"/orgs/{ORG}/copilot/billing",
    f"/orgs/{ORG}/copilot/billing/seats",
    ORG_PREMIUM,
    ENT_PREMIUM,
    METRICS,
}


def _usage(*items: Dict[str, Any]) -> Dict[str, Any]:
    return {"timePeriod": {"year": 2026}, "usageItems": list(items)}


def _item(model: str, net: float, qty: float) -> Dict[str, Any]:
    return {
        "product": "Copilot",
        "sku": "Copilot Premium Request",
        "model": model,
        "unitType": "requests",
        "pricePerUnit": 0.04,
        "grossQuantity": qty,
        "grossAmount": net,
        "discountQuantity": 0,
        "discountAmount": 0,
        "netQuantity": qty,
        "netAmount": net,
    }


@dataclass
class FakeGitHub:
    """Programmable GitHub stand-in that records every request."""

    org_premium_status: int = 200
    ent_premium_status: int = 200
    org_aggregate_status: int = 200
    ent_aggregate_status: int = 200
    metrics_status: int = 200
    per_user: Dict[str, Dict[str, Any]] = field(
        default_factory=lambda: {
            "alice": _usage(_item("model-a", 1.20, 30), _item("model-b", 0.40, 10)),
            "bob": _usage(_item("model-a", 0.80, 20)),
        }
    )
    aggregate: Dict[str, Any] = field(
        default_factory=lambda: _usage(_item("model-a", 2.00, 50))
    )
    report: str = "\n".join(
        [
            json.dumps(
                {
                    "user_login": "alice",
                    "day": "2026-09-24",
                    "user_initiated_interaction_count": 7,
                    "prompt": "free text that must never be stored",
                    "totals_by_ide": [{"ide": "vscode", "extra": "x"}],
                    "totals_by_feature": [
                        {"feature": "chat", "user_initiated_interaction_count": 7}
                    ],
                    "totals_by_model_feature": [
                        {
                            "model": "model-a",
                            "feature": "chat",
                            "user_initiated_interaction_count": 5,
                        },
                        {
                            "model": "model-b",
                            "feature": "chat",
                            "user_initiated_interaction_count": 2,
                        },
                    ],
                    "ai_credits_used": 3,
                }
            ),
            json.dumps({"user_login": "bob", "user_initiated_interaction_count": 1}),
        ]
    )
    requests: List[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = dict(request.url.params)
        if request.url.host == "reports.example.test":
            return httpx.Response(200, text=self.report)
        if path == f"/orgs/{ORG}/copilot/billing":
            return httpx.Response(
                200,
                json={
                    "seat_breakdown": {"total": 2, "active_this_cycle": 2},
                    "plan_type": "business",
                },
            )
        if path == f"/orgs/{ORG}/copilot/billing/seats":
            return httpx.Response(
                200,
                json={
                    "total_seats": 2,
                    "seats": [
                        {
                            "assignee": {
                                "login": "alice",
                                "name": "Private Name",
                                "email": "private@example.test",
                            },
                            "last_activity_at": "2026-09-26T10:00:00Z",
                            "last_activity_editor": "vscode/1.0",
                            "plan_type": "business",
                        },
                        {
                            "assignee": {"login": "bob"},
                            "last_activity_at": None,
                            "last_activity_editor": None,
                        },
                    ],
                },
            )
        if path in (ORG_PREMIUM, ENT_PREMIUM):
            is_org = path == ORG_PREMIUM
            user = params.get("user")
            if user:
                status = self.org_premium_status if is_org else self.ent_premium_status
                if status != 200:
                    return httpx.Response(status, json={"message": "Forbidden"})
                return httpx.Response(200, json={"user": user, **self.per_user[user]})
            status = self.org_aggregate_status if is_org else self.ent_aggregate_status
            if status != 200:
                return httpx.Response(status, json={"message": "Forbidden"})
            return httpx.Response(200, json=self.aggregate)
        if path == METRICS:
            if self.metrics_status != 200:
                return httpx.Response(self.metrics_status)
            return httpx.Response(
                200,
                json={"download_links": [DOWNLOAD_URL], "report_day": "2026-09-24"},
            )
        return httpx.Response(599, json={"message": f"unexpected {path}"})

    def factory(self) -> Callable[[], httpx.Client]:
        return lambda: httpx.Client(transport=httpx.MockTransport(self.handler))

    def api_requests(self) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.host == "api.github.com"]


def _make_connection(
    db: Session,
    account_id: Any,
    *,
    enterprise: Optional[str] = None,
    enterprise_token: Optional[str] = None,
    seat_price: Optional[float] = None,
    last_synced_day: Optional[date] = None,
) -> models.CopilotImportConnection:
    secrets = get_secret_service()
    org_secret = secrets.create_local_secret_reference(
        db,
        account_id=account_id,
        name="org",
        secret_kind=svc.COPILOT_IMPORT_SECRET_KIND,
        secret_value=ORG_TOKEN,
    )
    ent_secret_id = None
    if enterprise_token:
        ent_secret_id = secrets.create_local_secret_reference(
            db,
            account_id=account_id,
            name="ent",
            secret_kind=svc.COPILOT_IMPORT_SECRET_KIND,
            secret_value=enterprise_token,
        ).id
    return crud_copilot_import_connection.create(
        db,
        obj_in={
            "account_id": account_id,
            "organization": ORG,
            "enterprise": enterprise,
            "secret_reference_id": org_secret.id,
            "enterprise_secret_reference_id": ent_secret_id,
            "seat_price_monthly": seat_price,
            "last_synced_day": last_synced_day,
        },
    )


def _rows(db: Session, account_id: Any, line_item: str) -> List[Any]:
    return crud_copilot_usage.list_rows(
        db,
        account_id=str(account_id),
        line_item=line_item,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2027, 1, 1, tzinfo=UTC),
    )


def _window() -> Dict[str, datetime]:
    return {
        "start": datetime(2026, 9, 1, tzinfo=UTC),
        "end": datetime(2026, 9, 28, tzinfo=UTC),
    }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_latest_available_day_waits_out_the_freshness_window() -> None:
    assert svc.latest_available_day(NOW) == date(2026, 9, 24)
    late = datetime(2026, 9, 27, 23, 59, tzinfo=UTC)
    assert svc.latest_available_day(late) == date(2026, 9, 24)


def test_days_to_sync_first_run_catch_up_and_cap() -> None:
    assert svc.days_to_sync(None, DAY) == [DAY]
    assert svc.days_to_sync(DAY, DAY) == [DAY]
    assert svc.days_to_sync(DAY - timedelta(days=2), DAY) == [
        DAY - timedelta(days=1),
        DAY,
    ]
    capped = svc.days_to_sync(DAY - timedelta(days=30), DAY)
    assert len(capped) == svc.MAX_CATCHUP_DAYS
    assert capped[-1] == DAY


def test_parse_report_accepts_ndjson_array_and_object() -> None:
    assert svc.parse_report('{"a": 1}\n\nnot json\n{"b": 2}\n') == [
        {"a": 1},
        {"b": 2},
    ]
    assert svc.parse_report('[{"a": 1}, 3]') == [{"a": 1}]
    assert svc.parse_report('{"a": 1}') == [{"a": 1}]
    assert svc.parse_report("  ") == []


def test_reduce_metrics_record_keeps_only_allowlisted_fields() -> None:
    reduced = svc.reduce_metrics_record(
        {
            "user_login": "alice",
            "prompt": "never stored",
            "user_initiated_interaction_count": 4,
            "totals_by_ide": [{"ide": "vscode", "note": "x"}, {"ide": "vscode"}],
            "totals_by_feature": [{"feature": "chat", "free": "y", "loc": 3}],
            "totals_by_model_feature": [
                {"model": "m", "feature": "chat", "code_generation_activity_count": 2}
            ],
            "ai_credits_used": 1.5,
        }
    )
    assert reduced == {
        "user_initiated_interaction_count": 4,
        "editors": ["vscode"],
        "features": [{"feature": "chat"}],
        "models": [
            {"model": "m", "feature": "chat", "code_generation_activity_count": 2}
        ],
        "ai_credits_used": 1.5,
    }


def test_download_refuses_plain_http() -> None:
    client = svc.GitHubCopilotClient(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        "t",
    )
    with pytest.raises(svc.CopilotImportError):
        client.download("http://reports.example.test/file")


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def test_owner_sync_stores_seats_per_user_spend_and_metrics(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    connection = _make_connection(db_session, account_id)
    github = FakeGitHub()

    result = svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=github.factory()
    )

    assert result == {"days": ["2026-09-24"], "per_user": True, "error": None}
    seats = _rows(db_session, account_id, LINE_ITEM_SEAT)
    assert {seat.user_login for seat in seats} == {"alice", "bob"}
    alice_seat = next(seat for seat in seats if seat.user_login == "alice")
    assert alice_seat.raw["last_activity_at"] == "2026-09-26T10:00:00Z"
    assert alice_seat.raw["last_activity_editor"] == "vscode/1.0"
    assert "Private Name" not in json.dumps([seat.raw for seat in seats])
    assert "private@example.test" not in json.dumps([seat.raw for seat in seats])

    premium = _rows(db_session, account_id, LINE_ITEM_PREMIUM_REQUEST)
    by_key = {(row.user_login, row.model): row for row in premium}
    assert set(by_key) == {
        ("alice", "model-a"),
        ("alice", "model-b"),
        ("bob", "model-a"),
    }
    row = by_key[("alice", "model-a")]
    assert row.provider == "copilot"
    assert row.usage_source == "imported"
    assert row.cost_basis == "reconciled"
    assert row.cost_amount == pytest.approx(1.20)
    assert row.bucket_start == datetime(2026, 9, 24, tzinfo=UTC)
    assert row.raw["netQuantity"] == 30

    metrics = _rows(db_session, account_id, LINE_ITEM_USAGE_METRICS)
    assert {m.user_login for m in metrics} == {"alice", "bob"}
    assert "never be stored" not in json.dumps([m.raw for m in metrics])

    db_session.refresh(connection)
    assert connection.last_synced_day == DAY
    assert connection.last_error is None
    assert connection.per_user_billing_status == "available"
    assert connection.metrics_status == "available"


def test_requests_use_only_the_cited_routes_and_headers(
    db_session: Session, test_user: models.User
) -> None:
    connection = _make_connection(db_session, test_user.account_id)
    github = FakeGitHub()
    svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=github.factory()
    )

    api = github.api_requests()
    assert api
    assert {r.url.path for r in api} <= ALLOWED_PATHS
    assert all(r.method == "GET" for r in github.requests)
    for request in api:
        assert request.headers["Authorization"] == f"Bearer {ORG_TOKEN}"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
    # The legacy metrics route is never called.
    assert not any(r.url.path == f"/orgs/{ORG}/copilot/metrics" for r in api)
    premium = [r for r in api if r.url.path == ORG_PREMIUM]
    assert {r.url.params["user"] for r in premium} == {"alice", "bob"}
    assert all(
        (r.url.params["year"], r.url.params["month"], r.url.params["day"])
        == ("2026", "9", "24")
        for r in premium
    )
    downloads = [r for r in github.requests if r.url.host == "reports.example.test"]
    assert len(downloads) == 1
    assert "Authorization" not in downloads[0].headers


def test_second_sync_is_idempotent(db_session: Session, test_user: models.User) -> None:
    account_id = test_user.account_id
    connection = _make_connection(db_session, account_id)
    github = FakeGitHub()
    for _ in range(2):
        svc.sync_connection(
            db_session, connection, now=NOW, http_client_factory=github.factory()
        )

    assert len(_rows(db_session, account_id, LINE_ITEM_PREMIUM_REQUEST)) == 3
    assert len(_rows(db_session, account_id, LINE_ITEM_SEAT)) == 2
    assert len(_rows(db_session, account_id, LINE_ITEM_USAGE_METRICS)) == 2


def test_org_403_falls_back_to_enterprise_route(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    connection = _make_connection(
        db_session,
        account_id,
        enterprise=ENTERPRISE,
        enterprise_token=ENT_TOKEN,
    )
    github = FakeGitHub(org_premium_status=403)

    result = svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=github.factory()
    )

    assert result["per_user"] is True
    ent = [r for r in github.api_requests() if r.url.path == ENT_PREMIUM]
    assert ent
    assert all(r.url.params["organization"] == ORG for r in ent)
    assert all(r.headers["Authorization"] == f"Bearer {ENT_TOKEN}" for r in ent)
    premium = _rows(db_session, account_id, LINE_ITEM_PREMIUM_REQUEST)
    assert len(premium) == 3
    assert all(row.raw["scope"] == "enterprise" for row in premium)


def test_falls_back_to_org_aggregate_with_reason(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    connection = _make_connection(db_session, account_id)
    github = FakeGitHub(org_premium_status=403)

    result = svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=github.factory()
    )

    assert result["per_user"] is False
    assert result["error"] is None
    premium = _rows(db_session, account_id, LINE_ITEM_PREMIUM_REQUEST)
    assert len(premium) == 1
    assert premium[0].user_login is None
    assert premium[0].cost_amount == pytest.approx(2.00)
    reason = premium[0].raw["per_user_unavailable_reason"]
    assert "403" in reason
    assert "enterprise" in reason
    db_session.refresh(connection)
    assert connection.per_user_billing_status == "unavailable"
    assert connection.per_user_billing_reason == reason

    summary = svc.build_copilot_summary(
        db_session, account_id=str(account_id), **_window()
    )
    premium_summary = summary["premium_requests"]
    assert premium_summary["per_user_status"] == "unavailable"
    assert premium_summary["per_user_unavailable_reason"] == reason
    assert premium_summary["org_aggregate_net_amount"] == pytest.approx(2.00)
    assert premium_summary["by_developer"] == []


def test_per_user_resync_replaces_the_aggregate(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    connection = _make_connection(db_session, account_id)
    svc.sync_connection(
        db_session,
        connection,
        now=NOW,
        http_client_factory=FakeGitHub(org_premium_status=403).factory(),
    )
    svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=FakeGitHub().factory()
    )

    premium = _rows(db_session, account_id, LINE_ITEM_PREMIUM_REQUEST)
    assert len(premium) == 3
    assert all(row.user_login for row in premium)


def test_every_route_refused_is_an_explicit_error(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    connection = _make_connection(db_session, account_id)
    github = FakeGitHub(org_premium_status=403, org_aggregate_status=403)

    result = svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=github.factory()
    )

    assert result["days"] == []
    assert "could not be read" in result["error"]
    assert "403" in result["error"]
    assert _rows(db_session, account_id, LINE_ITEM_PREMIUM_REQUEST) == []
    db_session.refresh(connection)
    assert connection.last_error == result["error"]
    assert connection.last_synced_day is None

    summary = svc.build_copilot_summary(
        db_session, account_id=str(account_id), **_window()
    )
    # No rows is "no data", never a silent zero.
    assert summary["premium_requests"]["total_net_amount"] is None
    assert summary["connection"]["last_error"] == result["error"]


def test_seat_403_is_an_explicit_error(
    db_session: Session, test_user: models.User
) -> None:
    connection = _make_connection(db_session, test_user.account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    result = svc.sync_connection(
        db_session,
        connection,
        now=NOW,
        http_client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(handler)
        ),
    )
    assert "403" in result["error"]
    assert "organization owner" in result["error"]


def test_revoked_token_is_an_explicit_error(
    db_session: Session, test_user: models.User
) -> None:
    connection = _make_connection(db_session, test_user.account_id)
    result = svc.sync_connection(
        db_session,
        connection,
        now=NOW,
        http_client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(401))
        ),
    )
    assert "401" in result["error"]


def test_metrics_403_keeps_premium_rows_and_records_reason(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    connection = _make_connection(db_session, account_id)
    github = FakeGitHub(metrics_status=403)

    result = svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=github.factory()
    )

    assert result["error"] is None
    assert len(_rows(db_session, account_id, LINE_ITEM_PREMIUM_REQUEST)) == 3
    assert _rows(db_session, account_id, LINE_ITEM_USAGE_METRICS) == []
    db_session.refresh(connection)
    assert connection.metrics_status == "unavailable"
    assert "403" in connection.metrics_reason
    assert "View Organization Copilot Metrics" in connection.metrics_reason


def test_metrics_204_is_available_without_rows(
    db_session: Session, test_user: models.User
) -> None:
    connection = _make_connection(db_session, test_user.account_id)
    github = FakeGitHub(metrics_status=204)
    svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=github.factory()
    )
    db_session.refresh(connection)
    assert connection.metrics_status == "available"
    assert connection.metrics_reason is None


def test_import_never_writes_gateway_usage(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    before = crud_api_usage.get_multi(db_session, limit=10_000)
    connection = _make_connection(db_session, account_id)
    svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=FakeGitHub().factory()
    )
    after = crud_api_usage.get_multi(db_session, limit=10_000)
    assert len(after) == len(before)


def test_ingest_skips_inactive_connections(
    db_session: Session, test_user: models.User
) -> None:
    connection = _make_connection(db_session, test_user.account_id)
    crud_copilot_import_connection.update(
        db_session, db_obj=connection, obj_in={"is_active": False}
    )
    github = FakeGitHub()
    svc.ingest_copilot_usage(
        db_session,
        account_id=str(test_user.account_id),
        now=NOW,
        http_client_factory=github.factory(),
    )
    assert github.requests == []


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summary_seat_estimate_uses_operator_price(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    connection = _make_connection(db_session, account_id, seat_price=19.0)
    svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=FakeGitHub().factory()
    )

    summary = svc.build_copilot_summary(
        db_session, account_id=str(account_id), **_window()
    )

    assert summary["metered_by_gateway"] is False
    assert summary["marker"] == "Not metered by the gateway"
    seats = summary["seats"]
    assert seats["total_seats"] == 2
    assert seats["seat_price_monthly"] == 19.0
    assert seats["monthly_seat_estimate"] == pytest.approx(38.0)
    assert {s["login"] for s in seats["assigned"]} == {"alice", "bob"}

    premium = summary["premium_requests"]
    assert premium["per_user_status"] == "available"
    assert premium["total_net_amount"] == pytest.approx(2.40)
    assert [d["login"] for d in premium["by_developer"]] == ["alice", "bob"]
    assert premium["by_developer"][0]["net_amount"] == pytest.approx(1.60)
    assert [m["model"] for m in premium["by_model"]] == ["model-a", "model-b"]
    assert premium["by_model"][0]["net_amount"] == pytest.approx(2.00)

    mix = {entry["login"]: entry for entry in summary["model_mix"]}
    assert mix["alice"]["basis"] == "net_amount"
    shares = {m["model"]: m["share"] for m in mix["alice"]["models"]}
    assert shares["model-a"] == pytest.approx(0.75)
    assert shares["model-b"] == pytest.approx(0.25)


def test_summary_without_seat_price_has_no_dollar_seat_line(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    connection = _make_connection(db_session, account_id, seat_price=None)
    svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=FakeGitHub().factory()
    )
    summary = svc.build_copilot_summary(
        db_session, account_id=str(account_id), **_window()
    )
    assert summary["seats"]["total_seats"] == 2
    assert summary["seats"]["seat_price_monthly"] is None
    assert summary["seats"]["monthly_seat_estimate"] is None


def test_model_mix_falls_back_to_metrics_requests(
    db_session: Session, test_user: models.User
) -> None:
    account_id = test_user.account_id
    connection = _make_connection(db_session, account_id)
    github = FakeGitHub(
        per_user={"alice": _usage(), "bob": _usage()},
    )
    svc.sync_connection(
        db_session, connection, now=NOW, http_client_factory=github.factory()
    )
    summary = svc.build_copilot_summary(
        db_session, account_id=str(account_id), **_window()
    )
    mix = {entry["login"]: entry for entry in summary["model_mix"]}
    assert mix["alice"]["basis"] == "requests"
    shares = {m["model"]: m["share"] for m in mix["alice"]["models"]}
    assert shares["model-a"] == pytest.approx(5 / 7)
    assert shares["model-b"] == pytest.approx(2 / 7)


def test_summary_without_connection_is_empty(
    db_session: Session, test_user: models.User
) -> None:
    summary = svc.build_copilot_summary(
        db_session, account_id=str(test_user.account_id), **_window()
    )
    assert summary["connection"] is None
    assert summary["seats"]["total_seats"] is None
    assert summary["premium_requests"]["per_user_status"] == "no_data"
    assert summary["marker"] == "Not metered by the gateway"
