"""The Browser Use adapter posts through the real browser step route.

Replays the recorded 12-step Browser Use history from the adapter package
with the FastAPI test client as its HTTP transport, so conversion,
the batch route, screenshot storage and timeline ordering are exercised
together.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from preloop.models import models
from preloop.models.crud import (
    crud_api_key,
    crud_runtime_session,
    crud_runtime_session_activity,
)

PLUGIN = Path(__file__).resolve().parents[3] / "runtime-plugins" / "browser-use-preloop"
sys.path.insert(0, str(PLUGIN / "src"))

from preloop_browser_use import PreloopBrowserUseReporter, PreloopTarget  # noqa: E402

FIXTURE = PLUGIN / "tests" / "fixtures" / "browser_use_history_12.json"


class _ReplayAgent:
    def __init__(self, items):
        self.id = "bu-run-1"
        self._items = items
        self.history = SimpleNamespace(history=[])

    async def run(self, on_step_end=None):
        for item in self._items:
            self.history.history.append(item)
            await on_step_end(self)
        return self.history


def _setup(db_session, test_user):
    started = datetime(2026, 9, 21, 14, 13, tzinfo=timezone.utc)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id="browser-use-replay",
        session_reference="browser-use-replay",
        runtime_principal_type="agent",
        runtime_principal_id="browser-use",
        runtime_principal_name="Browser Use",
        started_at=started,
        last_activity_at=started,
    )
    _key, token = crud_api_key.create_runtime_key(
        db_session,
        name="Browser Use",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"runtime_session_id": str(session.id)},
    )
    return session, token, started


def _replay(client, session, token):
    target = PreloopTarget(str(client.base_url), token, str(session.id))
    reporter = PreloopBrowserUseReporter(target, batch_size=5, client=client)
    items = json.loads(FIXTURE.read_text())["history"]
    asyncio.run(reporter.run(_ReplayAgent(items)))
    return reporter


def test_twelve_steps_land_interleaved_with_model_turns(client, db_session, test_user):
    session, token, started = _setup(db_session, test_user)
    # Recorded steps end at +22.5s, +25.5s, ... +55.5s.
    for offset in (1, 24, 40):
        crud_runtime_session_activity.log_model_gateway_call(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            status="success",
            summary=f"model turn at +{offset}s",
            timestamp=started + timedelta(seconds=offset),
            commit=False,
        )
    db_session.commit()

    reporter = _replay(client, session, token)

    assert sum(r.accepted for r in reporter.results) == 12
    assert all(not r.rejected for r in reporter.results)
    rows = (
        db_session.query(models.RuntimeSessionActivity)
        .filter(models.RuntimeSessionActivity.runtime_session_id == session.id)
        .order_by(models.RuntimeSessionActivity.timestamp.asc())
        .all()
    )
    kinds = [r.activity_type for r in rows if r.activity_type != "artifact"]
    model, step = "model_gateway_call", "browser_step"
    assert kinds == [model, step, model] + [step] * 5 + [model] + [step] * 6
    steps = [r for r in rows if r.activity_type == "browser_step"]
    assert all(r.metadata_["source"] == "browser_use" for r in steps)
    assert all(r.metadata_["screenshot"]["availability"] == "available" for r in steps)
    shots = (
        db_session.query(models.RuntimeSessionArtifact)
        .filter(
            models.RuntimeSessionArtifact.runtime_session_id == session.id,
            models.RuntimeSessionArtifact.kind == "screenshot",
        )
        .count()
    )
    assert shots == 12


def test_replaying_the_same_run_is_all_duplicates(client, db_session, test_user):
    session, token, _ = _setup(db_session, test_user)
    _replay(client, session, token)

    again = _replay(client, session, token)

    assert sum(r.accepted for r in again.results) == 0
    assert sum(r.duplicates for r in again.results) == 12
