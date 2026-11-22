"""Content-pinned extension approvals (#2321) on synthetic PostgreSQL data."""

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models import models
from preloop.models.crud import crud_extension_approval
from preloop.models.crud.extension_approval import (
    MAX_PINS_PER_APPROVAL,
    approval_join,
    PinConflictError,
    PinStateError,
    effective_state,
    effective_state_sql,
)
from preloop.schemas.discovered_extension import (
    ExtensionApprovalUpdate,
    ExtensionMetadata,
    ExtensionPinCreate,
)
from tests.test_extension_inventory import (
    APPROVAL,
    EXTENSIONS,
    NOW,
    REPORTS,
    extension,
    report,
    store,
)

H1, H2, H3 = ("sha256:" + c * 64 for c in "123")
PINS = APPROVAL + "/pins"


def device_report(fingerprint: str, content_hash: str | None) -> dict[str, Any]:
    """One device reporting the reviewer plugin with a given content hash."""
    status = "ok" if content_hash else "too_large"
    payload = report([extension(content_hash=content_hash, content_hash_status=status)])
    payload["workstation_fingerprint"] = fingerprint
    return payload


def approve(db: Session, user: models.User, **pin: Any) -> models.ExtensionApproval:
    """Approve the reviewer plugin, optionally pinned, through the CRUD layer."""
    return crud_extension_approval.set_state(
        db,
        account_id=user.account_id,
        agent_kind="claude_code",
        extension_kind="plugin",
        extension_id="reviewer@community",
        decided_by_user_id=user.id,
        payload=ExtensionApprovalUpdate(
            approval_state="approved",
            pin=ExtensionPinCreate(**pin) if pin else None,
        ),
    )


TRUTH_TABLE = [
    # (approval_state or None, pins, row hash, expected)
    (None, [], H1, "unknown"),
    ("unapproved", [H1], H1, "unapproved"),
    ("approved", [], H2, "approved"),
    ("approved", [H1], H1, "approved"),
    ("approved", [H1], H2, "content_changed"),
    ("approved", [H1], None, "content_unverified"),
]


@pytest.mark.parametrize(("state", "pins", "row_hash", "expected"), TRUTH_TABLE)
def test_effective_state_truth_table(
    state: str | None, pins: list[str], row_hash: str | None, expected: str
) -> None:
    """protects=the issue's truth table, one row per case; fails_when=any row
    maps to another state; no_claim=SimpleNamespace stands in for ORM rows.
    """
    approval = None if state is None else SimpleNamespace(approval_state=state)
    row = SimpleNamespace(content_hash=row_hash)
    assert effective_state(row, approval, pins) == expected


@pytest.mark.parametrize(("state", "pins", "row_hash", "expected"), TRUTH_TABLE)
def test_effective_state_sql_matches_python(
    db_session: Session,
    test_user: models.User,
    state: str | None,
    pins: list[str],
    row_hash: str | None,
    expected: str,
) -> None:
    """protects=filters agree with the reported state; fails_when=the SQL
    mirror diverges from effective_state on a truth-table row; no_claim=one
    synthetic device per case.
    """
    store(db_session, test_user.account_id, device_report("a" * 64, row_hash))
    if state is not None:
        row = approve(db_session, test_user)
        row.approval_state = state
        for content_hash in pins:
            db_session.add(
                models.ExtensionApprovalPin(
                    approval_id=row.id, content_hash=content_hash, pinned_at=NOW
                )
            )
        db_session.flush()
    model, approval = models.DiscoveredExtension, models.ExtensionApproval
    value = db_session.scalar(
        select(effective_state_sql())
        .select_from(model)
        .outerjoin(approval, approval_join())
        .where(model.account_id == test_user.account_id)
    )
    assert value == expected


def test_pin_crud_conflicts_state_and_cascade(
    db_session: Session, test_user: models.User
) -> None:
    """protects=pin invariants; fails_when=duplicates, pins on a non-approved
    decision, or orphaned pins after the approval is deleted; no_claim=CRUD
    only, HTTP codes are covered below.
    """
    row = approve(db_session, test_user, content_hash=H1)
    with pytest.raises(PinConflictError):
        crud_extension_approval.add_pin(
            db_session,
            approval=row,
            pin=ExtensionPinCreate(content_hash=H1),
            pinned_by_user_id=test_user.id,
        )
    row.approval_state = "unapproved"
    with pytest.raises(PinStateError):
        crud_extension_approval.add_pin(
            db_session,
            approval=row,
            pin=ExtensionPinCreate(content_hash=H2),
            pinned_by_user_id=test_user.id,
        )
    db_session.delete(row)
    db_session.flush()
    db_session.expire_all()
    assert db_session.scalars(select(models.ExtensionApprovalPin)).all() == []


def test_pin_limit(db_session: Session, test_user: models.User) -> None:
    """protects=the 50-pin cap; fails_when=a 51st pin is stored; no_claim=the
    row lock is exercised serially, not under concurrent writers.
    """
    row = approve(db_session, test_user)
    for i in range(MAX_PINS_PER_APPROVAL):
        crud_extension_approval.add_pin(
            db_session,
            approval=row,
            pin=ExtensionPinCreate(content_hash=f"sha256:{i:064x}"),
            pinned_by_user_id=test_user.id,
        )
    with pytest.raises(PinConflictError):
        crud_extension_approval.add_pin(
            db_session,
            approval=row,
            pin=ExtensionPinCreate(content_hash=H1),
            pinned_by_user_id=test_user.id,
        )


def test_migration_pins_existing_approvals_to_observed_content(
    db_session: Session, test_user: models.User
) -> None:
    """protects=upgrade safety; fails_when=an existing name approval stays
    approved for content first seen after the upgrade; no_claim=the backfill
    SQL runs inside the test transaction, not through a full alembic run.
    """
    store(db_session, test_user.account_id, device_report("a" * 64, H1))
    row = approve(db_session, test_user)
    migration = importlib.import_module(
        "preloop.models.alembic.versions.20261226_ext_approval_pin"
    )
    db_session.execute(text(migration.BACKFILL_SQL))
    pins = crud_extension_approval.pins_for(db_session, approval_ids=[row.id])
    assert [p["content_hash"] for p in pins[row.id]] == [H1]
    assert pins[row.id][0]["pinned_by_user_id"] == test_user.id
    device = SimpleNamespace(content_hash=H2)
    assert effective_state(device, row, [H1]) == "content_changed"


def test_status_requires_null_hash() -> None:
    """protects=collector status contract; fails_when=a too_large record also
    carries a hash; no_claim=none beyond protects.
    """
    with pytest.raises(ValueError):
        ExtensionMetadata.model_validate(
            extension(content_hash=H1, content_hash_status="too_large")
        )


def test_http_pinned_states_and_repin(client: TestClient) -> None:
    """protects=acceptance 6: pins {h1}, devices h1/h2/null read approved,
    content_changed, content_unverified, and pinning h2 approves it on the next
    GET; fails_when=a changed hash stays approved or re-pinning does not clear
    it; no_claim=synthetic reports stand in for the collector.
    """
    for fingerprint, content_hash in (("a" * 64, H1), ("b" * 64, H2), ("c" * 64, None)):
        response = client.post(REPORTS, json=device_report(fingerprint, content_hash))
        assert response.status_code == 202, response.text
    response = client.put(
        APPROVAL, json={"approval_state": "approved", "pin": {"content_hash": H1}}
    )
    assert response.status_code == 200, response.text
    assert [p["content_hash"] for p in response.json()["pins"]] == [H1]

    def states() -> dict[str, str]:
        items = client.get(EXTENSIONS).json()["items"]
        return {item["fingerprint"][0]: item["effective_state"] for item in items}

    assert states() == {
        "a": "approved",
        "b": "content_changed",
        "c": "content_unverified",
    }
    changed = client.get(EXTENSIONS, params={"state": "content_changed"}).json()
    assert [item["fingerprint"][0] for item in changed["items"]] == ["b"]
    item = next(
        i for i in client.get(EXTENSIONS).json()["items"] if i["fingerprint"][0] == "c"
    )
    assert item["content_hash"] is None and item["content_hash_status"] == "too_large"

    grouped = client.get(EXTENSIONS, params={"group": "extension"}).json()["items"][0]
    assert grouped["pin_count"] == 1
    assert grouped["content_hashes"] == [
        {"content_hash": H1, "status": "ok", "device_count": 1, "pinned": True},
        {"content_hash": H2, "status": "ok", "device_count": 1, "pinned": False},
        {
            "content_hash": None,
            "status": "too_large",
            "device_count": 1,
            "pinned": False,
        },
    ]

    response = client.post(PINS, json={"content_hash": H2, "note": "Reviewed"})
    assert response.status_code == 201, response.text
    assert states()["b"] == "approved"

    listed = client.get("/api/v1/agents/extension-approvals").json()["items"][0]
    assert {p["content_hash"] for p in listed["pins"]} == {H1, H2}
    assert all(p["pinned_by"] for p in listed["pins"])

    # Deleting every pin returns to a name approval.
    for pin in listed["pins"]:
        assert client.delete(f"{PINS}/{pin['id']}").status_code == 204
    assert set(states().values()) == {"approved"}


def test_http_pin_errors(client: TestClient) -> None:
    """protects=pin endpoint contract; fails_when=missing approval, bad hash,
    non-approved approval, duplicate, or a foreign approval's pin id succeed;
    no_claim=none beyond protects.
    """
    assert client.post(PINS, json={"content_hash": H1}).status_code == 404
    assert (
        client.put(APPROVAL, json={"approval_state": "unapproved"}).status_code == 200
    )
    assert client.post(PINS, json={"content_hash": H1}).status_code == 422
    assert client.post(PINS, json={"content_hash": "sha256:XYZ"}).status_code == 422
    assert (
        client.put(
            APPROVAL, json={"approval_state": "unapproved", "pin": {"content_hash": H1}}
        ).status_code
        == 422
    )
    assert client.put(APPROVAL, json={"approval_state": "approved"}).status_code == 200
    pin = client.post(PINS, json={"content_hash": H1})
    assert pin.status_code == 201
    assert client.post(PINS, json={"content_hash": H1}).status_code == 409
    other = "/api/v1/agents/extension-approvals/claude_code/skill/other"
    assert client.put(other, json={"approval_state": "approved"}).status_code == 200
    assert client.delete(f"{other}/pins/{pin.json()['id']}").status_code == 404
    assert client.delete(f"{PINS}/{pin.json()['id']}").status_code == 204
    assert client.delete(f"{PINS}/{pin.json()['id']}").status_code == 404


def test_http_pin_requires_manage(
    client: TestClient,
    app: FastAPI,
    test_viewer_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """protects=manage_agents on pin writes; fails_when=a viewer pins or
    unpins; no_claim=synthetic permission provider stands in for EE RBAC.
    """
    monkeypatch.setattr(
        "preloop.utils.permissions.members_have_full_access", lambda: False
    )
    monkeypatch.setattr(
        "preloop.utils.permissions.user_holds_permission",
        lambda db, user, permission: permission == "view_agents",
    )
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
    assert client.post(PINS, json={"content_hash": H1}).status_code == 403
    pin_id = "00000000-0000-0000-0000-000000000001"
    assert client.delete(f"{PINS}/{pin_id}").status_code == 403
