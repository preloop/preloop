"""The Skyvern importer writes through the real step and deposit routes.

Skyvern is replayed from the package's recorded API v1 fixture; Preloop
is the FastAPI test client, so dedupe and artifact validation are the
production code paths.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from preloop.models import models
from preloop.models.crud import crud_api_key, crud_runtime_session

PLUGIN = Path(__file__).resolve().parents[3] / "runtime-plugins" / "skyvern-preloop"
sys.path.insert(0, str(PLUGIN / "src"))
sys.path.insert(0, str(PLUGIN / "tests"))

from preloop_skyvern import (  # noqa: E402
    PreloopClient,
    PreloopTarget,
    SkyvernClient,
    import_task,
)
from skyvern_fakes import SKYVERN, RecordedSkyvern, recorded  # noqa: E402

TASK = recorded()["task"]["task_id"]


def _session_and_key(db_session, test_user):
    started = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id="skyvern-import",
        session_reference="skyvern-import",
        runtime_principal_type="agent",
        runtime_principal_id="skyvern",
        runtime_principal_name="Skyvern",
        started_at=started,
        last_activity_at=started,
    )
    _key, token = crud_api_key.create_runtime_key(
        db_session,
        name="Skyvern importer",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"runtime_session_id": str(session.id)},
    )
    return session, token


def _import(client, session, token):
    skyvern = SkyvernClient(
        "sk-skyvern-test",
        base_url=SKYVERN,
        client=httpx.Client(transport=httpx.MockTransport(RecordedSkyvern())),
    )
    preloop = PreloopClient(
        PreloopTarget(str(client.base_url), token, str(session.id)), client=client
    )
    return import_task(TASK, skyvern=skyvern, preloop=preloop)


def _rows(db_session, session_id, model, **filters):
    query = db_session.query(model).filter(model.runtime_session_id == session_id)
    for name, value in filters.items():
        query = query.filter(getattr(model, name) == value)
    return query.all()


def test_import_stores_steps_screenshots_and_files(client, db_session, test_user):
    session, token = _session_and_key(db_session, test_user)

    report = _import(client, session, token)

    assert report.steps.accepted == 6 and not report.steps.rejected
    assert report.skipped == []
    steps = _rows(
        db_session,
        session.id,
        models.RuntimeSessionActivity,
        activity_type="browser_step",
    )
    assert len(steps) == 6
    assert {s.metadata_["source"] for s in steps} == {"skyvern"}
    artifacts = _rows(db_session, session.id, models.RuntimeSessionArtifact)
    by_kind = sorted(a.kind for a in artifacts)
    assert by_kind == ["recording"] + ["screenshot"] * 6 + ["trace"] * 2
    trace_types = {a.content_type for a in artifacts if a.kind == "trace"}
    assert trace_types == {"application/zip"}


def test_reimport_with_the_same_source_ref_creates_no_duplicates(
    client, db_session, test_user
):
    session, token = _session_and_key(db_session, test_user)
    first = _import(client, session, token)

    again = _import(client, session, token)

    assert again.steps.accepted == 0 and again.steps.duplicates == 6
    assert sorted(a["id"] for a in again.artifacts) == sorted(
        a["id"] for a in first.artifacts
    )
    steps = _rows(
        db_session,
        session.id,
        models.RuntimeSessionActivity,
        activity_type="browser_step",
    )
    assert len(steps) == 6
    assert len(_rows(db_session, session.id, models.RuntimeSessionArtifact)) == 9
