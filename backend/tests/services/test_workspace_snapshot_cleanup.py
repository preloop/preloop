"""Retention pass over workspace snapshots and Docker workspace volumes."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.services import workspace_snapshot_cleanup as cleanup


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows

    def update(self, values, synchronize_session=False):
        for row in self._rows:
            row.workspace_snapshot = None
        return len(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.commits = 0

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._rows)

    def commit(self):
        self.commits += 1


class TestCutoff:
    def test_cutoff_uses_the_configured_ttl(self, monkeypatch):
        monkeypatch.setattr(
            cleanup.settings, "workspace_snapshot_ttl_hours", 24, raising=False
        )
        now = datetime(2026, 9, 4, 12, 0, 0)

        assert cleanup.workspace_snapshot_cutoff(now) == datetime(2026, 9, 3, 12, 0, 0)

    def test_zero_ttl_expires_everything_immediately(self, monkeypatch):
        monkeypatch.setattr(
            cleanup.settings, "workspace_snapshot_ttl_hours", 0, raising=False
        )
        now = datetime(2026, 9, 4, 12, 0, 0)

        assert cleanup.workspace_snapshot_cutoff(now) == now


class TestSnapshotPurge:
    def test_expired_snapshots_are_cleared_and_committed(self):
        rows = [
            SimpleNamespace(workspace_snapshot=b"old-1"),
            SimpleNamespace(workspace_snapshot=b"old-2"),
        ]
        db = _FakeSession(rows)

        purged = cleanup.purge_expired_snapshots(db, cutoff=datetime(2026, 9, 3))

        assert purged == 2
        assert [row.workspace_snapshot for row in rows] == [None, None]
        assert db.commits == 1

    def test_nothing_expired_does_not_commit(self):
        db = _FakeSession([])

        assert cleanup.purge_expired_snapshots(db, cutoff=datetime(2026, 9, 3)) == 0
        assert db.commits == 0


class TestVolumePurge:
    @staticmethod
    def _fake_docker(volumes, deleted):
        docker = MagicMock()
        docker.volumes.list = AsyncMock(return_value={"Volumes": volumes})

        def _get(name):
            handle = MagicMock()

            async def _delete():
                deleted.append(name)

            handle.delete = _delete
            return handle

        docker.volumes.get = _get
        docker.close = AsyncMock()
        return docker

    @pytest.mark.asyncio
    async def test_only_expired_agent_workspace_volumes_are_removed(self, monkeypatch):
        cutoff = datetime(2026, 9, 3, 12, 0, 0)
        old = (cutoff - timedelta(hours=2)).replace(tzinfo=UTC).isoformat()
        fresh = (cutoff + timedelta(hours=2)).replace(tzinfo=UTC).isoformat()
        volumes = [
            {"Name": "agent-workspace-exec-old", "CreatedAt": old},
            {"Name": "agent-workspace-exec-fresh", "CreatedAt": fresh},
            {"Name": "postgres-data", "CreatedAt": old},
        ]
        deleted: list[str] = []
        monkeypatch.setattr(
            "aiodocker.Docker", lambda *a, **k: self._fake_docker(volumes, deleted)
        )

        removed = await cleanup.purge_expired_docker_volumes(cutoff=cutoff)

        assert removed == 1
        assert deleted == ["agent-workspace-exec-old"]

    @pytest.mark.asyncio
    async def test_no_docker_socket_is_a_no_op(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("Cannot connect to the Docker daemon")

        monkeypatch.setattr("aiodocker.Docker", _boom)

        assert await cleanup.purge_expired_docker_volumes(cutoff=datetime.now()) == 0


class TestCleanupPass:
    @pytest.mark.asyncio
    async def test_pass_reports_both_halves(self, monkeypatch):
        monkeypatch.setattr(
            cleanup.settings, "workspace_snapshot_ttl_hours", 24, raising=False
        )
        monkeypatch.setattr(
            cleanup, "purge_expired_docker_volumes", AsyncMock(return_value=3)
        )
        db = _FakeSession([SimpleNamespace(workspace_snapshot=b"old")])

        report = await cleanup.cleanup_workspace_artifacts(db)

        assert report == {"snapshots_purged": 1, "volumes_removed": 3}


def test_purge_clears_orphaned_null_end_time_snapshots(db_session, test_user):
    """Crashed runs (no end_time) still release snapshots after the start-time TTL."""
    from datetime import UTC, datetime, timedelta

    from preloop.models import models
    from preloop.models.crud import crud_flow_execution

    now = datetime.now(UTC).replace(tzinfo=None)
    old = now - timedelta(hours=48)
    cutoff = now - timedelta(hours=24)
    flow = models.Flow(
        name="Snapshot purge",
        prompt_template="test",
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
    )
    db_session.add(flow)
    db_session.flush()
    orphan = models.FlowExecution(
        flow_id=flow.id,
        status="RUNNING",
        workspace_snapshot=b"orphan",
        start_time=old,
        end_time=None,
    )
    recent = models.FlowExecution(
        flow_id=flow.id,
        status="RUNNING",
        workspace_snapshot=b"live",
        start_time=now,
        end_time=None,
    )
    finished = models.FlowExecution(
        flow_id=flow.id,
        status="SUCCEEDED",
        workspace_snapshot=b"done",
        start_time=old,
        end_time=old,
    )
    db_session.add_all([orphan, recent, finished])
    db_session.commit()

    assert crud_flow_execution.purge_workspace_snapshots(db_session, cutoff=cutoff) == 2
    db_session.refresh(orphan)
    db_session.refresh(recent)
    db_session.refresh(finished)
    assert orphan.workspace_snapshot is None
    assert recent.workspace_snapshot == b"live"
    assert finished.workspace_snapshot is None
