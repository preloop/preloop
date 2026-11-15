"""Missing plugin handlers warn once and count every skipped invocation."""

import logging
from collections import Counter
from unittest.mock import patch

import pytest

from preloop.plugins import base
from preloop.sync import tasks


@pytest.mark.parametrize(
    "task,service",
    [
        ("ingest_provider_billing", "provider_billing_ingestion"),
        ("send_optimization_digest", "optimization_digest"),
        ("reconcile_stripe_subscriptions", "subscription_sync"),
        ("governance_session_digest_sweep", "governance_session_digest_sweep"),
        ("governance_weekly_review", "governance_weekly_review"),
    ],
)
def test_missing_handler_warns_once_and_counts(
    task: str,
    service: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated skips remain observable without flooding worker logs."""
    from preloop.sync import plugin_status

    monkeypatch.setattr(plugin_status, "_skips", Counter())
    with patch.object(base, "get_plugin_manager") as manager:
        manager.return_value.get_service.return_value = None
        with caplog.at_level(logging.WARNING):
            getattr(tasks, task)()
            getattr(tasks, task)()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert task in warnings[0].getMessage()
    assert service in warnings[0].getMessage()
    from preloop.sync.plugin_status import skipped_tasks

    assert skipped_tasks()[task] >= 2
