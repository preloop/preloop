"""Observe terminal metadata through the authenticated runner heartbeat."""

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
    if not report["observe_enabled"]:
        clear_device_locations(db, runner=runner)
        report["agents"] = []
    elif report["protocol"] != 22 or report.get("status"):
        report["status"] = "herdr_protocol_unsupported"
        report["agents"] = old.get("agents", []) if old else []
    else:
        report = reconcile(db, runner=runner, report=report)
    base = previous or {
        "schema": 1,
        "generated_at": report["observed_at"],
        "hash": "sha256:" + "0" * 64,
        "entries": [],
    }
    crud_flow_runner.set_harness_inventory(
        db, runner=runner, inventory=base | {"herdr": report}
    )
    return old != report
