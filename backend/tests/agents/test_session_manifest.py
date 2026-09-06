"""Single-conversation checkpoint boundaries, independent of provider access."""

import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from preloop.agents.session_manifest import (
    SessionRestoreError,
    pack_session,
    restore_session,
    select_session_files,
    unpack_session,
)

NOW = datetime(2026, 9, 6, tzinfo=UTC)
IDENTITY = {
    "harness": "codex",
    "harness_version": "0.100.0",
    "session_id": "session-a",
    "thread_id": "account-a/flow-a/repo-a/pr-a",
}


def checkpoint() -> bytes:
    return pack_session(
        {
            "rollout.jsonl": json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": "session-a", "fact": "apricot"},
                }
            ).encode()
        },
        **IDENTITY,
        now=NOW,
        expires_at=NOW + timedelta(days=7),
    )


def test_checkpoint_roundtrip_empty_home(tmp_path: Path) -> None:
    files = unpack_session(checkpoint(), **IDENTITY, now=NOW)
    assert files is not None
    assert b"apricot" in files["rollout.jsonl"]
    restore_session(files, tmp_path / "home")
    assert (tmp_path / "home/rollout.jsonl").read_bytes() == files["rollout.jsonl"]
    with pytest.raises(SessionRestoreError, match="empty"):
        restore_session(files, tmp_path / "home")


@pytest.mark.parametrize(
    "field", ["thread_id", "session_id", "harness_version", "harness"]
)
def test_foreign_thread_session_or_image_fails(field: str) -> None:
    identity = {**IDENTITY, field: "foreign"}
    with pytest.raises(SessionRestoreError, match="mismatch"):
        unpack_session(checkpoint(), **identity, now=NOW)


def test_expired_handoff_is_distinct_from_corruption() -> None:
    assert unpack_session(checkpoint(), **IDENTITY, now=NOW + timedelta(days=8)) is None
    with pytest.raises(SessionRestoreError, match="corrupt"):
        unpack_session(b"broken", **IDENTITY, now=NOW)


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "auth.json", "logs/a", "a/../b"]
)
def test_forbidden_paths(name: str) -> None:
    with pytest.raises(SessionRestoreError):
        pack_session(
            {name: b"secret"}, **IDENTITY, now=NOW, expires_at=NOW + timedelta(days=1)
        )


def test_symlink_archive_rejected() -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo("files/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/tmp/escape"
        tar.addfile(info)
    with pytest.raises(SessionRestoreError):
        unpack_session(out.getvalue(), **IDENTITY, now=NOW)


def test_codex_selection_excludes_other_issue_and_includes_child(
    tmp_path: Path,
) -> None:
    for sid, parent in [
        ("session-a", None),
        ("child-a", "session-a"),
        ("session-b", None),
    ]:
        payload = {"id": sid, "forked_from_id": parent}
        (tmp_path / f"{sid}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": payload}) + "\n"
        )
    (tmp_path / "auth.json").write_text('{"token":"never pack"}')
    selected = select_session_files(tmp_path, "codex", "session-a")
    assert set(selected) == {"session-a.jsonl", "child-a.jsonl"}
    with pytest.raises(SessionRestoreError, match="not found"):
        select_session_files(tmp_path, "codex", "unknown")


def test_opencode_selection_excludes_other_conversation(tmp_path: Path) -> None:
    for category, key, record in [
        ("session", "ses_a", {"id": "ses_a"}),
        ("session", "ses_b", {"id": "ses_b"}),
        ("message", "msg_a", {"id": "msg_a", "sessionID": "ses_a"}),
        ("part", "part_a", {"sessionID": "ses_a", "messageID": "msg_a"}),
        ("message", "msg_b", {"id": "msg_b", "sessionID": "ses_b"}),
    ]:
        path = tmp_path / category / f"{key}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(record))
    assert set(select_session_files(tmp_path, "opencode", "ses_a")) == {
        "session/ses_a.json",
        "message/msg_a.json",
        "part/part_a.json",
    }


def test_opencode_sqlite_exports_one_graph_and_no_credentials(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "opencode.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE project (id TEXT, worktree TEXT);
        CREATE TABLE session (id TEXT, parent_id TEXT, project_id TEXT, permission TEXT, share_url TEXT);
        CREATE TABLE message (id TEXT, session_id TEXT, data TEXT);
        CREATE TABLE control_account (access_token TEXT);
        INSERT INTO project VALUES ('repo','/workspace');
        INSERT INTO session VALUES ('a',NULL,'repo','dangerous permission','secret share');
        INSERT INTO session VALUES ('child','a','repo',NULL,NULL);
        INSERT INTO session VALUES ('b',NULL,'repo',NULL,NULL);
        INSERT INTO message VALUES ('ma','a','seeded apricot');
        INSERT INTO message VALUES ('mc','child','child evidence');
        INSERT INTO message VALUES ('mb','b','foreign conversation');
        INSERT INTO control_account VALUES ('foreign account token');
        """)
    data = select_session_files(tmp_path, "opencode", "a")["opencode.db"]
    assert b"foreign conversation" not in data
    assert b"foreign account token" not in data
    assert b"dangerous permission" not in data
    assert b"secret share" not in data
    restored = tmp_path / "restored.db"
    restored.write_bytes(data)
    with sqlite3.connect(restored) as db:
        assert set(db.execute("SELECT id FROM session")) == {("a",), ("child",)}
        assert db.execute("SELECT count(*) FROM control_account").fetchone()[0] == 0


def test_codex_capture_selects_parent_not_newest_child(tmp_path: Path) -> None:
    from preloop.agents.session_manifest import capture_codex_session_id

    for name, payload in [
        ("rollout-1-parent.jsonl", {"id": "parent", "source": "exec"}),
        (
            "rollout-9-child.jsonl",
            {
                "id": "child",
                "source": {
                    "subagent": {"thread_spawn": {"parent_thread_id": "parent"}}
                },
            },
        ),
    ]:
        (tmp_path / name).write_text(
            json.dumps({"type": "session_meta", "payload": payload}) + "\n"
        )
    assert set(select_session_files(tmp_path, "codex", "parent")) == {
        "rollout-1-parent.jsonl",
        "rollout-9-child.jsonl",
    }
    assert capture_codex_session_id(tmp_path) == "parent"
    assert capture_codex_session_id(tmp_path, "parent") == "parent"
    with pytest.raises(SessionRestoreError):
        capture_codex_session_id(tmp_path, "child")
