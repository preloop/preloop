"""Tests for the SQLAlchemy connection pool monitor."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.db_pool_monitor import (
    DbPoolMonitor,
    collect_pool_stats,
    db_monitoring_enabled,
    register_otel_pool_gauges,
)


def make_pool(size=8, checked_out=0, checked_in=None, overflow=0):
    pool = MagicMock()
    pool.size.return_value = size
    pool.checkedout.return_value = checked_out
    pool.checkedin.return_value = (
        checked_in if checked_in is not None else max(size - checked_out, 0)
    )
    pool.overflow.return_value = overflow
    pool.status.return_value = (
        f"Pool size: {size} Connections in pool: {max(size - checked_out, 0)}"
    )
    return pool


def make_engine(pool):
    engine = MagicMock()
    engine.pool = pool
    engine.sync_engine.pool = pool
    return engine


def patch_engines(sync_engine=None, async_engine=None):
    return (
        patch(
            "preloop.models.db.session.get_engine_if_initialized",
            return_value=sync_engine,
        ),
        patch(
            "preloop.models.db.session.get_async_engine_if_initialized",
            return_value=async_engine,
        ),
        patch(
            "preloop.models.db.session._env_int",
            side_effect=lambda name, default: 12
            if name == "DATABASE_MAX_OVERFLOW"
            else default,
        ),
    )


class TestCollectPoolStats:
    def test_skips_uninitialized_engines(self):
        p1, p2, p3 = patch_engines(sync_engine=None, async_engine=None)
        with p1, p2, p3:
            assert collect_pool_stats() == []

    def test_reports_both_engines(self):
        sync_engine = make_engine(make_pool(size=8, checked_out=2))
        async_engine = make_engine(make_pool(size=8, checked_out=5, overflow=1))
        p1, p2, p3 = patch_engines(sync_engine, async_engine)
        with p1, p2, p3:
            stats = collect_pool_stats()

        assert [s["engine"] for s in stats] == ["sync", "async"]
        async_stats = stats[1]
        assert async_stats["checked_out"] == 5
        assert async_stats["overflow_in_use"] == 1
        # Ceiling is pool_size + max_overflow (12 from the patched env).
        assert async_stats["ceiling"] == 20

    def test_negative_overflow_clamped_to_zero(self):
        engine = make_engine(make_pool(size=8, checked_out=1, overflow=-7))
        p1, p2, p3 = patch_engines(sync_engine=engine)
        with p1, p2, p3:
            stats = collect_pool_stats()
        assert stats[0]["overflow_in_use"] == 0


class TestWarnThreshold:
    def test_warns_at_80_percent_of_ceiling(self, caplog):
        # Ceiling is 8 + 12 = 20; 16 checked out is exactly 80%.
        engine = make_engine(make_pool(size=8, checked_out=16, overflow=8))
        monitor = DbPoolMonitor(interval_seconds=30, warn_ratio=0.8)
        p1, p2, p3 = patch_engines(async_engine=engine)
        with p1, p2, p3, caplog.at_level(logging.WARNING):
            monitor.check_once()

        warnings = [
            r for r in caplog.records if "pool nearing exhaustion" in r.getMessage()
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "engine=async" in message
        assert "16/20" in message

    def test_quiet_below_threshold(self, caplog):
        engine = make_engine(make_pool(size=8, checked_out=10, overflow=2))
        monitor = DbPoolMonitor(interval_seconds=30, warn_ratio=0.8)
        p1, p2, p3 = patch_engines(async_engine=engine)
        with p1, p2, p3, caplog.at_level(logging.WARNING):
            monitor.check_once()

        assert not [
            r for r in caplog.records if "pool nearing exhaustion" in r.getMessage()
        ]


class TestEnableFlag:
    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("DB_MONITORING_ENABLED", raising=False)
        assert db_monitoring_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "OFF"])
    def test_disabled_values(self, monkeypatch, value):
        monkeypatch.setenv("DB_MONITORING_ENABLED", value)
        assert db_monitoring_enabled() is False

    def test_enabled_values(self, monkeypatch):
        monkeypatch.setenv("DB_MONITORING_ENABLED", "true")
        assert db_monitoring_enabled() is True


class TestOtelGauges:
    def test_registers_against_noop_meter(self):
        # With no meter provider configured the default no-op meter must
        # absorb the registration without errors.
        assert register_otel_pool_gauges() is True
