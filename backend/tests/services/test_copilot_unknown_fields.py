"""Vendor fields omitted from a response must not become measured zeroes."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session
from preloop.models import models
from preloop.services import copilot_usage_import as svc


def test_premium_parser_preserves_missing_fields_and_reported_zero() -> None:
    grouped = svc._usage_by_model(
        {"usageItems": [{"model": "unknown-price", "netQuantity": 0}]}
    )
    assert grouped["unknown-price"]["netAmount"] is None
    assert grouped["unknown-price"]["netQuantity"] == 0
    mixed = svc._usage_by_model(
        {"usageItems": [{"model": "a", "netAmount": 2}, {"model": "a"}]}
    )
    assert mixed["a"]["netAmount"] is None


def test_residual_does_not_invent_missing_quantity() -> None:
    rows = svc._residual_rows(
        {"usageItems": [{"model": "a", "netAmount": 2}]},
        [],
        day=date(2026, 10, 1),
        org="example-org",
        scope="organization",
        fetched_at=datetime(2026, 10, 4, tzinfo=UTC),
    )
    assert rows[0]["cost_amount"] == 2
    assert rows[0]["raw"]["netQuantity"] is None


def test_summary_preserves_unknown_amounts_and_quantities() -> None:
    with (
        patch.object(svc, "crud_copilot_import_connection") as connections,
        patch.object(svc, "crud_copilot_usage") as usage,
    ):
        connections.get_for_account.return_value = None
        usage.premium_request_totals.return_value = [
            {"user_login": "jane", "model": "a", "net_amount": None, "net_quantity": 2},
            {"user_login": "jane", "model": "b", "net_amount": 1, "net_quantity": None},
        ]
        usage.list_rows.return_value = []
        usage.latest_seat_snapshot.return_value = {"summary": None, "seats": []}
        result = svc.build_copilot_summary(
            MagicMock(),
            account_id="account",
            start=datetime(2026, 10, 1, tzinfo=UTC),
            end=datetime(2026, 10, 4, tzinfo=UTC),
        )
    premium = result["premium_requests"]
    assert premium["total_net_amount"] is None
    assert premium["by_developer"][0]["net_amount"] is None
    assert premium["by_developer"][0]["net_quantity"] is None
    assert premium["by_model"][0]["net_amount"] == 1
    assert result["metered_by_gateway"] is False


def test_metrics_preserve_editor_counters_without_free_text() -> None:
    reduced = svc.reduce_metrics_record(
        {
            "totals_by_ide": [
                {
                    "ide": "vscode",
                    "user_initiated_interaction_count": 3,
                    "prompt": "excluded",
                }
            ],
            "code_generation_activity_count": True,
        }
    )
    assert reduced["editors"] == ["vscode"]
    assert reduced["editor_usage"] == [
        {"editor": "vscode", "user_initiated_interaction_count": 3}
    ]
    assert "code_generation_activity_count" not in reduced


def test_crud_does_not_sum_partial_vendor_fields_as_complete_totals(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.models.crud import crud_copilot_usage
    from preloop.models.crud.copilot_import import LINE_ITEM_PREMIUM_REQUEST

    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 4, tzinfo=UTC)
    for day, amount, quantity in [(1, 1, None), (2, None, 2)]:
        bucket = datetime(2026, 10, day, tzinfo=UTC)
        crud_copilot_usage.replace_day_rows(
            db_session,
            account_id=test_user.account_id,
            bucket_start=bucket,
            line_items=(LINE_ITEM_PREMIUM_REQUEST,),
            rows=[
                {
                    "provider": "copilot",
                    "granularity": "1d",
                    "bucket_start": bucket,
                    "bucket_end": end,
                    "fetched_at": end,
                    "line_item": LINE_ITEM_PREMIUM_REQUEST,
                    "project_or_workspace_id": "example-org",
                    "user_login": "jane",
                    "model": "a",
                    "usage_source": "imported",
                    "cost_amount": amount,
                    "raw": {"netQuantity": quantity},
                }
            ],
        )
    totals = crud_copilot_usage.premium_request_totals(
        db_session,
        account_id=test_user.account_id,
        organization="example-org",
        start=start,
        end=end,
    )
    assert totals[0]["net_amount"] is None
    assert totals[0]["net_quantity"] is None
