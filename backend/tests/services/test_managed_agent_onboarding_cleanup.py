"""Onboarding cleanup helpers, the expiry task and its schedule."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.config import Settings
from preloop.services import managed_agent_onboarding_cleanup as cleanup
from preloop.sync import tasks
from preloop.sync.cli import scheduler_commands

LEAKED = "agt_" + "Z9y8X7w6V5u4T3s2R1q0P9n8M7k6J5h4G3f2E1d0C9b"


def test_created_resources_parse_keeps_only_well_formed_ids() -> None:
    first, second = str(uuid4()), str(uuid4())

    parsed = cleanup.parse_created_resources(
        {
            "agent_created": True,
            "credential_ids": [first, "not-a-uuid", 7, first],
            "ai_model_ids": [second.upper()],
            "mcp_server_ids": ["anything"],
            "unknown": "ignored",
        }
    )

    assert parsed.agent_created is True
    assert parsed.credential_ids == (first,)
    assert parsed.ai_model_ids == (second,)


@pytest.mark.parametrize("raw", [None, {}, "text", {"agent_created": "yes"}])
def test_created_resources_parse_defaults_to_nothing(raw: object) -> None:
    parsed = cleanup.parse_created_resources(raw)

    assert parsed.agent_created is False
    assert parsed.credential_ids == ()
    assert parsed.ai_model_ids == ()


def test_redaction_replaces_agent_keys_in_keys_and_values() -> None:
    redacted = cleanup.redact_agent_keys(
        {
            "env": {"KEY": LEAKED},
            "list": [f"Bearer {LEAKED}", 3, None, True],
            LEAKED: "as a key",
            "kept": "agt_short",
        }
    )

    assert LEAKED not in str(redacted)
    assert redacted["env"]["KEY"] == "<AGENT_KEY>"
    assert redacted["list"] == ["Bearer <AGENT_KEY>", 3, None, True]
    assert redacted["<AGENT_KEY>"] == "as a key"
    assert redacted["kept"] == "agt_short"


def test_pending_expiry_is_created_at_plus_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cleanup.settings, "preloop_pending_enrollment_ttl_hours", 5)
    created = datetime(2026, 1, 2, 3, 4, 5)

    assert cleanup.pending_enrollment_expires_at(
        "pending_local_config", created
    ) == created + timedelta(hours=5)
    assert cleanup.pending_enrollment_expires_at("validated", created) is None
    assert cleanup.pending_enrollment_expires_at("expired", created) is None


def test_ttl_setting_defaults_to_72_hours_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRELOOP_PENDING_ENROLLMENT_TTL_HOURS", raising=False)
    assert Settings.from_env().preloop_pending_enrollment_ttl_hours == 72

    monkeypatch.setenv("PRELOOP_PENDING_ENROLLMENT_TTL_HOURS", "12")
    assert Settings.from_env().preloop_pending_enrollment_ttl_hours == 12


def test_expiry_task_is_dispatchable_and_runs_one_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = MagicMock()
    seen: list[object] = []
    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([db]))
    monkeypatch.setattr(
        cleanup,
        "run_pending_enrollment_expiry_once",
        lambda session, now=None: seen.append(session) or 3,
    )

    assert "expire_pending_enrollments" in tasks.DISPATCHABLE_TASKS
    assert asyncio.run(tasks.expire_pending_enrollments()) == 3
    assert seen == [db]
    db.close.assert_called_once()


def test_expiry_task_is_scheduled_hourly() -> None:
    source = inspect.getsource(scheduler_commands)

    assert '("expire_pending_enrollments", "pending_enrollment_expiry")' in source


def test_expiry_pass_with_nothing_stale_touches_nothing(
    db_session: object,
) -> None:
    far_past = datetime.now(UTC) - timedelta(days=3650)

    assert cleanup.run_pending_enrollment_expiry_once(db_session, now=far_past) == 0
