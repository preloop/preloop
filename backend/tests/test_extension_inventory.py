"""Synthetic PostgreSQL and HTTP contracts for installed extension inventory."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_discovered_extension,
    crud_discovery_observation,
    crud_expected_device,
    crud_extension_approval,
    crud_user,
)
from preloop.schemas.agent_discovery import DiscoveryReportRequest
from preloop.schemas.discovered_extension import (
    ExtensionApprovalUpdate,
    ExtensionPinCreate,
)
from preloop.schemas.discovery_evidence import DiscoveryEvidence

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


_AGGREGATE_2314_KEYS = (
    "agent_kind",
    "extension_kind",
    "extension_id",
    "device_count",
    "versions",
    "source_kinds",
    "approval_state",
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
    # #2321 adds pin_count and content_hashes; compare the #2314 keys exactly.
    assert [{key: item[key] for key in _AGGREGATE_2314_KEYS} for item in grouped] == [
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


def test_complete_app_reconciles_despite_unrelated_profile_partial(
    db_session: Session, test_user: models.User
) -> None:
    """protects=app-level absence; fails_when=unverified runtime blocks removals;
    no_claim=synthetic all-profile evidence, no OS enumeration.
    """
    store(db_session, test_user.account_id, report(profile_ref="c" * 64))
    payload = report([], profile_ref="c" * 64, at=NOW + timedelta(minutes=1))
    payload["evidence"]["completeness"] = "partial"
    payload["evidence"]["profiles"][0].update(
        completeness="partial", error_code="probe_incomplete"
    )
    store(db_session, test_user.account_id, payload)
    assert (
        crud_discovered_extension.list_for_account(
            db_session, account_id=test_user.account_id
        )[0]
        == []
    )


def test_failed_envelope_never_proves_absence(
    db_session: Session, test_user: models.User
) -> None:
    """protects=failed scope; fails_when=a failed envelope removes inventory;
    no_claim=synthetic contradictory collector claims.
    """
    store(db_session, test_user.account_id, report())
    payload = report([], at=NOW + timedelta(minutes=1))
    payload["evidence"]["completeness"] = "failed"
    store(db_session, test_user.account_id, payload)
    assert (
        len(
            crud_discovered_extension.list_for_account(
                db_session, account_id=test_user.account_id
            )[0]
        )
        == 1
    )


@pytest.mark.parametrize("has_user_app", [False, True])
def test_managed_failure_preserves_complete_user_inventory(
    db_session: Session,
    test_user: models.User,
    has_user_app: bool,
) -> None:
    """Completed profile scans retain their coverage when another scope fails."""
    payload = report(profile_ref="c" * 64)
    if not has_user_app:
        payload["candidates"] = []
        payload["evidence"]["applications"] = []
    payload["evidence"].update(
        completeness="partial",
        errors=["permission_denied"],
        scan_scope=["user_config", "managed_config"],
        scope_completeness={"user_config": "complete", "managed_config": "partial"},
        extension_completeness="complete",
    )
    payload["evidence"]["applications"].append(
        {
            "agent_kind": "claude_code",
            "config_path_hash": "d" * 64,
            "config_scope": "managed_config",
            "controls": [
                {
                    "axis": "native_tool_hook",
                    "configured": "unknown",
                    "configuration_evidence": "unavailable",
                }
            ],
        }
    )
    store(db_session, test_user.account_id, payload)
    assert (
        crud_discovered_extension.completeness(
            db_session, account_id=test_user.account_id
        )
        == "complete"
    )


def test_empty_complete_user_scan_with_managed_context_is_complete(
    db_session: Session,
    test_user: models.User,
) -> None:
    """Zero installed applications is a valid complete profile collection."""
    payload = report(profile_ref="c" * 64)
    payload["candidates"] = []
    payload["evidence"]["scan_scope"] = ["user_config", "managed_config"]
    payload["evidence"]["applications"] = [
        {
            "agent_kind": "claude_code",
            "config_path_hash": "d" * 64,
            "config_scope": "managed_config",
        }
    ]
    store(db_session, test_user.account_id, payload)
    assert (
        crud_discovered_extension.completeness(
            db_session, account_id=test_user.account_id
        )
        == "complete"
    )


def test_extension_coverage_states(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """protects=honest coverage; fails_when=legacy or failed scope looks complete;
    no_claim=synthetic HTTP reports, not OS profile enumeration.
    """
    assert client.get(EXTENSIONS).json()["completeness"] == "unknown"
    payload = report(profile_ref="c" * 64)
    payload["evidence"]["completeness"] = "partial"
    payload["evidence"]["profiles"][0].update(
        completeness="partial", error_code="probe_incomplete"
    )
    payload["evidence"]["extension_completeness"] = "complete"
    assert client.post(REPORTS, json=payload).status_code == 202
    assert client.get(EXTENSIONS).json()["completeness"] == "complete"
    payload = report(completeness="partial", at=NOW + timedelta(minutes=1))
    assert client.post(REPORTS, json=payload).status_code == 202
    assert client.get(EXTENSIONS).json()["completeness"] == "partial"
    legacy = report(profile_ref="c" * 64, at=NOW + timedelta(minutes=2))
    legacy["workstation_fingerprint"] = "d" * 64
    legacy["candidates"][0].pop("extensions")
    legacy["evidence"]["schema_version"] = 2
    legacy["evidence"]["applications"][0].pop("extensions")
    legacy["evidence"]["applications"][0].pop("completeness")
    crud_discovery_observation.record(
        db_session,
        account_id=test_user.account_id,
        workstation_fingerprint=legacy["workstation_fingerprint"],
        source_ref=test_user.id,
        evidence=DiscoveryEvidence.model_validate(legacy["evidence"]).model_dump(
            mode="json"
        ),
    )
    assert client.get(EXTENSIONS).json()["completeness"] == "unknown"


def test_failed_profile_preserves_presence_and_newer_empty_blocks_old_presence(
    db_session: Session, test_user: models.User
) -> None:
    """protects=absence ordering; fails_when=failed profiles delete or old scans
    resurrect absent rows; no_claim=source timestamps are synthetic.
    """
    account = test_user.account_id
    store(db_session, account, report(profile_ref="c" * 64))
    payload = report([], profile_ref="c" * 64, at=NOW + timedelta(minutes=1))
    payload["evidence"]["profiles"][0].update(
        completeness="failed", error_code="profile_unreadable"
    )
    payload["evidence"]["completeness"] = "partial"
    store(db_session, account, payload)
    assert (
        len(
            crud_discovered_extension.list_for_account(db_session, account_id=account)[
                0
            ]
        )
        == 1
    )
    store(
        db_session,
        account,
        report([], profile_ref="c" * 64, at=NOW + timedelta(minutes=2)),
    )
    store(db_session, account, report(profile_ref="c" * 64))
    assert (
        crud_discovered_extension.list_for_account(db_session, account_id=account)[0]
        == []
    )


def test_account_lock_is_exclusive_for_writers() -> None:
    """protects=writer serialization; fails_when=SQL uses shared KEY SHARE;
    no_claim=SQL compilation only, concurrency is exercised separately.
    """
    db = Mock()
    crud_expected_device.lock_account(db, account_id=uuid4())
    statement = db.scalar.call_args.args[0]
    assert str(statement.compile(dialect=postgresql.dialect())).endswith(
        "FOR NO KEY UPDATE"
    )


def test_concurrent_first_reports_and_approvals(db_engine: Engine) -> None:
    """protects=atomic first writes; fails_when=parallel reports/decisions race
    into duplicates or old presence wins; no_claim=4 local PostgreSQL writers.
    """
    with Session(db_engine) as db:
        account = crud_account.create(
            db, obj_in={"organization_name": "Example concurrency"}
        )
        account_id = account.id
        user = crud_user.create(
            db,
            obj_in={
                "account_id": account_id,
                "email": f"{uuid4()}@example.com",
                "username": f"user-{uuid4()}",
                "hashed_password": "synthetic",
            },
        )
        user_id = user.id
        db.commit()
    barrier = Barrier(4)

    def write(index: int) -> None:
        """Start independent transactions together against one natural key."""
        with Session(db_engine) as db:
            barrier.wait(timeout=10)
            if index < 2:
                store(
                    db,
                    account_id,
                    report([] if index else None, at=NOW + timedelta(minutes=index)),
                )
            else:
                crud_extension_approval.set_state(
                    db,
                    account_id=account_id,
                    agent_kind="claude_code",
                    extension_kind="plugin",
                    extension_id="reviewer@community",
                    decided_by_user_id=user_id,
                    payload=ExtensionApprovalUpdate(approval_state="approved"),
                )
            db.commit()

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(4)))
        with Session(db_engine) as db:
            assert (
                crud_discovered_extension.list_for_account(db, account_id=account_id)[0]
                == []
            )
            assert len(crud_extension_approval.list(db, account_id=account_id)[0]) == 1
    finally:
        with Session(db_engine) as db:
            crud_account.delete(db, id=account_id)


def test_coverage_projects_scalars_after_selecting_latest(
    db_session: Session, test_user: models.User
) -> None:
    """protects=bounded result payload; fails_when=coverage fetches full history
    JSON; no_claim=1000 synthetic observations, not production latency benchmark.
    """
    for index in range(1000):
        evidence = DiscoveryEvidence.model_validate(
            report(
                [extension(id=f"plugin-{i}") for i in range(20)],
                at=NOW + timedelta(seconds=index),
            )["evidence"]
        )
        crud_discovery_observation.record(
            db_session,
            account_id=test_user.account_id,
            workstation_fingerprint="a" * 64,
            source_ref=test_user.id,
            evidence=evidence.model_dump(mode="json"),
        )
    statements: list[str] = []

    def capture(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        """Capture only SQL structure, never bound report values."""
        statements.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert (
            crud_discovered_extension.completeness(
                db_session, account_id=test_user.account_id
            )
            == "complete"
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(statements) == 1
    projection = statements[0].split("FROM", 1)[0]
    assert "->>" in projection and "coalesce" in projection
    assert "DISTINCT ON" in statements[0]
    assert "SELECT DISTINCT ON" in statements[0]


@pytest.mark.parametrize("legacy", [1, 2])
def test_extension_coverage_is_v3_only(legacy: int) -> None:
    """protects=immutable legacy schema; fails_when=v3 scalar enters v1/v2;
    no_claim=Pydantic validation only.
    """
    payload = report()["evidence"]
    payload["schema_version"] = legacy
    payload["applications"][0].pop("extensions")
    payload["applications"][0].pop("completeness")
    payload["extension_completeness"] = "complete"
    with pytest.raises(ValidationError):
        DiscoveryEvidence.model_validate(payload)


EXPORT = "/api/v1/agents/extension-approvals/export"


def test_export_requires_view_agents(
    client: TestClient, test_viewer_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=view_agents on the policy download; fails_when=a user without
    it can export decisions; no_claim=synthetic permission provider, not EE RBAC.
    """
    monkeypatch.setattr(
        "preloop.utils.permissions.members_have_full_access", lambda: False
    )

    def holds(_db: Any, _user: models.User, permission: str) -> bool:
        return permission == "view_agents"

    monkeypatch.setattr("preloop.utils.permissions.user_holds_permission", holds)
    client.app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
    assert client.get(EXPORT).status_code == 200
    monkeypatch.setattr(
        "preloop.utils.permissions.user_holds_permission",
        lambda _db, _user, _permission: False,
    )
    assert client.get(EXPORT).status_code == 403


def test_export_policy_file_shape(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """protects=every approval row and its pinned hashes in the offline file;
    fails_when=the export stops at the list page size, leaks a note or account
    id, drops a pin, or includes another account; no_claim=one synthetic pin,
    not the pin limit.
    """
    pinned = "sha256:" + "ab" * 32
    for index in range(51):
        crud_extension_approval.set_state(
            db_session,
            account_id=test_user.account_id,
            agent_kind="claude_code",
            extension_kind="plugin",
            extension_id=f"plugin-{index:03d}",
            decided_by_user_id=test_user.id,
            payload=ExtensionApprovalUpdate(
                approval_state="unknown" if index == 0 else "approved",
                version_constraint="1.4.*" if index == 1 else None,
                note="synthetic-note-value" if index == 1 else None,
                pin=(
                    ExtensionPinCreate(content_hash=pinned, note="synthetic-pin-note")
                    if index == 1
                    else None
                ),
            ),
        )
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Example account"}
    )
    crud_extension_approval.set_state(
        db_session,
        account_id=other.id,
        agent_kind="claude_code",
        extension_kind="plugin",
        extension_id="other-only",
        decided_by_user_id=test_user.id,
        payload=ExtensionApprovalUpdate(approval_state="approved"),
    )
    db_session.flush()
    response = client.get(EXPORT)
    assert response.status_code == 200, response.status_code
    body = response.json()
    assert body["schema"] == "preloop.extension-policy.v1"
    assert (
        body["account_ref"]
        == hashlib.sha256(str(test_user.account_id).encode()).hexdigest()[:16]
    )
    assert len(body["account_ref"]) == 16
    assert str(test_user.account_id) not in response.text
    assert str(test_user.id) not in response.text
    assert "synthetic-note-value" not in response.text
    assert "decided_by" not in response.text
    assert len(body["entries"]) == 51
    assert [row["extension_id"] for row in body["entries"]] == [
        f"plugin-{index:03d}" for index in range(51)
    ]
    assert body["entries"][0]["approval_state"] == "unknown"
    assert body["entries"][1]["version_constraint"] == "1.4.*"
    assert body["entries"][1]["content_hashes"] == [pinned]
    assert all(
        row["content_hashes"] == []
        for row in body["entries"]
        if row["extension_id"] != "plugin-001"
    )
    assert "synthetic-pin-note" not in response.text
    assert "other-only" not in response.text


def test_coverage_uses_latest_device_reasons_and_is_account_scoped(
    db_session: Session, test_user: models.User, client: TestClient
) -> None:
    """Current collector failures survive the report and inventory API."""
    payload = report(completeness="partial")
    payload["evidence"].update(
        collector_version="1.1.0-rc.1",
        extension_completeness="partial",
        extension_errors=["extension_project_unscanned"],
    )
    store(db_session, test_user.account_id, payload)
    expected = [
        {
            "fingerprint": "a" * 64,
            "collector_version": "1.1.0-rc.1",
            "completeness": "partial",
            "reasons": ["extension_project_unscanned"],
        }
    ]
    stored = crud_discovered_extension.coverage(
        db_session, account_id=test_user.account_id
    )
    # The report time lets the inventory tell a fresh report from a stale one.
    assert all(row.pop("reported_at") is not None for row in stored)
    assert stored == expected
    api = client.get(EXTENSIONS).json()["coverage"]
    assert all(row.pop("reported_at") for row in api)
    assert api == expected
    assert crud_discovered_extension.coverage(db_session, account_id=uuid4()) == []
    assert (
        crud_discovered_extension.coverage(
            db_session, account_id=test_user.account_id, fingerprint="c" * 64
        )
        == []
    )
    newer = report(at=NOW + timedelta(minutes=1))
    newer["evidence"]["collector_version"] = "1.1.0-rc.1"
    store(db_session, test_user.account_id, newer, now=NOW + timedelta(minutes=1))
    coverage = crud_discovered_extension.coverage(
        db_session, account_id=test_user.account_id
    )
    assert coverage[0]["completeness"] == "complete"
    assert coverage[0]["reasons"] == []


def test_extension_reason_codes_preserve_legacy_bytes_and_reject_paths() -> None:
    """The additive reason field is omitted for old reports and allowlisted."""
    payload = report(completeness="partial")["evidence"]
    original = DiscoveryEvidence.model_validate(payload).model_dump(mode="json")
    assert "extension_errors" not in original
    for errors in [["/private/example"], ["extension_project_unscanned"] * 11]:
        with pytest.raises(ValidationError):
            DiscoveryEvidence.model_validate({**payload, "extension_errors": errors})
    with pytest.raises(ValidationError):
        DiscoveryEvidence.model_validate(
            {
                **payload,
                "extension_completeness": "complete",
                "extension_errors": ["extension_project_unscanned"],
            }
        )


def test_extension_details_v4_round_trip(
    db_session: Session, test_user: models.User
) -> None:
    """Store v4 metadata and expose it on both list shapes, preserving v3."""
    details = {
        "description": "Review changes",
        "author": "Jane Doe",
        "bundles": {
            "skills": 1,
            "commands": 0,
            "hooks": 0,
            "mcp_servers": 0,
            "agents": 0,
        },
    }
    payload = report([extension(details=details)])
    payload["evidence"]["schema_version"] = 4
    store(db_session, test_user.account_id, payload)
    for group in (None, "extension"):
        rows, _ = crud_discovered_extension.list_for_account(
            db_session, account_id=test_user.account_id, group=group
        )
        assert rows[0]["details"]["description"] == "Review changes"
    old = report(at=NOW - timedelta(minutes=1))
    store(db_session, test_user.account_id, old)
    rows, _ = crud_discovered_extension.list_for_account(
        db_session, account_id=test_user.account_id
    )
    assert rows[0]["details"]["description"] == "Review changes"


@pytest.mark.parametrize(
    "details",
    [
        {"description": "x" * 301},
        {"description": "line\nbreak"},
        {"description": "https://example.com"},
        {"body": "private"},
        {
            "hook_matchers": [
                {
                    "matcher": "Bash",
                    "count": 1,
                    "commands": [{"executable": "/bin/bash", "preloop_managed": False}],
                }
            ]
        },
    ],
)
def test_extension_details_reject_unsafe_fields(details: dict[str, Any]) -> None:
    """Reject content, paths, URLs and unnormalized text at ingestion."""
    payload = report([extension(details=details)])
    payload["evidence"]["schema_version"] = 4
    with pytest.raises(ValidationError):
        DiscoveryReportRequest.model_validate(payload)
