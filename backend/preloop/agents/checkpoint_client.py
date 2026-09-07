"""Standalone stdlib runner checkpoint client (also injected into agent images).

No imports from Preloop: custom images need Python 3, git and the configured
harness only. Transport credentials are execution capabilities, never storage
credentials. Uploads commit only after the complete archive validates.
"""

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

WORKSPACE_ROOT = Path("/workspace")
EVIDENCE_REFERENCE_PATH = Path("/tmp/preloop-evidence-reference.json")
MAX_EVIDENCE_MEMBERS = 100_000
RESULT_JSON_MAX_BYTES = 256 * 1024
_READ_CHUNK = 65536

EXCLUDED = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".preloop-agent-session",
    ".preloop-native-session",
    ".preloop-checkpoint.json",
    ".ssh",
    ".aws",
    ".azure",
    ".git-credentials",
    ".netrc",
    "auth.json",
    "credentials.json",
}


def permitted(path: Path, root: Path) -> bool:
    """Keep source/git state while excluding credentials and reproducible caches."""
    parts = path.relative_to(root).parts
    return (
        not any(
            part in EXCLUDED
            or part == ".env"
            or part.startswith(".env.")
            or part.endswith((".pem", ".key"))
            for part in parts
        )
        and not path.is_symlink()
    )


def git_value(repo: Path, *args: str) -> str | None:
    """Read git identity without reporting remote credentials."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15
    )
    return result.stdout.strip() if result.returncode == 0 else None


def capture(root: Path, *, max_bytes: int) -> bytes:
    """Capture a stable file set, detecting concurrent writes before upload."""
    root = root.resolve()
    for repo in [root, *root.iterdir()]:
        git = repo / ".git"
        if git.is_dir() and any(
            (git / name).exists()
            for name in ("index.lock", "HEAD.lock", "shallow.lock", "config.lock")
        ):
            raise ValueError("checkpoint_workspace_busy")
    files: list[tuple[Path, os.stat_result]] = []
    for directory, subdirs, names in os.walk(root):
        parent = Path(directory)
        subdirs[:] = sorted(name for name in subdirs if permitted(parent / name, root))
        for name in sorted(names):
            path = parent / name
            if permitted(path, root) and path.is_file():
                files.append((path, path.stat()))
    repositories = []
    for repo in [root, *sorted(root.iterdir())]:
        if (repo / ".git").is_dir():
            repositories.append(
                {
                    "path": str(repo.relative_to(root)),
                    "branch": git_value(repo, "branch", "--show-current"),
                    "head_sha": git_value(repo, "rev-parse", "HEAD"),
                }
            )
    buffer = io.BytesIO()
    digest = hashlib.sha256()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, before in files:
            if path.name == "config" and path.parent.name == ".git":
                # Runtime remotes can embed clone credentials; recreate from
                # trusted repository configuration when resuming.
                continue
            data = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError("checkpoint_workspace_busy")
            relative = str(path.relative_to(root))
            digest.update(relative.encode() + b"\0" + hashlib.sha256(data).digest())
            info = tarfile.TarInfo("workspace/" + relative)
            info.size = len(data)
            info.mode = before.st_mode & 0o777
            archive.addfile(info, io.BytesIO(data))
            if buffer.tell() > max_bytes:
                raise ValueError("checkpoint_oversized")
        metadata = json.dumps(
            {
                "version": 1,
                "repositories": repositories,
                "file_state_sha256": digest.hexdigest(),
                "created_at": time.time(),
            }
        ).encode()
        info = tarfile.TarInfo("workspace/.preloop-checkpoint.json")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
    # Detect files changing between their individual capture and archive end.
    for path, before in files:
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("checkpoint_workspace_busy")
    final_paths: set[Path] = set()
    for directory, subdirs, names in os.walk(root):
        parent = Path(directory)
        subdirs[:] = [name for name in subdirs if permitted(parent / name, root)]
        final_paths.update(
            parent / name
            for name in names
            if permitted(parent / name, root) and (parent / name).is_file()
        )
    if final_paths != {path for path, _ in files}:
        raise ValueError("checkpoint_workspace_busy")
    body = buffer.getvalue()
    if len(body) > max_bytes:
        raise ValueError("checkpoint_oversized")
    return body


def _posix_member_name(relative: str) -> str:
    """Reject absolute names, parent traversal, and backslashes before packing."""
    posix = PurePosixPath(relative)
    if (
        posix.is_absolute()
        or ".." in posix.parts
        or "\\" in relative
        or not posix.parts
    ):
        raise ValueError("evidence_unsafe_path")
    return relative


def _lstat_regular(path: Path) -> os.stat_result:
    """Stat a member without following links; only regular files are packable."""
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ValueError("evidence_unsafe_member")
    return st


def _read_bounded(path: Path, *, expected: os.stat_result, limit: int) -> bytes:
    """Read at most ``limit`` bytes. Growth past the lstat size is a TOCTOU failure.

    ``expected.st_size`` is checked before any read so a huge or sparse file
    cannot be allocated into memory.
    """
    if expected.st_size > limit:
        raise ValueError("evidence_expansion_limit")
    data = bytearray()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_READ_CHUNK)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > expected.st_size:
                raise ValueError("evidence_busy")
            if len(data) > limit:
                raise ValueError("evidence_expansion_limit")
    after = path.lstat()
    if (
        after.st_size != expected.st_size
        or after.st_mtime_ns != expected.st_mtime_ns
        or len(data) != expected.st_size
    ):
        raise ValueError("evidence_busy")
    return bytes(data)


def pack_evidence(root: Path, *, max_bytes: int, max_expanded_bytes: int) -> bytes:
    """Pack /workspace/evidence and result.json with the same member rules as storage.

    Member caps, symlink rejection (including the evidence root), and expanded
    size are enforced from ``lstat`` before any file body is read.
    """
    root = root.resolve()
    evidence_dir = root / "evidence"
    result_path = root / "result.json"
    if evidence_dir.is_symlink() or (
        evidence_dir.exists() and not evidence_dir.is_dir()
    ):
        raise ValueError("evidence_unsafe_member")
    if result_path.is_symlink() or (result_path.exists() and not result_path.is_file()):
        raise ValueError("evidence_unsafe_member")
    if not evidence_dir.is_dir() and not result_path.is_file():
        raise ValueError("evidence_absent")
    pending: list[tuple[Path, str, os.stat_result, int, str]] = []
    expanded = 0

    def _queue(
        path: Path,
        relative: str,
        *,
        limit: int,
        oversized: str,
    ) -> None:
        nonlocal expanded
        if len(pending) >= MAX_EVIDENCE_MEMBERS:
            raise ValueError("evidence_invalid_members")
        name = _posix_member_name(relative)
        st = _lstat_regular(path)
        if st.st_size > limit:
            raise ValueError(oversized)
        if expanded + st.st_size > max_expanded_bytes:
            raise ValueError("evidence_expansion_limit")
        expanded += st.st_size
        pending.append((path, name, st, limit, oversized))

    if evidence_dir.is_dir():
        for directory, subdirs, names in os.walk(evidence_dir, followlinks=False):
            parent = Path(directory)
            kept: list[str] = []
            for name in sorted(subdirs):
                child = parent / name
                if child.is_symlink():
                    raise ValueError("evidence_unsafe_member")
                if not name.startswith(".") and child.is_dir():
                    kept.append(name)
            subdirs[:] = kept
            for name in sorted(names):
                path = parent / name
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError as exc:
                    raise ValueError("evidence_unsafe_path") from exc
                _queue(
                    path,
                    relative,
                    limit=max_expanded_bytes,
                    oversized="evidence_expansion_limit",
                )
    if result_path.is_file():
        _queue(
            result_path,
            "result.json",
            limit=RESULT_JSON_MAX_BYTES,
            oversized="evidence_result_oversized",
        )
    if not pending:
        raise ValueError("evidence_empty")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, relative, st, limit, oversized in pending:
            try:
                data = _read_bounded(path, expected=st, limit=limit)
            except ValueError as exc:
                if str(exc) == "evidence_expansion_limit":
                    raise ValueError(oversized) from exc
                raise
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = st.st_mode & 0o777
            archive.addfile(info, io.BytesIO(data))
            if buffer.tell() > max_bytes:
                raise ValueError("evidence_oversized")
    body = buffer.getvalue()
    if not body or len(body) > max_bytes:
        raise ValueError("evidence_oversized" if body else "evidence_empty")
    return body


def request(
    method: str, token: str, data: bytes | None = None, *, url: str | None = None
) -> bytes:
    """Use only the operator-provided endpoint and scoped capability."""
    target = url or os.environ["PRELOOP_CHECKPOINT_URL"]
    req = urllib.request.Request(
        url=target,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        limit = int(
            os.environ.get(
                "PRELOOP_EVIDENCE_MAX_BYTES",
                os.environ.get("PRELOOP_CHECKPOINT_MAX_BYTES", str(32 * 1024 * 1024)),
            )
        )
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("checkpoint_response_oversized")
        return body


def restore(body: bytes, destination: Path) -> None:
    """Validate and stage recovery before moving files into the workspace."""
    total = 0
    limit = int(
        os.environ.get("PRELOOP_CHECKPOINT_EXPANDED_MAX_BYTES", str(2 * 1024**3))
    )
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("checkpoint_destination_not_empty")
    # Stage on the destination volume: a Kubernetes emptyDir is a different
    # filesystem from the container overlay, so cross-volume rename fails.
    with tempfile.TemporaryDirectory(dir=destination) as staging:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            seen: set[str] = set()
            for member in archive:
                path = Path(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or not path.parts
                    or path.parts[0] != "workspace"
                    or not (member.isfile() or member.isdir())
                    or member.name in seen
                    or len(seen) >= 100_000
                ):
                    raise ValueError("checkpoint_unsafe_archive")
                seen.add(member.name)
                total += member.size
                if total > limit:
                    raise ValueError("checkpoint_expansion_limit")
                try:
                    archive.extract(member, path=staging, filter="data")
                except TypeError:
                    archive.extract(member, path=staging)
        staged = Path(staging) / "workspace"
        if not staged.is_dir():
            raise ValueError("checkpoint_missing_workspace")
        for path in staged.iterdir():
            path.rename(destination / path.name)


def main() -> None:
    """Run one capture or restore, reporting outcome without credentials."""
    import sys

    import fcntl

    # Serialize periodic, final and prepublication captures in this sandbox.
    with open("/tmp/preloop-checkpoint.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if sys.argv[1] == "restore":
                restore(
                    request("GET", os.environ["PRELOOP_CHECKPOINT_GET_TOKEN"]),
                    WORKSPACE_ROOT,
                )
                print("PRELOOP_CHECKPOINT restored", flush=True)
            elif sys.argv[1] == "evidence":
                # The marker file is agent-writable. Presence is not proof of
                # a server commit; always pack and PUT. The control plane
                # verifies account, execution, kind and digest.
                body = pack_evidence(
                    WORKSPACE_ROOT,
                    max_bytes=int(os.environ["PRELOOP_EVIDENCE_MAX_BYTES"]),
                    max_expanded_bytes=int(
                        os.environ.get(
                            "PRELOOP_EVIDENCE_EXPANDED_MAX_BYTES", str(2 * 1024**3)
                        )
                    ),
                )
                reference = json.loads(
                    request(
                        "PUT",
                        os.environ["PRELOOP_EVIDENCE_PUT_TOKEN"],
                        body,
                        url=os.environ["PRELOOP_EVIDENCE_URL"],
                    )
                )
                EVIDENCE_REFERENCE_PATH.write_text(json.dumps(reference))
                print(
                    "PRELOOP_EVIDENCE committed " + reference["artifact_id"],
                    flush=True,
                )
            else:
                body = capture(
                    WORKSPACE_ROOT,
                    max_bytes=int(os.environ["PRELOOP_CHECKPOINT_MAX_BYTES"]),
                )
                reference = json.loads(
                    request("PUT", os.environ["PRELOOP_CHECKPOINT_PUT_TOKEN"], body)
                )
                Path("/tmp/preloop-checkpoint-reference.json").write_text(
                    json.dumps(reference)
                )
                print(
                    "PRELOOP_CHECKPOINT committed " + reference["artifact_id"],
                    flush=True,
                )
        except Exception as exc:
            label = (
                "PRELOOP_EVIDENCE failed "
                if len(sys.argv) > 1 and sys.argv[1] == "evidence"
                else "PRELOOP_CHECKPOINT failed "
            )
            print(label + type(exc).__name__, flush=True)
            raise SystemExit(2 if str(exc) == "evidence_absent" else 1) from None


if __name__ == "__main__":
    main()
