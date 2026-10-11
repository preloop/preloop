"""Harness inventory published by personal runners (#1480, contract A).

A runner reports which agent harnesses are installed on its host, their
login state, governance and models. This module normalizes what arrives on
the wire (caps sizes so one oversized report cannot fail a heartbeat),
stores it through the CRUD layer, audits changes, and redacts host details
for viewers who are neither the runner owner nor an account admin.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_audit_log
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.schemas.flow_runner import (
    HARNESS_IDS,
    MAX_HARNESS_INVENTORY_ENTRIES,
    MAX_HARNESS_MODEL_ID_LENGTH,
    MAX_HARNESS_MODELS,
    HarnessInventory,
)

logger = logging.getLogger(__name__)

HARNESS_INVENTORY_CHANGED_ACTION = "runner.harness_inventory_changed"
#: Entry fields only the runner owner and account admins see.
OWNER_ONLY_ENTRY_FIELDS = ("version", "account_host")
_MAX_CAPABILITIES = 16
_MAX_DISPLAY_NAME = 128
_MAX_VERSION = 64


def _cap_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    capped = dict(entry)
    models_raw = capped.get("models")
    if isinstance(models_raw, list):
        capped["models"] = [
            model
            for model in models_raw
            if isinstance(model, dict)
            and isinstance(model.get("id"), str)
            and 0 < len(model["id"]) <= MAX_HARNESS_MODEL_ID_LENGTH
        ][:MAX_HARNESS_MODELS]
    capabilities = capped.get("capabilities")
    if isinstance(capabilities, list):
        capped["capabilities"] = [
            value for value in capabilities if isinstance(value, str) and value
        ][:_MAX_CAPABILITIES]
    name = capped.get("display_name")
    if isinstance(name, str):
        capped["display_name"] = name[:_MAX_DISPLAY_NAME]
    version = capped.get("version")
    if isinstance(version, str) and len(version) > _MAX_VERSION:
        capped.pop("version")
    return capped


def normalize_harness_inventory(raw: Any) -> Optional[Dict[str, Any]]:
    """Return the wire form of a valid inventory, capped, or None.

    Unknown harness ids are dropped, lists are truncated to the contract
    limits, and anything still invalid makes the whole report ignored
    (logged) instead of failing the runner's heartbeat or registration.
    """
    if isinstance(raw, HarnessInventory):
        raw = raw.to_wire()
    if not isinstance(raw, dict):
        return None
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return None
    known = [
        _cap_entry(entry)
        for entry in entries
        if isinstance(entry, dict) and entry.get("harness") in HARNESS_IDS
    ][:MAX_HARNESS_INVENTORY_ENTRIES]
    try:
        inventory = HarnessInventory.model_validate({**raw, "entries": known})
    except ValidationError as exc:
        logger.info(
            "Ignoring invalid harness inventory: %s", exc.errors(include_input=False)
        )
        return None
    return inventory.to_wire()


def redact_harness_inventory(
    inventory: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Copy of the inventory without version and account host per entry."""
    if not isinstance(inventory, dict):
        return inventory
    entries: List[Dict[str, Any]] = []
    for entry in inventory.get("entries") or []:
        if isinstance(entry, dict):
            entries.append(
                {k: v for k, v in entry.items() if k not in OWNER_ONLY_ENTRY_FIELDS}
            )
    return {**inventory, "entries": entries}


def viewer_is_account_admin(db: Session, viewer: Optional[models.User]) -> bool:
    """Superuser, the account's primary user, or a manage_account holder.

    Costs up to two queries; resolve it once per request and pass it to
    ``inventory_for_viewer`` when rendering many runners.
    """
    if viewer is None:
        return False
    if getattr(viewer, "is_superuser", False):
        return True
    account = crud_account.get(db, id=viewer.account_id)
    if account is not None and str(account.primary_user_id) == str(viewer.id):
        return True
    from preloop.utils.permissions import user_holds_permission

    return user_holds_permission(db, viewer, "manage_account")


def viewer_sees_full_inventory(
    db: Session,
    runner: models.FlowRunner,
    viewer: Optional[models.User],
    *,
    viewer_is_admin: Optional[bool] = None,
) -> bool:
    """Runner owner, or an admin of the runner's account."""
    if viewer is None:
        return False
    if getattr(viewer, "is_superuser", False):
        return True
    owner_id = getattr(runner, "registered_by_user_id", None)
    if owner_id is not None and str(owner_id) == str(viewer.id):
        return True
    if str(getattr(runner, "account_id", "")) != str(viewer.account_id):
        return False
    if viewer_is_admin is None:
        viewer_is_admin = viewer_is_account_admin(db, viewer)
    return viewer_is_admin


def inventory_for_viewer(
    db: Session,
    runner: models.FlowRunner,
    viewer: Optional[models.User],
    *,
    viewer_is_admin: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """The stored inventory as this viewer may see it."""
    inventory = getattr(runner, "harness_inventory", None)
    if inventory is None or viewer_sees_full_inventory(
        db, runner, viewer, viewer_is_admin=viewer_is_admin
    ):
        return inventory
    return redact_harness_inventory(inventory)


def inventory_hash_known(runner: models.FlowRunner, reported_hash: Any) -> bool:
    """Whether the stored inventory is the one the runner says it has."""
    stored = getattr(runner, "harness_inventory", None)
    return (
        isinstance(stored, dict)
        and isinstance(reported_hash, str)
        and stored.get("hash") == reported_hash
    )


def record_harness_inventory(
    db: Session, runner: models.FlowRunner, raw: Any, *, commit: bool = True
) -> bool:
    """Store a reported inventory; audit and return True when it changed."""
    inventory = normalize_harness_inventory(raw)
    if inventory is None:
        return False
    previous_hash = crud_flow_runner.set_harness_inventory(
        db, runner=runner, inventory=inventory, commit=commit
    )
    if previous_hash == inventory.get("hash"):
        return False
    crud_audit_log.log_action(
        db,
        account_id=runner.account_id,
        user_id=runner.registered_by_user_id,
        action=HARNESS_INVENTORY_CHANGED_ACTION,
        resource_type="flow_runner",
        resource_id=str(runner.id),
        status="success",
        details={
            "runner_id": str(runner.id),
            "runner_name": runner.name,
            "host": runner.hostname,
            "previous_hash": previous_hash,
            "hash": inventory.get("hash"),
            "harnesses": [
                {
                    "harness": entry.get("harness"),
                    "login_state": entry.get("login_state"),
                    "governance": entry.get("governance"),
                    "enabled": entry.get("enabled"),
                }
                for entry in inventory.get("entries", [])
            ],
        },
        commit=commit,
    )
    return True
