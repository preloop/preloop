"""Period export: manifest shape, digests, boundaries and the row cap."""

import hashlib
import io
import json
import tarfile
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from preloop.config import settings
from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.models import models
from preloop.models.models.audit_log import AuditLog
from preloop.services.legal_hold import place_hold
from preloop.services.retention_export import (
    EXPORT_MANIFEST_SCHEMA,
    MEMBER_APPROVALS,
    MEMBER_AUDIT,
    MEMBER_EVIDENCE,
    MEMBER_HOLDS,
    PeriodExportError,
    audit_period_export,
    build_period_export,
)

PERIOD_START = datetime(2026, 4, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 5, 1, tzinfo=UTC)
INSIDE = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def account(db_session, test_user):
    return db_session.get(models.Account, test_user.account_id)


def _audit_row(db_session, account_id, when, action="permission_check"):
    row = AuditLog(
        account_id=account_id,
        action=action,
        resource_type="tool",
        resource_id="send_payment",
        status="success",
        timestamp=when,
        details={"decision": "allow"},
    )
    db_session.add(row)
    db_session.flush()
    return row


def _approval(db_session, test_user, when, *, status="approved"):
    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name=f"wf-{uuid.uuid4().hex[:8]}",
        approval_type="manual",
        channel="email",
    )
    db_session.add(workflow)
    db_session.flush()
    config = models.ToolConfiguration(
        account_id=test_user.account_id,
        tool_name="send_payment",
        tool_source="mcp",
        approval_workflow_id=workflow.id,
        is_enabled=True,
        custom_config={},
    )
    db_session.add(config)
    db_session.flush()
    request = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        tool_name="send_payment",
        tool_args={"amount": 1000, "iban": "secret account number"},
        summary="Pay invoice 12",
        status=status,
        requested_at=when.replace(tzinfo=None),
    )
    db_session.add(request)
    db_session.flush()
    return request


def _evidence(db_session, test_user, when):
    flow = models.Flow(
        name=f"flow-{uuid.uuid4().hex[:8]}",
        prompt_template="t",
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(
        flow_id=flow.id,
        status="COMPLETED",
        trigger_event_details={"_session_thread_id": "t"},
    )
    db_session.add(execution)
    db_session.flush()
    artifact = models.FlowArtifact(
        account_id=test_user.account_id,
        flow_id=flow.id,
        execution_id=execution.id,
        thread_id="t",
        kind="evidence",
        manifest={"sha256": "c" * 64, "size_bytes": 4096},
        manifest_sha256="d" * 64,
        ciphertext=b"cipher-bytes-nobody-should-see",
        expires_at=when + timedelta(days=30),
        created_at=when,
        availability="available",
    )
    db_session.add(artifact)
    db_session.flush()
    return execution, artifact


def _members(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {
            member.name: tar.extractfile(member).read() for member in tar.getmembers()
        }


def _manifest(archive: bytes) -> dict:
    return json.loads(_members(archive)["manifest.json"])


# --- shape -----------------------------------------------------------------


def test_the_archive_carries_a_manifest_and_one_file_per_class(
    db_session, test_user, account
):
    _audit_row(db_session, account.id, INSIDE)
    _approval(db_session, test_user, INSIDE)
    _evidence(db_session, test_user, INSIDE)
    db_session.flush()

    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    names = set(_members(export.archive))
    assert names == {
        "manifest.json",
        MEMBER_AUDIT,
        MEMBER_APPROVALS,
        MEMBER_EVIDENCE,
        MEMBER_HOLDS,
    }


def test_the_manifest_digests_every_member(db_session, test_user, account):
    _audit_row(db_session, account.id, INSIDE)
    db_session.flush()

    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    members = _members(export.archive)
    manifest = _manifest(export.archive)
    assert manifest["schema"] == EXPORT_MANIFEST_SCHEMA
    for entry in manifest["members"]:
        body = members[entry["name"]]
        assert entry["size_bytes"] == len(body)
        assert entry["sha256"] == hashlib.sha256(body).hexdigest()


def test_the_members_digest_is_computed_the_way_evidence_packs_do_it(
    db_session, test_user, account
):
    """#511's shape, so one verifier covers both and #558 signs one format."""
    _audit_row(db_session, account.id, INSIDE)
    db_session.flush()

    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    manifest = _manifest(export.archive)
    expected = hashlib.sha256(canonical_manifest_json(manifest["members"])).hexdigest()
    assert manifest["members_digest"] == expected


def test_tampering_with_a_member_breaks_its_digest(db_session, test_user, account):
    _audit_row(db_session, account.id, INSIDE)
    db_session.flush()
    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )
    manifest = _manifest(export.archive)
    entry = next(m for m in manifest["members"] if m["name"] == MEMBER_AUDIT)

    altered = _members(export.archive)[MEMBER_AUDIT].replace(b"allow", b"deny_")

    assert hashlib.sha256(altered).hexdigest() != entry["sha256"]


def test_the_manifest_says_plainly_that_it_is_not_signed(
    db_session, test_user, account
):
    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    assert "not signed" in _manifest(export.archive)["note"]


def test_the_manifest_records_the_retention_in_force(db_session, account):
    account.meta_data = {"retention": {"audit": 400}}
    db_session.add(account)
    db_session.flush()

    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    assert _manifest(export.archive)["retention"]["audit"] == 400


def test_the_same_rows_produce_the_same_archive_bytes(db_session, account):
    _audit_row(db_session, account.id, INSIDE)
    db_session.flush()
    stamp = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)

    first = build_period_export(
        db_session,
        account=account,
        start=PERIOD_START,
        end=PERIOD_END,
        generated_at=stamp,
    )
    second = build_period_export(
        db_session,
        account=account,
        start=PERIOD_START,
        end=PERIOD_END,
        generated_at=stamp,
    )

    assert first.sha256 == second.sha256


# --- contents --------------------------------------------------------------


def test_only_rows_inside_the_period_are_exported(db_session, account):
    _audit_row(db_session, account.id, PERIOD_START - timedelta(seconds=1), "before")
    _audit_row(db_session, account.id, PERIOD_START, "at_start")
    _audit_row(db_session, account.id, PERIOD_END, "at_end")
    _audit_row(db_session, account.id, INSIDE, "inside")
    db_session.flush()

    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    body = _members(export.archive)[MEMBER_AUDIT].decode()
    actions = {json.loads(line)["action"] for line in body.splitlines()}
    # Start inclusive, end exclusive, so consecutive periods tile exactly.
    assert actions == {"at_start", "inside"}


def test_another_accounts_rows_are_never_exported(db_session, account):
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    _audit_row(db_session, other.id, INSIDE, "theirs")
    _audit_row(db_session, account.id, INSIDE, "ours")
    db_session.flush()

    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    body = _members(export.archive)[MEMBER_AUDIT].decode()
    assert "theirs" not in body
    assert "ours" in body


def test_evidence_is_exported_by_receipt_not_by_payload(db_session, test_user, account):
    """Inlining packs would be unbounded and would duplicate the download."""
    execution, artifact = _evidence(db_session, test_user, INSIDE)
    db_session.flush()

    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    assert b"cipher-bytes-nobody-should-see" not in export.archive
    row = json.loads(_members(export.archive)[MEMBER_EVIDENCE].decode().strip())
    assert row["artifact_id"] == str(artifact.id)
    assert row["sha256"] == "c" * 64
    assert row["payload_present"] is True
    assert "object_lock" not in row


def test_approval_tool_arguments_stay_out_of_the_export(db_session, test_user, account):
    _approval(db_session, test_user, INSIDE)
    db_session.flush()

    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    body = _members(export.archive)[MEMBER_APPROVALS].decode()
    assert "secret account number" not in body
    assert json.loads(body.strip())["summary"] == "Pay invoice 12"


def test_holds_placed_in_the_period_explain_a_frozen_record(
    db_session, test_user, account
):
    execution, _ = _evidence(db_session, test_user, INSIDE)
    db_session.flush()
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
        now=INSIDE,
        user_id=test_user.id,
    )

    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    row = json.loads(_members(export.archive)[MEMBER_HOLDS].decode().strip())
    assert row["reason"] == "incident review INC-114"
    assert row["active"] is True


def test_an_empty_period_is_an_archive_not_an_error(db_session, account):
    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    assert export.counts == {
        "audit": 0,
        "approvals": 0,
        "evidence": 0,
        "legal_holds": 0,
    }
    assert _members(export.archive)[MEMBER_AUDIT] == b""


# --- bounds ----------------------------------------------------------------


def test_a_period_over_the_row_cap_is_refused_not_truncated(
    db_session, account, monkeypatch
):
    """A silently short compliance export is worse than no export."""
    monkeypatch.setattr(settings, "retention_export_max_rows", 2, raising=False)
    for index in range(3):
        _audit_row(db_session, account.id, INSIDE, f"row-{index}")
    db_session.flush()

    with pytest.raises(PeriodExportError) as excinfo:
        build_period_export(
            db_session, account=account, start=PERIOD_START, end=PERIOD_END
        )

    assert excinfo.value.code == "period_too_large"
    assert "narrow" in str(excinfo.value)


def test_exactly_at_the_cap_still_exports(db_session, account, monkeypatch):
    monkeypatch.setattr(settings, "retention_export_max_rows", 2, raising=False)
    for index in range(2):
        _audit_row(db_session, account.id, INSIDE, f"row-{index}")
    db_session.flush()

    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    assert export.counts["audit"] == 2


def test_an_inverted_period_is_refused(db_session, account):
    with pytest.raises(PeriodExportError) as excinfo:
        build_period_export(
            db_session, account=account, start=PERIOD_END, end=PERIOD_START
        )

    assert excinfo.value.code == "invalid_period"


# --- audit -----------------------------------------------------------------


def test_the_export_is_audited_with_the_digest_of_what_was_taken(
    db_session, test_user, account
):
    _audit_row(db_session, account.id, INSIDE)
    db_session.flush()
    export = build_period_export(
        db_session, account=account, start=PERIOD_START, end=PERIOD_END
    )

    audit_period_export(
        db_session, account_id=account.id, user_id=test_user.id, export=export
    )

    from sqlalchemy import select

    row = db_session.execute(
        select(AuditLog).where(
            AuditLog.account_id == account.id,
            AuditLog.action == "retention_period_export",
        )
    ).scalar_one()
    assert row.user_id == test_user.id
    assert row.details["archive_sha256"] == export.sha256
    assert row.details["period_start"] == "2026-04-01T00:00:00Z"
    assert row.details["counts"]["audit"] == 1
