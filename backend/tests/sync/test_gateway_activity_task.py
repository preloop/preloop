"""Task wiring for gateway activity maintenance."""

from unittest.mock import MagicMock

from preloop.sync import tasks


def test_task_is_dispatchable():
    assert "maintain_gateway_activity" in tasks.DISPATCHABLE_TASKS


def test_task_closes_the_session_and_swallows_errors(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([db]))

    def boom(session):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "preloop.services.gateway_activity_maintenance."
        "run_gateway_activity_maintenance",
        boom,
    )
    assert tasks.maintain_gateway_activity() is None
    db.close.assert_called_once()
