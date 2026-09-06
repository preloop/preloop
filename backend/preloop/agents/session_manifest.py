"""Portable, single-conversation CLI checkpoints (no credential material).

This module uses only the standard library so the same validator can execute
inside supported sandbox images. Storage encryption belongs to the artifact
service; this format never provides an authorization decision by itself.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "preloop.native-session/v1"
MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 10000
DENIED_PARTS = frozenset({"auth.json", "credentials", "logs", "log", ".env"})


class SessionRestoreError(ValueError):
    """An existing checkpoint cannot safely resume the requested conversation."""


def _safe_path(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or path.as_posix() != name
        or any(part in DENIED_PARTS for part in path.parts)
    ):
        raise SessionRestoreError("unsafe session path")
    return name


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def capture_codex_session_id(root: Path, expected_id: str | None = None) -> str:
    """Capture the explicit root execution, never whichever rollout is newest."""
    roots = set()
    for path in root.rglob("rollout-*.jsonl"):
        if path.is_symlink():
            raise SessionRestoreError("rollout is a symlink")
        with path.open("rb") as stream:
            first = stream.readline(1024 * 1024)
        try:
            record = json.loads(first)
        except ValueError as exc:
            raise SessionRestoreError("invalid Codex rollout metadata") from exc
        payload = record.get("payload") or {}
        if (
            record.get("type") == "session_meta"
            and payload.get("source") in ("exec", "cli")
            and not payload.get("forked_from_id")
        ):
            roots.add(str(payload["id"]))
    if expected_id:
        if expected_id not in roots:
            raise SessionRestoreError("explicit Codex root session not found")
        return expected_id
    if len(roots) != 1:
        raise SessionRestoreError("expected exactly one Codex root session")
    return roots.pop()


def _session_parent(record: dict[str, Any]) -> str | None:
    parent = record.get("parentID") or record.get("forked_from_id")
    source = record.get("source")
    if not parent and isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, dict):
            spawn = subagent.get("thread_spawn") or subagent
            if isinstance(spawn, dict):
                parent = spawn.get("parent_thread_id")
    return str(parent) if parent else None


def select_session_files(root: Path, harness: str, session_id: str) -> dict[str, bytes]:
    """Select an explicit conversation and its verified descendant records.

    Codex rollouts identify themselves in session_meta. OpenCode JSON storage
    identifies session parentage and message/part ownership. Unknown storage
    layouts fail explicitly rather than copying an entire harness home.
    """
    if root.is_symlink():
        raise SessionRestoreError("session root is a symlink")
    if harness == "opencode" and (root / "opencode.db").exists():
        return {
            "opencode.db": select_opencode_database(root / "opencode.db", session_id)
        }
    records: dict[str, tuple[bytes, dict[str, Any]]] = {}
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SessionRestoreError("session storage contains a symlink")
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        if any(part in DENIED_PARTS for part in PurePosixPath(name).parts):
            continue
        if path.suffix not in {".json", ".jsonl"}:
            continue
        _safe_path(name)
        if path.stat().st_size > MAX_BYTES:
            raise SessionRestoreError("session record exceeds size limit")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_BYTES or len(records) >= MAX_FILES:
            raise SessionRestoreError("session storage exceeds selection limit")
        try:
            obj = json.loads(data.splitlines()[0] if harness == "codex" else data)
        except (ValueError, IndexError) as exc:
            raise SessionRestoreError("invalid session record") from exc
        if not isinstance(obj, dict):
            continue
        if harness == "codex":
            if obj.get("type") != "session_meta":
                continue
            obj = obj.get("payload", {})
        records[name] = (data, obj)

    if harness not in {"codex", "opencode"}:
        raise SessionRestoreError("unsupported harness storage")
    session_records = {
        name: record
        for name, record in records.items()
        if harness == "codex" or PurePosixPath(name).parts[0] == "session"
    }
    ids = {session_id}
    if not any(obj.get("id") == session_id for _, obj in session_records.values()):
        raise SessionRestoreError("explicit session not found")
    # Include descendants only when storage records explicitly establish parentage.
    while True:
        children = {
            str(obj["id"])
            for _, obj in session_records.values()
            if obj.get("id") and _session_parent(obj) in ids
        }
        before = len(ids)
        ids.update(children)
        if len(ids) == before:
            break
    selected = {
        name: data
        for name, (data, obj) in records.items()
        if (name in session_records and obj.get("id") in ids)
        or (harness == "opencode" and obj.get("sessionID") in ids)
    }
    return selected


def select_opencode_database(path: Path, session_id: str) -> bytes:
    """Export one SQLite session graph in a consistent read transaction.

    Build a NEW database, never redact a copy: deleted SQLite pages can retain
    other transcripts or account tokens. Credential/share/permission tables
    retain schema only. WAL is read through SQLite's transaction machinery.
    """
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise SessionRestoreError("unsafe or oversized SQLite session database")
    allowed = {
        "__drizzle_migrations",
        "project",
        "session",
        "message",
        "part",
        "todo",
        "permission",
        "session_share",
        "control_account",
    }
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source:
        source.execute("PRAGMA trusted_schema=OFF")
        source.execute("BEGIN")
        schemas = source.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if any(name not in allowed for name, _ in schemas):
            raise SessionRestoreError("unsupported OpenCode database schema")
        sessions = source.execute(
            "SELECT id, parent_id, project_id FROM session"
        ).fetchall()
        ids = {session_id}
        if not any(row[0] == session_id for row in sessions):
            raise SessionRestoreError("explicit session not found")
        while True:
            children = {row[0] for row in sessions if row[1] in ids}
            if children <= ids:
                break
            ids.update(children)
        projects = {row[2] for row in sessions if row[0] in ids}
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "opencode.db"
            with sqlite3.connect(target) as dest:
                dest.execute("PRAGMA trusted_schema=OFF")
                for _, sql in schemas:
                    dest.execute(sql)
                for table, _ in schemas:
                    if table in {"permission", "session_share", "control_account"}:
                        continue
                    columns = [
                        row[1]
                        for row in source.execute(f'PRAGMA table_info("{table}")')
                    ]
                    rows = source.execute(f'SELECT * FROM "{table}"')
                    count = 0
                    for raw in rows:
                        row = dict(zip(columns, raw, strict=True))
                        if table == "project" and row["id"] not in projects:
                            continue
                        if table == "session" and row["id"] not in ids:
                            continue
                        if (
                            table in {"message", "part", "todo"}
                            and row["session_id"] not in ids
                        ):
                            continue
                        if table == "session":
                            if row.get("parent_id") not in ids:
                                row["parent_id"] = None
                            for key in ("permission", "share_url"):
                                if key in row:
                                    row[key] = None
                        placeholders = ",".join("?" for _ in columns)
                        dest.execute(
                            f'INSERT INTO "{table}" VALUES ({placeholders})',
                            tuple(row.values()),
                        )
                        count += 1
                        if count > MAX_FILES:
                            raise SessionRestoreError("session row limit exceeded")
            data = target.read_bytes()
            if len(data) > MAX_BYTES:
                raise SessionRestoreError("session database exceeds size limit")
            return data


def pack_session(
    files: dict[str, bytes],
    *,
    harness: str,
    harness_version: str,
    session_id: str,
    thread_id: str,
    expires_at: datetime,
    now: datetime | None = None,
) -> bytes:
    """Create a bounded archive whose manifest commits to every included file."""
    now = now or datetime.now(UTC)
    if not files or len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
        raise SessionRestoreError("invalid session checkpoint size")
    if not all((harness_version, session_id, thread_id)) or expires_at <= now:
        raise SessionRestoreError("invalid session checkpoint identity or expiry")
    entries = [
        {"path": _safe_path(name), "size": len(data), "sha256": _digest(data)}
        for name, data in sorted(files.items())
    ]
    manifest = {
        "schema": SCHEMA,
        "harness": harness,
        "harness_version": harness_version,
        "session_id": session_id,
        "thread_id": thread_id,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "max_bytes": MAX_BYTES,
        "files": entries,
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        contents = {"manifest.json": json.dumps(manifest, sort_keys=True).encode()}
        contents.update({f"files/{name}": data for name, data in files.items()})
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def unpack_session(
    archive: bytes,
    *,
    harness: str,
    harness_version: str,
    session_id: str,
    thread_id: str,
    now: datetime | None = None,
) -> dict[str, bytes] | None:
    """Validate before extracting; expired state returns a visible cold handoff.

    Returns None only for expiry. Corruption, incompatible formats and identity
    mismatch raise SessionRestoreError, and no files are written by this method.
    Authorization of account and thread is additionally required by the caller.
    """
    now = now or datetime.now(UTC)
    if len(archive) > MAX_BYTES:
        raise SessionRestoreError("archive exceeds size limit")
    contents: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                name = _safe_path(member.name)
                total += member.size
                if (
                    not member.isfile()
                    or member.size < 0
                    or total > MAX_BYTES + 4 * 1024 * 1024
                    or len(contents) > MAX_FILES
                    or name in contents
                ):
                    raise SessionRestoreError("invalid archive member or size")
                stream = tar.extractfile(member)
                if stream is None:
                    raise SessionRestoreError("unreadable archive member")
                contents[name] = stream.read(member.size + 1)
        manifest = json.loads(contents.pop("manifest.json"))
        expected = {
            "schema": SCHEMA,
            "harness": harness,
            "harness_version": harness_version,
            "session_id": session_id,
            "thread_id": thread_id,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise SessionRestoreError("session identity or harness version mismatch")
        entries = manifest["files"]
        if not isinstance(entries, list) or not entries or len(entries) > MAX_FILES:
            raise SessionRestoreError("invalid manifest file list")
        result: dict[str, bytes] = {}
        for entry in entries:
            name = _safe_path(entry["path"])
            if name in result:
                raise SessionRestoreError("duplicate manifest file")
            data = contents.pop(f"files/{name}")
            if len(data) != entry["size"] or _digest(data) != entry["sha256"]:
                raise SessionRestoreError("session content digest mismatch")
            result[name] = data
        if contents or sum(map(len, result.values())) > MAX_BYTES:
            raise SessionRestoreError("unmanifested session content")
        # A matching manifest must not authorize an account-wide database.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            restore_session(result, root)
            selected = select_session_files(root, harness, session_id)
            if set(selected) != set(result):
                raise SessionRestoreError("unrelated session files in checkpoint")
            if harness == "opencode" and "opencode.db" in result:
                with sqlite3.connect(root / "opencode.db") as db:
                    for table in ("control_account", "permission", "session_share"):
                        exists = db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,),
                        ).fetchone()
                        if (
                            exists
                            and db.execute(
                                f'SELECT count(*) FROM "{table}"'
                            ).fetchone()[0]
                        ):
                            raise SessionRestoreError(
                                "credential or permission rows in checkpoint"
                            )
                    selected_path = root / "selected.db"
                    selected_path.write_bytes(selected["opencode.db"])
                    with sqlite3.connect(selected_path) as clean:
                        if set(db.execute("SELECT id FROM session")) != set(
                            clean.execute("SELECT id FROM session")
                        ):
                            raise SessionRestoreError("unrelated database sessions")
            elif selected != result:
                raise SessionRestoreError("unrelated session content")
        if datetime.fromisoformat(manifest["expires_at"]) <= now:
            return None
        return result
    except (
        KeyError,
        TypeError,
        ValueError,
        tarfile.TarError,
        OSError,
        sqlite3.Error,
    ) as exc:
        if isinstance(exc, SessionRestoreError):
            raise
        raise SessionRestoreError("corrupt session checkpoint") from exc


def restore_session(files: dict[str, bytes], destination: Path) -> None:
    """Restore validated files into an empty dedicated session directory."""
    if destination.is_symlink() or (
        destination.exists() and any(destination.iterdir())
    ):
        raise SessionRestoreError("session home must be empty")
    for name in files:
        _safe_path(name)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, data in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o600)
