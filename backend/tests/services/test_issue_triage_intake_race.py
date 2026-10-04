"""Triage reservation when the flow event beats the webhook commit (#1195).

The GitHub App publishes a flow event before the core webhook handler has
committed the new issue row, and the same delivery reaches the worker twice.
Both triggers must converge on one issue row and one triage reservation.
"""

import asyncio
import threading
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_issue, crud_issue_lifecycle, crud_project
from preloop.services import issue_triage_controller as controller
from tests.services.test_issue_triage_controller_review import _committed_rig

SUBJECT = {
    "id": 9000042,
    "number": 42,
    "title": "Export button does nothing",
    "body": "Clicking export shows no dialog.",
    "state": "open",
    "created_at": "2026-10-03T10:00:00Z",
    "updated_at": "2026-10-03T10:00:00Z",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("webhook", ["uncommitted", "absent"])
async def test_racing_triggers_reserve_one_execution_for_unsynced_issue(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch, webhook: str
) -> None:
    with _committed_rig(db_engine, monkeypatch) as rig, Session(db_engine) as second:
        crud_project.update(
            rig.db, db_obj=rig.project, obj_in={"slug": "example/project"}
        )
        writer = Session(db_engine)
        timer = None
        try:
            if webhook == "uncommitted":
                # The webhook handler's transaction: row written, not committed.
                crud_issue.create(
                    writer,
                    obj_in={
                        "title": SUBJECT["title"],
                        "external_id": str(SUBJECT["id"]),
                        "key": "example/project#42",
                        "project_id": rig.project.id,
                        "tracker_id": rig.issue.tracker_id,
                    },
                    commit=False,
                )
                timer = threading.Timer(0.3, writer.commit)
                timer.start()
            event = {
                "type": "issue_opened",
                "project_id": str(rig.project.id),
                "payload": {"action": "opened", "issue": dict(SUBJECT)},
            }
            first, reused = await asyncio.wait_for(
                controller.reserve_triage_execution(rig.db, flow=rig.flow, event=event),
                10,
            )
            other_flow = crud_issue_lifecycle.triage_flow(
                second, account_id=rig.account_id, flow_id=rig.flow.id
            )
            duplicate, repeated = await asyncio.wait_for(
                controller.reserve_triage_execution(
                    second, flow=other_flow, event=event
                ),
                10,
            )
        finally:
            if timer is not None:
                timer.join()
            writer.close()

        assert not reused and repeated and duplicate.id == first.id
        issues = rig.db.scalars(
            select(models.Issue).where(
                models.Issue.project_id == rig.project.id,
                models.Issue.external_id == str(SUBJECT["id"]),
            )
        ).all()
        assert len(issues) == 1
        triage = [
            row
            for row in crud_issue_lifecycle.list_for_issue(
                rig.db, account_id=rig.account_id, issue_id=issues[0].id
            )
            if row.kind == "triage"
        ]
        assert len(triage) == 1
