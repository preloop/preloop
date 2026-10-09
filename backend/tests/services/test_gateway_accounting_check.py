"""Tests for gateway accounting health-check threshold logic."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import OperationalError

from preloop.services import gateway_accounting_check as gac


NEW_KEYS = {"token_details_normalized", "subscription_billing_coverage"}


def _subscription(requests: int = 0, covered: int = 0) -> dict:
    return {
        "request_count": requests,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "coverage_rows": covered,
        "api_equivalent_cost": 0.0,
    }


def _counters(**overrides: int) -> dict[str, int]:
    base = {
        "total_rows": 0,
        "success_rows": 0,
        "streaming_rows": 0,
        "streaming_rows_with_tokens": 0,
        "priceable_rows": 0,
        "priced_rows": 0,
        "unpriced_source_rows": 0,
        "usage_source_rows": 0,
        "provider_usage_rows": 0,
    }
    base.update(overrides)
    return base


class TestStreamingCheck:
    def test_skips_when_no_streaming(self) -> None:
        result = gac._streaming_check(_counters())
        assert result["status"] == "skip"

    def test_passes_at_healthy_share(self) -> None:
        result = gac._streaming_check(
            _counters(streaming_rows=10, streaming_rows_with_tokens=9)
        )
        assert result["status"] == "pass"

    def test_fails_below_threshold(self) -> None:
        result = gac._streaming_check(
            _counters(streaming_rows=10, streaming_rows_with_tokens=8)
        )
        assert result["status"] == "fail"


class TestCostsPricedCheck:
    def test_skips_when_no_priceable(self) -> None:
        assert gac._costs_priced_check(_counters())["status"] == "skip"

    def test_passes_at_healthy_share(self) -> None:
        result = gac._costs_priced_check(
            _counters(priceable_rows=10, priced_rows=9, unpriced_source_rows=1)
        )
        assert result["status"] == "pass"

    def test_fails_below_threshold(self) -> None:
        result = gac._costs_priced_check(
            _counters(priceable_rows=10, priced_rows=8, unpriced_source_rows=2)
        )
        assert result["status"] == "fail"


class TestUsageSourceCheck:
    def test_skips_when_no_source_rows(self) -> None:
        assert gac._usage_source_check(_counters())["status"] == "skip"

    def test_passes_above_warn_share(self) -> None:
        result = gac._usage_source_check(
            _counters(usage_source_rows=10, provider_usage_rows=8)
        )
        assert result["status"] == "pass"
        assert "below the expected" not in result["detail"]

    def test_warns_but_passes_between_fail_and_warn(self) -> None:
        result = gac._usage_source_check(
            _counters(usage_source_rows=10, provider_usage_rows=6)
        )
        assert result["status"] == "pass"
        assert "below the expected" in result["detail"]

    def test_fails_below_fail_share(self) -> None:
        result = gac._usage_source_check(
            _counters(usage_source_rows=10, provider_usage_rows=4)
        )
        assert result["status"] == "fail"


class TestRunAccountingChecks:
    def test_no_traffic_skips_all(self) -> None:
        db = MagicMock()
        with patch.object(
            gac.crud_api_usage,
            "get_accounting_health_counters",
            return_value=_counters(),
        ):
            result = gac.run_accounting_checks(db, account_id="acct", window_hours=24)
        assert result["status"] == "skip"
        assert all(check["status"] == "skip" for check in result["checks"])

    def test_healthy_traffic_passes(self) -> None:
        db = MagicMock()
        counters = _counters(
            total_rows=10,
            success_rows=10,
            streaming_rows=10,
            streaming_rows_with_tokens=10,
            priceable_rows=10,
            priced_rows=10,
            usage_source_rows=10,
            provider_usage_rows=10,
        )
        with (
            patch.object(
                gac.crud_api_usage,
                "get_accounting_health_counters",
                return_value=counters,
            ),
            patch.object(gac, "_audit_table_exists", return_value=True),
            patch.object(gac.crud_audit_log, "count_by_account", return_value=3),
            patch.object(gac.crud_api_usage, "list_token_detail_rows", return_value=[]),
            patch.object(
                gac.crud_api_usage,
                "get_subscription_usage_summary",
                return_value=_subscription(),
            ),
        ):
            result = gac.run_accounting_checks(db, account_id="acct", window_hours=24)
        assert result["status"] == "pass"
        assert all(
            check["status"] == ("skip" if check["key"] in NEW_KEYS else "pass")
            for check in result["checks"]
        )

    def test_db_error_reports_fail_instead_of_raising(self) -> None:
        db = MagicMock()
        with patch.object(
            gac.crud_api_usage,
            "get_accounting_health_counters",
            side_effect=OperationalError("SELECT", {}, Exception("down")),
        ):
            result = gac.run_accounting_checks(db, account_id="acct", window_hours=24)
        assert result["status"] == "fail"
        assert result["checks"][0]["key"] == "gateway_traffic_seen"
        assert result["checks"][0]["status"] == "fail"

    def test_audit_db_error_marks_audit_check_failed(self) -> None:
        db = MagicMock()
        counters = _counters(
            total_rows=5,
            streaming_rows=0,
            priceable_rows=0,
            usage_source_rows=0,
        )
        with (
            patch.object(
                gac.crud_api_usage,
                "get_accounting_health_counters",
                return_value=counters,
            ),
            patch.object(
                gac,
                "_audit_events_check",
                side_effect=OperationalError("SELECT", {}, Exception("down")),
            ),
        ):
            result = gac.run_accounting_checks(db, account_id="acct", window_hours=24)
        assert result["status"] == "fail"
        audit = next(c for c in result["checks"] if c["key"] == "audit_events_present")
        assert audit["status"] == "fail"


def test_audit_events_check_uses_count_gt_zero() -> None:
    db = MagicMock()
    with (
        patch.object(gac, "_audit_table_exists", return_value=True),
        patch.object(gac.crud_audit_log, "count_by_account", return_value=0),
    ):
        result = gac._audit_events_check(
            db, account_id="acct", start=datetime.now(timezone.utc)
        )
    assert result["status"] == "fail"


def _detail_row(usage, **columns):
    row = MagicMock()
    row.usage_details = usage
    for name in ("cache_read_tokens", "cache_creation_tokens", "reasoning_tokens"):
        setattr(row, name, columns.get(name))
    return row


class TestTokenDetailsCheck:
    def _run(self, rows):
        with patch.object(
            gac.crud_api_usage, "list_token_detail_rows", return_value=rows
        ):
            return gac._token_details_check(
                MagicMock(), account_id="acct", start=datetime.now(timezone.utc)
            )

    def test_skips_without_detail_rows(self) -> None:
        assert self._run([])["status"] == "skip"

    def test_passes_when_columns_match(self) -> None:
        rows = [
            _detail_row(
                {"input_tokens_details": {"cached_tokens": 0}}, cache_read_tokens=0
            )
        ]
        assert self._run(rows)["status"] == "pass"

    def test_warns_on_null_column(self) -> None:
        rows = [_detail_row({"output_tokens_details": {"reasoning_tokens": 4}})]
        result = self._run(rows)
        assert result["status"] == "warn"
        assert "reasoning_tokens=1" in result["detail"]

    def test_unknown_raw_value_is_not_a_mismatch(self) -> None:
        rows = [_detail_row({"input_tokens_details": {"cached_tokens": -1}})]
        assert self._run(rows)["status"] == "pass"


class TestSubscriptionCoverageCheck:
    def _run(self, summary):
        with patch.object(
            gac.crud_api_usage, "get_subscription_usage_summary", return_value=summary
        ):
            return gac._subscription_coverage_check(
                MagicMock(), account_id="acct", start=datetime.now(timezone.utc)
            )

    def test_skips_without_subscription_rows(self) -> None:
        assert self._run(_subscription())["status"] == "skip"

    def test_warns_even_with_full_estimate_coverage(self) -> None:
        result = self._run(_subscription(requests=4, covered=4))
        assert result["status"] == "warn"
        assert result["detail"].startswith(gac.SUBSCRIPTION_BILLING_UNAVAILABLE)
        assert "4/4 (100%)" in result["detail"]


def test_warn_outranks_pass_but_not_fail() -> None:
    db = MagicMock()
    counters = _counters(total_rows=2, priceable_rows=2, priced_rows=2)
    with (
        patch.object(
            gac.crud_api_usage, "get_accounting_health_counters", return_value=counters
        ),
        patch.object(gac, "_audit_table_exists", return_value=False),
        patch.object(gac.crud_api_usage, "list_token_detail_rows", return_value=[]),
        patch.object(
            gac.crud_api_usage,
            "get_subscription_usage_summary",
            return_value=_subscription(requests=2, covered=2),
        ),
    ):
        result = gac.run_accounting_checks(db, account_id="acct", window_hours=24)
    assert result["status"] == "warn"


def test_fail_outranks_warn() -> None:
    db = MagicMock()
    counters = _counters(total_rows=2, priceable_rows=2, priced_rows=0)
    with (
        patch.object(
            gac.crud_api_usage, "get_accounting_health_counters", return_value=counters
        ),
        patch.object(gac, "_audit_table_exists", return_value=False),
        patch.object(gac.crud_api_usage, "list_token_detail_rows", return_value=[]),
        patch.object(
            gac.crud_api_usage,
            "get_subscription_usage_summary",
            return_value=_subscription(requests=2, covered=2),
        ),
    ):
        result = gac.run_accounting_checks(db, account_id="acct", window_hours=24)
    statuses = {check["key"]: check["status"] for check in result["checks"]}
    assert statuses["costs_priced"] == "fail"
    assert statuses["subscription_billing_coverage"] == "warn"
    assert result["status"] == "fail"
