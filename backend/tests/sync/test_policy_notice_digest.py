"""The Monday digest's "Policy notices" section (#959).

The digest itself lives in the ``optimization_digest`` plugin. These tests
stand in a fake plugin service that renders the section the way the plugin
does, and check the OSS shim still no-ops when no plugin is installed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from sqlalchemy.orm import Session

from preloop.models.models.user import User
from preloop.plugins.base import get_plugin_manager
from preloop.services.policy_notices import (
    PolicyNotice,
    build_policy_notice_digest_section,
    record_policy_notice,
)
from preloop.sync import tasks


def _record(db: Session, user: User, *, excerpt: str, at: datetime) -> None:
    record_policy_notice(
        db,
        PolicyNotice(
            account_id=user.account_id,
            user_id=user.id,
            target="model.request",
            rule_id="notify-codename",
            rule_description="Mentions of the codename",
            text_sha256="c" * 64,
            excerpt=excerpt,
        ),
        now=at,
        deliver=lambda _db, _hit: {},
    )


def test_fake_digest_plugin_renders_policy_notices(
    db_session: Session, test_user: User, monkeypatch
) -> None:
    now = datetime.now(timezone.utc)
    _record(db_session, test_user, excerpt="older mention", at=now - timedelta(days=2))
    _record(
        db_session, test_user, excerpt="newest mention", at=now - timedelta(hours=1)
    )

    rendered: dict[str, Any] = {}

    def fake_digest(db: Session, account_id: Any = None) -> str:
        section = build_policy_notice_digest_section(db, test_user.account_id)
        rendered["text"] = section.render_text()
        return "sent"

    manager = get_plugin_manager()
    monkeypatch.setitem(manager._services, "optimization_digest", fake_digest)
    monkeypatch.setattr(db_session, "close", lambda: None)

    with patch.object(tasks, "get_db_session", return_value=iter([db_session])):
        assert tasks.send_optimization_digest() == "sent"

    text = rendered["text"]
    assert text.startswith("Policy notices")
    assert "notify-codename: 2 hits" in text
    assert "Last user: testuser" in text
    assert "Last excerpt: newest mention" in text


def test_digest_shim_noops_without_plugin(monkeypatch) -> None:
    manager = get_plugin_manager()
    monkeypatch.delitem(manager._services, "optimization_digest", raising=False)
    with patch.object(tasks, "get_db_session") as get_db:
        assert tasks.send_optimization_digest() is None
    get_db.assert_not_called()
