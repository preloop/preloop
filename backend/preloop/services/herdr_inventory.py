"""Observe terminal metadata through the authenticated runner heartbeat."""

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.crud.terminal_location import (
    _instant,
    clear_device_locations,
    reconcile,
)
from preloop.models.schemas.terminal_location import HerdrInventory


logger = logging.getLogger(__name__)


def _emit_changed(runner: models.FlowRunner, affected: set[str]) -> None:
    """Invalidate only changed sessions after the report transaction commits."""
    from preloop.services.account_realtime import (
        ACCOUNT_TOPIC_RUNTIME_SESSIONS,
        build_account_event,
        emit_account_event,
    )

    for session_id in sorted(affected):
        try:
            emit_account_event(
                build_account_event(
                    account_id=str(runner.account_id),
                    topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                    event_type="runtime_session_updated",
                    runtime_session_id=session_id,
                    payload={
                        "runtime_session_id": session_id,
                        "terminal_location_changed": True,
                    },
                )
            )
        except Exception:  # live delivery must never interrupt the runner
            logger.debug("Terminal location live event failed", exc_info=True)


def record_herdr_inventory(db: Session, runner: models.FlowRunner, raw: Any) -> bool:
    """Store switch state and supported metadata; never infer closure from errors."""
    try:
        report = HerdrInventory.model_validate(raw).model_dump(mode="json")
    except ValidationError:
        return False
    previous = runner.harness_inventory or {}
    old = previous.get("herdr")
    if old and _instant(old["observed_at"]) > _instant(report["observed_at"]):
        return False
    affected: set[str] = set()
    if not report["observe_enabled"]:
        affected = clear_device_locations(db, runner=runner)
        report["agents"] = []
    elif report["protocol"] != 22 or report.get("status"):
        report["status"] = "herdr_protocol_unsupported"
        report["agents"] = old.get("agents", []) if old else []
    else:
        report = reconcile(db, runner=runner, report=report, affected=affected)
    base = previous or {
        "schema": 1,
        "generated_at": report["observed_at"],
        "hash": "sha256:" + "0" * 64,
        "entries": [],
    }
    crud_flow_runner.set_harness_inventory(
        db, runner=runner, inventory=base | {"herdr": report}
    )
    _emit_changed(runner, affected)
    return old != report


def reconcile_cached_herdr_inventory(db: Session, runner: models.FlowRunner) -> bool:
    """Join late-arriving governed sessions without refreshing observation time.

    Normal heartbeats retry only unmanaged identities in the latest successful
    snapshot. Cached metadata never closes panes, updates already joined rows,
    or adds a commit when nothing became managed.
    """
    inventory = getattr(runner, "harness_inventory", None) or {}
    report = inventory.get("herdr")
    if (
        not report
        or not report.get("observe_enabled")
        or report.get("protocol") != 22
        or report.get("status")
        or not runner.workstation_fingerprint
        or not any(not pane.get("managed") for pane in report.get("agents", []))
    ):
        return False
    affected: set[str] = set()
    updated = reconcile(
        db, runner=runner, report=report, cached=True, affected=affected
    )
    if updated == report:
        return False
    crud_flow_runner.set_harness_inventory(
        db, runner=runner, inventory=inventory | {"herdr": updated}
    )
    _emit_changed(runner, affected)
    return True
