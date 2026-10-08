"""An unchanged workspace is stored once per execution and thread (#1339).

A run captures its workspace periodically and again before publication. On a
clean review checkout both captures hold the same files at the same commit,
so the second one reuses the first row instead of storing it again.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import flow_artifact as crud
from preloop.services.flow_artifacts import get_artifact, put_artifact

HEAD = "a" * 40


def snapshot(
    digest: str = "f" * 64,
    heads: tuple[str, ...] = (HEAD,),
    payload: bytes = b"source",
) -> bytes:
    """A workspace archive shaped like checkpoint_client.capture output.

    ``created_at`` differs on every call, as it does between real captures,
    so identical state never means identical archive bytes.
    """
    metadata = json.dumps(
        {
            "version": 1,
            "repositories": [
                {"path": f"repo{i}", "branch": "main", "head_sha": head}
                for i, head in enumerate(heads)
            ],
            "file_state_sha256": digest,
            "created_at": time.time(),
        }
    ).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in (
            ("workspace/source", payload),
            ("workspace/.preloop-checkpoint.json", metadata),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def new_execution(db: Any, account_id: Any, flow_id: Any | None = None) -> dict:
    if flow_id is None:
        flow = models.Flow(
            name=f"dedupe-{uuid4().hex[:8]}",
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
        "thread_id": "thread-dedupe",
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
            models.FlowArtifact.kind == "workspace",
        )
        .all()
    )


def test_identical_state_is_one_row_and_two_receipts(db_session, scope) -> None:
    first = put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    row = rows(db_session, scope["account_id"])[0]
    row.expires_at = datetime.now(UTC) + timedelta(minutes=5)
    db_session.commit()

    second = put_artifact(db_session, **scope, kind="workspace", archive=snapshot())

    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.artifact_id == first.artifact_id
    assert second.manifest_sha256 == first.manifest_sha256
    stored = rows(db_session, scope["account_id"])
    assert len(stored) == 1
    # Retention restarts at the second capture, as a new row's would.
    expected = datetime.now(UTC) + timedelta(
        hours=settings.workspace_snapshot_ttl_hours
    )
    assert abs(stored[0].expires_at - expected) < timedelta(minutes=1)
    # Recovery still finds it by execution and thread, and it still reads.
    latest = crud.latest(db_session, **scope, kind="workspace")
    assert latest.id == first.artifact_id
    read_scope = {k: v for k, v in scope.items() if k != "execution_id"}
    assert get_artifact(db_session, **read_scope, reference=second)


def test_expiry_is_never_shortened(db_session, scope, monkeypatch) -> None:
    put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    row = rows(db_session, scope["account_id"])[0]
    later = datetime.now(UTC) + timedelta(days=30)
    row.expires_at = later
    db_session.commit()
    ref = put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    assert ref.deduplicated is True
    db_session.refresh(row)
    assert row.expires_at == later


@pytest.mark.parametrize(
    "changed",
    [
        {"digest": "e" * 64},
        {"heads": ("b" * 40,)},
        {"heads": (HEAD, "c" * 40)},
    ],
)
def test_changed_state_is_two_rows(db_session, scope, changed: dict) -> None:
    first = put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    second = put_artifact(
        db_session, **scope, kind="workspace", archive=snapshot(**changed)
    )
    assert second.deduplicated is False
    assert second.artifact_id != first.artifact_id
    assert len(rows(db_session, scope["account_id"])) == 2


def test_only_the_newest_row_is_compared(db_session, scope) -> None:
    """A-B-A stores three rows: the resume point must be the newest state."""
    put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    put_artifact(
        db_session, **scope, kind="workspace", archive=snapshot(digest="e" * 64)
    )
    third = put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    assert third.deduplicated is False
    assert len(rows(db_session, scope["account_id"])) == 3


def test_no_digest_is_never_deduplicated(db_session, scope) -> None:
    body = snapshot(digest="")
    put_artifact(db_session, **scope, kind="workspace", archive=body)
    ref = put_artifact(db_session, **scope, kind="workspace", archive=body)
    assert ref.deduplicated is False
    assert len(rows(db_session, scope["account_id"])) == 2


def test_expired_row_is_not_reused(db_session, scope) -> None:
    put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    row = rows(db_session, scope["account_id"])[0]
    row.ciphertext = None
    row.availability = "expired"
    db_session.commit()
    ref = put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    assert ref.deduplicated is False


def test_dedupe_never_crosses_threads(db_session, scope) -> None:
    put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    ref = put_artifact(
        db_session,
        **{**scope, "thread_id": "other-thread"},
        kind="workspace",
        archive=snapshot(),
    )
    assert ref.deduplicated is False
    assert len(rows(db_session, scope["account_id"])) == 2


def test_dedupe_never_crosses_executions(db_session, scope) -> None:
    """Recovery looks snapshots up by execution, so each keeps its own row."""
    put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    resumed = new_execution(db_session, scope["account_id"], scope["flow_id"])
    ref = put_artifact(db_session, **resumed, kind="workspace", archive=snapshot())
    assert ref.deduplicated is False
    assert crud.latest(db_session, **resumed, kind="workspace").id == ref.artifact_id


def test_dedupe_never_crosses_accounts(db_session, scope) -> None:
    put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    other = models.Account(organization_name="other account")
    db_session.add(other)
    db_session.flush()
    other_scope = new_execution(db_session, other.id)
    ref = put_artifact(db_session, **other_scope, kind="workspace", archive=snapshot())
    assert ref.deduplicated is False
    assert len(rows(db_session, other.id)) == 1
    assert len(rows(db_session, scope["account_id"])) == 1


def test_deduplicated_capture_never_raises_quota(
    db_session, scope, monkeypatch
) -> None:
    put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    retained = len(rows(db_session, scope["account_id"])[0].ciphertext)
    # Exactly full: any new row would be refused.
    monkeypatch.setattr(settings, "flow_artifact_account_quota_bytes", retained)
    ref = put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    assert ref.deduplicated is True
    with pytest.raises(ValueError, match="artifact_quota_exceeded"):
        put_artifact(
            db_session, **scope, kind="workspace", archive=snapshot(digest="e" * 64)
        )


def test_closed_execution_is_still_refused(db_session, scope) -> None:
    put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    execution.status = "SUCCEEDED"
    db_session.commit()
    with pytest.raises(ValueError, match="artifact_execution_closed"):
        put_artifact(db_session, **scope, kind="workspace", archive=snapshot())
