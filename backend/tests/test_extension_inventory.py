"""Synthetic PostgreSQL and HTTP contracts for installed extension inventory."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models import models
from preloop.models.crud import (
    crud_discovered_extension,
    crud_discovery_observation,
    crud_extension_approval,
    crud_account,
)
from preloop.schemas.agent_discovery import DiscoveryReportRequest
from preloop.schemas.discovery_evidence import DiscoveryEvidence
from preloop.schemas.discovered_extension import ExtensionApprovalUpdate

NOW = datetime(2026, 11, 16, tzinfo=UTC)
REPORTS = "/api/v1/agents/discovery-reports"
EXTENSIONS = "/api/v1/agents/discovery-extensions"
APPROVAL = "/api/v1/agents/extension-approvals/claude_code/plugin/reviewer@community"


def extension(**changes: Any) -> dict[str, Any]:
    """Build synthetic metadata without local contents or paths."""
    return {
        "kind": "plugin",
        "id": "reviewer@community",
        "version": "1.4.0",
        "source_kind": "third_party_marketplace",
        "scope": "user",
        "enabled": "unknown",
        **changes,
    }


def report(
    extensions: list[dict[str, Any]] | None = None,
    *,
    completeness: str = "complete",
    profile_ref: str | None = None,
    at: datetime = NOW,
) -> dict[str, Any]:
    """Bind candidate extensions to one explicit observation context."""
    extensions = [extension()] if extensions is None else extensions
    app = {
        "agent_kind": "claude_code",
        "config_path_hash": "b" * 64,
        "extensions": extensions,
        "completeness": completeness,
    }
    evidence = {
        "schema_version": 3,
        "observation_id": str(uuid4()),
        "started_at": at.isoformat(),
        "ended_at": at.isoformat(),
        "scan_scope": ["user_config"],
        "completeness": completeness,
        "applications": [app],
    }
    if profile_ref:
        app["profile_ref"] = profile_ref
        evidence["profiles"] = [
            {"profile_ref": profile_ref, "completeness": "complete"}
        ]
    return {
        "workstation_fingerprint": "a" * 64,
        "candidates": [
            {
                "agent_kind": "claude_code",
                "config_path_hash": "b" * 64,
                "extensions": extensions,
            }
        ],
        "evidence": evidence,
    }


def store(
    db: Session, account_id: Any, payload: dict[str, Any], *, now: datetime = NOW
) -> None:
    """Exercise the real CRUD transaction with synthetic source identity."""
    request = DiscoveryReportRequest.model_validate(payload)
    observation = crud_discovery_observation.record(
        db,
        account_id=account_id,
        workstation_fingerprint=request.workstation_fingerprint,
        source_ref=account_id,
        evidence=request.evidence.model_dump(mode="json"),
        now=now,
    )
    crud_discovered_extension.upsert_from_report(
        db,
        account_id=account_id,
        workstation_fingerprint=request.workstation_fingerprint,
        evidence=request.evidence,
        observation_id=observation.id,
        now=now,
    )


def test_upsert_reconciliation_and_profile_isolation(
    db_session: Session, test_user: models.User
) -> None:
    """protects=presence history; fails_when=partial/replay duplicates or deletes;
    no_claim=synthetic reports, not collector filesystem coverage.
    """
    db, account = db_session, test_user.account_id
    payload = report()
    store(db, account, payload)
    store(db, account, payload, now=NOW + timedelta(minutes=1))
    rows, _ = crud_discovered_extension.list_for_device(
        db, account_id=account, fingerprint="a" * 64
    )
    assert len(rows) == 1 and rows[0]["approval_state"] == "unknown"
    assert rows[0]["first_seen_at"] != rows[0]["last_seen_at"]
    store(
        db, account, report([], completeness="partial", at=NOW + timedelta(minutes=2))
    )
    assert (
        len(crud_discovered_extension.list_for_account(db, account_id=account)[0]) == 1
    )
    store(db, account, report(profile_ref="c" * 64))
    store(db, account, report([], at=NOW + timedelta(minutes=3)))
    present, _ = crud_discovered_extension.list_for_account(db, account_id=account)
    assert len(present) == 1 and present[0]["profile_ref"] == "c" * 64
    absent, _ = crud_discovered_extension.list_for_account(
        db, account_id=account, present=False
    )
    assert len(absent) == 1 and absent[0]["profile_ref"] == "current"
    store(db, account, payload, now=NOW + timedelta(minutes=4))
    assert (
        len(crud_discovered_extension.list_for_account(db, account_id=account)[0]) == 1
    )


def test_null_project_identity_constraint(
    db_session: Session, test_user: models.User
) -> None:
    """protects=NULL-safe identity; fails_when=database permits duplicates;
    no_claim=real PostgreSQL constraint, no distributed load test.
    """
    store(db_session, test_user.account_id, report())
    row = db_session.scalar(select(models.DiscoveredExtension))
    values = {
        column.name: getattr(row, column.name)
        for column in row.__table__.columns
        if column.name not in {"id", "created_at", "updated_at"}
    }
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(models.DiscoveredExtension(**values))
        db_session.flush()


def test_approval_transitions_and_retention(
    db_session: Session, test_user: models.User
) -> None:
    """protects=durable tenant approval; fails_when=purge deletes decisions or
    another tenant's inventory; no_claim=clock is synthetic.
    """
    db, account = db_session, test_user.account_id
    other = crud_account.create(db, obj_in={"organization_name": "Example account"})
    store(db, account, report())
    store(db, other.id, report(), now=NOW + timedelta(days=50))
    ids = set()
    for state in ("approved", "unapproved", "unknown"):
        decision = crud_extension_approval.set_state(
            db,
            account_id=account,
            agent_kind="claude_code",
            extension_kind="plugin",
            extension_id="reviewer@community",
            decided_by_user_id=test_user.id,
            payload=ExtensionApprovalUpdate(approval_state=state),
        )
        ids.add(decision.id)
        rows, _ = crud_discovered_extension.list_for_account(
            db, account_id=account, state=state
        )
        assert len(rows) == 1 and rows[0]["approval_state"] == state
    assert len(ids) == 1
    crud_discovery_observation.purge_expired(
        db, account_id=account, now=NOW + timedelta(days=91)
    )
    assert crud_discovered_extension.list_for_account(db, account_id=account)[0] == []
    assert len(crud_extension_approval.list(db, account_id=account)[0]) == 1
    assert (
        len(crud_discovered_extension.list_for_account(db, account_id=other.id)[0]) == 1
    )


def test_http_ingest_approval_and_aggregate(client: TestClient) -> None:
    """protects=HTTP ingest and decision joins; fails_when=upserts or PUT are
    invisible to GET; no_claim=local app fixture, no browser.
    """
    payload = report()
    response = client.post(REPORTS, json=payload)
    assert response.status_code == 202, response.text
    assert client.post(REPORTS, json=payload).json()["created"] == 0
    page = client.get(EXTENSIONS).json()
    assert page["items"][0]["approval_state"] == "unknown"
    response = client.put(
        APPROVAL, json={"approval_state": "approved", "version_constraint": "1.4.*"}
    )
    assert response.status_code == 200, response.text
    assert client.get(EXTENSIONS).json()["items"][0]["approval_state"] == "approved"
    grouped = client.get(
        EXTENSIONS,
        params={
            "group": "extension",
            "source_kind": "third_party_marketplace",
            "state": "approved",
        },
    ).json()["items"]
    assert grouped == [
        {
            "agent_kind": "claude_code",
            "extension_kind": "plugin",
            "extension_id": "reviewer@community",
            "device_count": 1,
            "versions": ["1.4.0"],
            "source_kinds": ["third_party_marketplace"],
            "approval_state": "approved",
        }
    ]
    assert (
        client.get(EXTENSIONS, params={"source_kind": "builtin"}).json()["items"] == []
    )
    assert len(client.get("/api/v1/agents/extension-approvals").json()["items"]) == 1


def test_http_permissions(
    client: TestClient, test_viewer_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=manage permission; fails_when=view-only users decide approvals;
    no_claim=synthetic viewer and permission provider stand in for EE RBAC.
    """
    # Core accounts have no roles. Exercise the shared fallback with the same
    # permission boundary an installed role provider supplies.
    monkeypatch.setattr(
        "preloop.utils.permissions.members_have_full_access", lambda: False
    )
    monkeypatch.setattr(
        "preloop.utils.permissions.user_holds_permission",
        lambda db, user, permission: permission == "view_agents",
    )
    client.app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
    assert client.get(EXTENSIONS).status_code == 200
    assert client.put(APPROVAL, json={"approval_state": "approved"}).status_code == 403


def test_pagination_thousand_rows(client: TestClient) -> None:
    """protects=complete cursor traversal; fails_when=1000 rows are capped,
    repeated or skipped; no_claim=concurrent mutation during paging.
    """
    for batch in range(5):
        payload = report(
            [extension(id=f"extension-{batch * 200 + i:04d}") for i in range(200)],
            profile_ref=f"{batch:064x}",
        )
        response = client.post(REPORTS, json=payload)
        assert response.status_code == 202, response.text
    for group in (None, "extension"):
        seen = []
        params = {"limit": 137}
        if group:
            params["group"] = group
        while True:
            response = client.get(EXTENSIONS, params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            seen.extend(row["extension_id"] for row in page["items"])
            if page["next_cursor"] is None:
                break
            params["cursor"] = page["next_cursor"]
        assert len(seen) == len(set(seen)) == 1000
    assert client.get(EXTENSIONS, params={"cursor": "broken"}).status_code == 422


@pytest.mark.parametrize("constraint", [None, "1.4.0", "1.4.*", "v1.4-rc.1"])
def test_valid_constraints(constraint: str | None) -> None:
    """protects=exact/prefix constraints; fails_when=valid syntax is rejected;
    no_claim=syntax only, version matching belongs to policy consumers.
    """
    ExtensionApprovalUpdate(approval_state="approved", version_constraint=constraint)


@pytest.mark.parametrize(
    "constraint", ["", "*", ">=1.4", "1.*.0", "1.4 || 2", "https://example.com"]
)
def test_invalid_constraints(constraint: str) -> None:
    """protects=bounded grammar; fails_when=range/URL expressions are accepted;
    no_claim=syntactic validation only.
    """
    with pytest.raises(ValidationError):
        ExtensionApprovalUpdate(
            approval_state="approved", version_constraint=constraint
        )


@pytest.mark.parametrize(
    "mutation", ["secret", "mismatch", "legacy", "duplicate", "oversized"]
)
def test_v3_context_and_privacy_validation(mutation: str) -> None:
    """protects=bounded matched DTOs; fails_when=extra fields or mismatched
    candidate evidence are stored; no_claim=identifier content is not inspected.
    """
    payload = report()
    if mutation == "secret":
        payload["candidates"][0]["extensions"][0]["env"] = {"TOKEN": "synthetic-secret"}
    elif mutation == "mismatch":
        payload["candidates"][0]["config_path_hash"] = "d" * 64
    elif mutation == "legacy":
        payload["evidence"]["schema_version"] = 2
    elif mutation == "duplicate":
        payload["candidates"][0]["extensions"].append(extension())
    else:
        payload["candidates"][0]["extensions"].extend(
            extension(id=f"p-{i}") for i in range(200)
        )
    with pytest.raises(ValidationError):
        DiscoveryReportRequest.model_validate(payload)


def test_legacy_evidence_canonical_bytes() -> None:
    """protects=v1/v2 immutable hashes; fails_when=v3 defaults alter old dumps;
    no_claim=canonical serialization rather than HTTP transport.
    """
    evidence = report()["evidence"]
    evidence["schema_version"] = 1
    evidence["applications"][0].pop("extensions")
    evidence["applications"][0].pop("completeness")
    dumped = DiscoveryEvidence.model_validate(evidence).model_dump(mode="json")
    assert "extensions" not in dumped["applications"][0]
    assert "completeness" not in dumped["applications"][0]
    assert "profiles" not in dumped


def test_managed_configuration_does_not_reconcile_or_stale_extension_contexts(
    db_session: Session,
    test_user: models.User,
) -> None:
    """Machine configuration is independent of profile extension collection."""
    db, account = db_session, test_user.account_id
    payload = report()
    managed = {
        "agent_kind": "claude_code",
        "config_path_hash": "d" * 64,
        "config_scope": "managed_config",
        "controls": [
            {
                "axis": "native_tool_hook",
                "configured": "true",
                "configuration_evidence": "managed_config",
            }
        ],
    }
    payload["evidence"]["scan_scope"].append("managed_config")
    payload["evidence"]["applications"].append(managed)
    store(db, account, payload)
    assert crud_discovered_extension.completeness(db, account_id=account) == "complete"
    # A later machine-only collection must not supersede the user context.
    later = report([], at=NOW + timedelta(minutes=2))
    later["candidates"] = []
    later["evidence"]["applications"] = [managed]
    later["evidence"]["scan_scope"] = ["managed_config"]
    store(db, account, later)
    assert crud_discovered_extension.completeness(db, account_id=account) == "unknown"
    assert (
        len(crud_discovered_extension.list_for_account(db, account_id=account)[0]) == 1
    )
    store(db, account, report([], at=NOW + timedelta(minutes=1)))
    assert crud_discovered_extension.list_for_account(db, account_id=account)[0] == []
