"""A metadata-only checkpoint is safe to publish and uses no quota (#1407)."""

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
from preloop.services.checkpoint_runtime import (
    checkpoint_context,
    parse_workspace_snapshots,
)
from preloop.services.flow_artifacts import get_artifact, put_artifact


HEAD = "a" * 40


def metadata_archive(
    *,
    metadata_only: bool = True,
    extra: bytes | None = None,
    head: str = HEAD,
    digest: str = "e" * 64,
) -> bytes:
    metadata = {
        "version": 1,
        "repositories": [
            {
                "path": ".",
                "branch": "main",
                "base_sha": head,
                "head_sha": head,
            }
        ],
        "file_state_sha256": digest,
        "created_at": time.time(),
    }
    if metadata_only:
        metadata["metadata_only"] = True
    raw = json.dumps(metadata).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo("workspace/.preloop-checkpoint.json")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
        if extra is not None:
            payload = tarfile.TarInfo("workspace/tracked.txt")
            payload.size = len(extra)
            archive.addfile(payload, io.BytesIO(extra))
    return stream.getvalue()


def payload_archive() -> bytes:
    return metadata_archive(metadata_only=False, extra=b"source-bytes", digest="f" * 64)


def new_execution(db: Any, account_id: Any) -> dict:
    flow = models.Flow(
        name=f"clean-{uuid4().hex[:8]}",
        prompt_template="test",
        agent_type="codex",
        agent_config={},
        account_id=account_id,
    )
    db.add(flow)
    db.flush()
    execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
    db.add(execution)
    db.flush()
    return {
        "account_id": account_id,
        "flow_id": flow.id,
        "thread_id": "thread-clean",
        "execution_id": execution.id,
    }


@pytest.fixture
def scope(db_session: Any, test_user: models.User) -> dict:
    return new_execution(db_session, test_user.account_id)


def workspace_rows(db: Any, account_id: Any) -> list[models.FlowArtifact]:
    return (
        db.query(models.FlowArtifact)
        .filter(
            models.FlowArtifact.account_id == account_id,
            models.FlowArtifact.kind == "workspace",
        )
        .all()
    )


def test_parse_workspace_snapshots_defaults_to_when_dirty() -> None:
    assert parse_workspace_snapshots(None) == "when_dirty"
    assert parse_workspace_snapshots({}) == "when_dirty"
    assert parse_workspace_snapshots({"workspace_snapshots": "sometimes"}) == (
        "when_dirty"
    )
    assert parse_workspace_snapshots({"workspace_snapshots": "always"}) == "always"
    assert parse_workspace_snapshots({"workspace_snapshots": "never"}) == "never"
    assert (
        parse_workspace_snapshots({"agent_config": {"workspace_snapshots": "always"}})
        == "always"
    )


def test_checkpoint_context_publishes_mode_and_expected_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4 as new_id

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    env = checkpoint_context(
        None,
        {
            "account_id": str(new_id()),
            "flow_id": str(new_id()),
            "execution_id": str(new_id()),
            "agent_config": {"workspace_snapshots": "when_dirty"},
            "trigger_event_data": {
                "payload": {"pull_request": {"head": {"sha": HEAD}}}
            },
            "git_clone_config": {"repositories": []},
        },
    )
    assert env["PRELOOP_WORKSPACE_SNAPSHOTS"] == "when_dirty"
    assert env["PRELOOP_CHECKPOINT_PUT_TOKEN"]
    assert json.loads(env["PRELOOP_CHECKPOINT_EXPECTED_HEADS"]) == {".": HEAD}
    assert env["PRELOOP_CHECKPOINT_CLONED_HEADS"] == "/tmp/preloop-cloned-heads.json"


def test_never_does_not_mint_a_workspace_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4 as new_id

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    env = checkpoint_context(
        None,
        {
            "account_id": str(new_id()),
            "flow_id": str(new_id()),
            "execution_id": str(new_id()),
            "agent_config": {"workspace_snapshots": "never"},
            "trigger_event_data": {},
        },
    )
    assert env["PRELOOP_WORKSPACE_SNAPSHOTS"] == "never"
    assert "PRELOOP_CHECKPOINT_PUT_TOKEN" not in env
    assert env["PRELOOP_NATIVE_SESSION_PUT_TOKEN"]


def test_metadata_only_counts_zero_bytes_when_quota_is_full(
    db_session, scope, monkeypatch
) -> None:
    put_artifact(db_session, **scope, kind="workspace", archive=payload_archive())
    retained = len(workspace_rows(db_session, scope["account_id"])[0].ciphertext)
    usage_before = crud.usage(db_session, account_id=scope["account_id"])
    monkeypatch.setattr(settings, "flow_artifact_account_quota_bytes", retained)

    ref = put_artifact(
        db_session, **scope, kind="workspace", archive=metadata_archive()
    )

    stored = workspace_rows(db_session, scope["account_id"])
    metadata_rows = [row for row in stored if row.ciphertext is None]
    assert metadata_rows
    assert metadata_rows[0].manifest["metadata"]["metadata_only"] is True
    assert metadata_rows[0].manifest["size_bytes"] == 0
    usage_after = crud.usage(db_session, account_id=scope["account_id"])
    assert (
        usage_after["by_kind"]["workspace"]["bytes"]
        == (usage_before["by_kind"]["workspace"]["bytes"])
    )
    read_scope = {key: value for key, value in scope.items() if key != "execution_id"}
    restored = get_artifact(db_session, **read_scope, reference=ref)
    with tarfile.open(fileobj=io.BytesIO(restored), mode="r:gz") as archive:
        names = [item.name for item in archive.getmembers() if item.isfile()]
        source = archive.extractfile("workspace/.preloop-checkpoint.json")
        assert source is not None
        document = json.loads(source.read().decode())
    assert names == ["workspace/.preloop-checkpoint.json"]
    assert document["metadata_only"] is True
    assert document["repositories"][0]["head_sha"] == HEAD

    monkeypatch.setattr(settings, "flow_artifact_account_quota_bytes", 1)
    still = put_artifact(
        db_session, **scope, kind="workspace", archive=metadata_archive()
    )
    assert still.artifact_id
    with pytest.raises(ValueError, match="artifact_quota_exceeded"):
        put_artifact(
            db_session,
            **scope,
            kind="workspace",
            archive=metadata_archive(
                metadata_only=False, extra=b"more-source", digest="d" * 64
            ),
        )


def test_smuggled_payload_still_counts_against_quota(
    db_session, scope, monkeypatch
) -> None:
    put_artifact(db_session, **scope, kind="workspace", archive=payload_archive())
    retained = len(workspace_rows(db_session, scope["account_id"])[0].ciphertext)
    monkeypatch.setattr(settings, "flow_artifact_account_quota_bytes", retained)
    with pytest.raises(ValueError, match="artifact_quota_exceeded"):
        put_artifact(
            db_session,
            **scope,
            kind="workspace",
            archive=metadata_archive(extra=b"hidden-source"),
        )


def test_identical_metadata_only_checkpoints_reuse_one_row(db_session, scope) -> None:
    first = put_artifact(
        db_session, **scope, kind="workspace", archive=metadata_archive()
    )
    second = put_artifact(
        db_session, **scope, kind="workspace", archive=metadata_archive()
    )
    assert second.deduplicated is True
    assert second.artifact_id == first.artifact_id
    assert len(workspace_rows(db_session, scope["account_id"])) == 1


def test_expired_metadata_only_row_is_marked_expired(db_session, scope) -> None:
    ref = put_artifact(
        db_session, **scope, kind="workspace", archive=metadata_archive()
    )
    row = crud.get(
        db_session,
        artifact_id=ref.artifact_id,
        **{key: value for key, value in scope.items() if key != "execution_id"},
    )
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert crud.cleanup(db_session, now=datetime.now(UTC)) == 1
    db_session.refresh(row)
    assert row.availability == "expired"
    assert row.ciphertext is None
