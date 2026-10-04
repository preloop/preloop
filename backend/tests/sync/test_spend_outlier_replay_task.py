"""The daily task registers the Copilot spend source in a fresh worker (#1061)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from unittest.mock import MagicMock

import pytest

from preloop.services import copilot_spend_source as src
from preloop.services import spend_outliers
from preloop.sync import tasks


@pytest.fixture
def unregister_after():
    yield
    spend_outliers.unregister_imported_spend_source(src.copilot_imported_spend)


def test_task_registers_the_source_exactly_once_before_the_pass(
    monkeypatch, unregister_after
):
    """Three runs of the task leave one registration, made before the pass."""
    db = MagicMock()
    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([db]))
    seen: list[int] = []

    def fake_pass(session, now=None):
        seen.append(
            spend_outliers.registered_imported_spend_sources().count(
                src.copilot_imported_spend
            )
        )
        return {"accounts": 0, "findings": 0}

    monkeypatch.setattr("preloop.services.spend_outliers.run_daily_pass", fake_pass)

    for _ in range(3):
        assert tasks.evaluate_spend_outliers() == {"accounts": 0, "findings": 0}

    assert seen == [1, 1, 1]
    assert (
        spend_outliers.registered_imported_spend_sources().count(
            src.copilot_imported_spend
        )
        == 1
    )
    assert db.close.call_count == 3


def test_fresh_process_registers_without_http_router_or_sync():
    """A new interpreter registers the source from stored data alone.

    No endpoint module and no GitHub client (``httpx``) may be imported by the
    registration path, and the source answers for an account that was never
    synced (it has no connection) without any network access.
    """
    script = textwrap.dedent(
        """
        import sys
        from unittest.mock import MagicMock
        from preloop.services.copilot_spend_source import (
            copilot_imported_spend, register_copilot_spend_source,
        )
        from preloop.services import spend_outliers
        register_copilot_spend_source()
        register_copilot_spend_source()
        sources = spend_outliers.registered_imported_spend_sources()
        assert sources.count(copilot_imported_spend) == 1, sources
        routers = [m for m in sys.modules if m.startswith("preloop.api.endpoints")]
        assert routers == [], routers
        assert "httpx" not in sys.modules
        assert "preloop.services.copilot_usage_import" not in sys.modules
        print("ok")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("ok")
