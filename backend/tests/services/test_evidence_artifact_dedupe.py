"""A byte-identical evidence pack is stored once per execution (#1408).

The Kubernetes wrapper and the inner EXIT trap can both PUT the same
archive. The second upload returns the first row instead of storing it
again, the same way an unchanged workspace snapshot does (#1369).
"""

from __future__ import annotations

import io
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import flow_artifact as crud
from preloop.services.flow_artifacts import get_artifact, put_artifact


def archive(payload: bytes = b"findings") -> bytes:
    """A small evidence archive. Identical bytes share a sha256."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as packed:
        info = tarfile.TarInfo("evidence/findings.json")
        info.size = len(payload)
        packed.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def new_execution(db: Any, account_id: Any, flow_id: Any | None = None) -> dict:
    if flow_id is None:
        flow = models.Flow(
            name=f"evidence-dedupe-{uuid4().hex[:8]}",
            prompt_template="test",
            agent_type="codex",
            agent_config={},
            account_id=account_id,
        )
        db.add(flow)
        db.flush()
        flow_id = flow.id
    execution = models.FlowExecution(flow_id=flow_id, status="RUNNING")
    db.add(execution)
    db.flush()
    return {
        "account_id": account_id,
        "flow_id": flow_id,
        "thread_id": "thread-evidence",
        "execution_id": execution.id,
    }


@pytest.fixture
def scope(db_session: Any, test_user: models.User) -> dict:
    return new_execution(db_session, test_user.account_id)


def rows(db: Any, account_id: Any) -> list[models.FlowArtifact]:
    return (
        db.query(models.FlowArtifact)
        .filter(
            models.FlowArtifact.account_id == account_id,
            models.FlowArtifact.kind == "evidence",
        )
        .all()
    )


def signatures(db: Any, account_id: Any) -> int:
    return (
        db.query(models.RecordSignature)
        .filter(models.RecordSignature.account_id == account_id)
        .count()
    )


def test_identical_evidence_is_one_row_and_two_receipts(db_session, scope) -> None:
    body = archive()
    first = put_artifact(db_session, **scope, kind="evidence", archive=body)
    row = rows(db_session, scope["account_id"])[0]
    row.expires_at = datetime.now(UTC) + timedelta(minutes=5)
    db_session.commit()

    second = put_artifact(db_session, **scope, kind="evidence", archive=body)

    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.artifact_id == first.artifact_id
    assert second.manifest_sha256 == first.manifest_sha256
    stored = rows(db_session, scope["account_id"])
    assert len(stored) == 1
    assert signatures(db_session, scope["account_id"]) == 1
    expected = datetime.now(UTC) + timedelta(
        hours=settings.flow_evidence_retention_hours
    )
    assert abs(stored[0].expires_at - expected) < timedelta(minutes=1)
    latest = crud.latest(db_session, **scope, kind="evidence")
    assert latest.id == first.artifact_id
    read_scope = {k: v for k, v in scope.items() if k != "execution_id"}
    assert get_artifact(db_session, **read_scope, reference=second) == body


def test_expiry_is_never_shortened(db_session, scope) -> None:
    body = archive()
    put_artifact(db_session, **scope, kind="evidence", archive=body)
    row = rows(db_session, scope["account_id"])[0]
    # Past the evidence retention window, so a reuse must not pull it back.
    later = datetime.now(UTC) + timedelta(days=90)
    row.expires_at = later
    db_session.commit()
    ref = put_artifact(db_session, **scope, kind="evidence", archive=body)
    assert ref.deduplicated is True
    db_session.refresh(row)
    assert row.expires_at == later


def test_distinct_evidence_bytes_are_two_rows(db_session, scope) -> None:
    first = put_artifact(db_session, **scope, kind="evidence", archive=archive(b"one"))
    second = put_artifact(db_session, **scope, kind="evidence", archive=archive(b"two"))
    assert second.deduplicated is False
    assert second.artifact_id != first.artifact_id
    assert len(rows(db_session, scope["account_id"])) == 2
    assert signatures(db_session, scope["account_id"]) == 2


def test_only_the_newest_evidence_row_is_compared(db_session, scope) -> None:
    """A-B-A stores three rows: a match against an older pack is not a reuse."""
    first = archive(b"a")
    put_artifact(db_session, **scope, kind="evidence", archive=first)
    put_artifact(db_session, **scope, kind="evidence", archive=archive(b"b"))
    third = put_artifact(db_session, **scope, kind="evidence", archive=first)
    assert third.deduplicated is False
    assert len(rows(db_session, scope["account_id"])) == 3


def test_expired_evidence_is_not_reused(db_session, scope) -> None:
    body = archive()
    put_artifact(db_session, **scope, kind="evidence", archive=body)
    row = rows(db_session, scope["account_id"])[0]
    row.ciphertext = None
    row.availability = "expired"
    db_session.commit()
    ref = put_artifact(db_session, **scope, kind="evidence", archive=body)
    assert ref.deduplicated is False
    assert len(rows(db_session, scope["account_id"])) == 2


def test_evidence_dedupe_never_crosses_threads(db_session, scope) -> None:
    body = archive()
    put_artifact(db_session, **scope, kind="evidence", archive=body)
    ref = put_artifact(
        db_session,
        **{**scope, "thread_id": "other-thread"},
        kind="evidence",
        archive=body,
    )
    assert ref.deduplicated is False
    assert len(rows(db_session, scope["account_id"])) == 2


def test_evidence_dedupe_never_crosses_executions(db_session, scope) -> None:
    body = archive()
    put_artifact(db_session, **scope, kind="evidence", archive=body)
    resumed = new_execution(db_session, scope["account_id"], scope["flow_id"])
    ref = put_artifact(db_session, **resumed, kind="evidence", archive=body)
    assert ref.deduplicated is False
    assert crud.latest(db_session, **resumed, kind="evidence").id == ref.artifact_id


def test_evidence_dedupe_never_crosses_accounts(db_session, scope) -> None:
    body = archive()
    put_artifact(db_session, **scope, kind="evidence", archive=body)
    other = models.Account(organization_name="other evidence account")
    db_session.add(other)
    db_session.flush()
    other_scope = new_execution(db_session, other.id)
    ref = put_artifact(db_session, **other_scope, kind="evidence", archive=body)
    assert ref.deduplicated is False
    assert len(rows(db_session, other.id)) == 1
    assert len(rows(db_session, scope["account_id"])) == 1


def test_deduplicated_evidence_never_raises_quota(
    db_session, scope, monkeypatch
) -> None:
    body = archive()
    put_artifact(db_session, **scope, kind="evidence", archive=body)
    retained = len(rows(db_session, scope["account_id"])[0].ciphertext)
    monkeypatch.setattr(settings, "flow_artifact_account_quota_bytes", retained)
    ref = put_artifact(db_session, **scope, kind="evidence", archive=body)
    assert ref.deduplicated is True
    with pytest.raises(ValueError, match="artifact_quota_exceeded"):
        put_artifact(db_session, **scope, kind="evidence", archive=archive(b"other"))


def test_repacked_evidence_reuses_the_first_row(
    db_session, scope, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two packs of the same files are not the same bytes.

    ``pack_evidence`` stamps the gzip header and ``generated_at`` from the
    clock, so a second pack a few seconds later has a different sha256.
    The content identity (``members_digest``) still matches.
    """
    from preloop.agents.checkpoint_client import pack_evidence

    workspace = tmp_path / "workspace"
    (workspace / "evidence").mkdir(parents=True)
    (workspace / "evidence" / "findings.json").write_text('{"id":"X"}')
    real_gmtime = time.gmtime
    clock = {"now": 1_700_000_000.0}

    def frozen_time() -> float:
        return clock["now"]

    def frozen_gmtime(secs: float | None = None) -> time.struct_time:
        return real_gmtime(clock["now"] if secs is None else secs)

    monkeypatch.setattr(time, "time", frozen_time)
    monkeypatch.setattr(time, "gmtime", frozen_gmtime)
    limits = {"max_bytes": 1024 * 1024, "max_expanded_bytes": 1024 * 1024}
    first_body = pack_evidence(workspace, **limits)
    clock["now"] += 5
    second_body = pack_evidence(workspace, **limits)
    assert first_body != second_body

    first = put_artifact(db_session, **scope, kind="evidence", archive=first_body)
    second = put_artifact(db_session, **scope, kind="evidence", archive=second_body)
    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.artifact_id == first.artifact_id
    assert len(rows(db_session, scope["account_id"])) == 1
    assert signatures(db_session, scope["account_id"]) == 1

    (workspace / "evidence" / "findings.json").write_text('{"id":"Y"}')
    clock["now"] += 5
    changed = pack_evidence(workspace, **limits)
    third = put_artifact(db_session, **scope, kind="evidence", archive=changed)
    assert third.deduplicated is False
    assert third.artifact_id != first.artifact_id
    assert len(rows(db_session, scope["account_id"])) == 2


def test_closed_execution_evidence_put_is_still_refused(db_session, scope) -> None:
    body = archive()
    put_artifact(db_session, **scope, kind="evidence", archive=body)
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    execution.status = "SUCCEEDED"
    db_session.commit()
    with pytest.raises(ValueError, match="artifact_execution_closed"):
        put_artifact(db_session, **scope, kind="evidence", archive=body)
