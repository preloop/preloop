"""Standalone stdlib runner checkpoint client (also injected into agent images).

No imports from Preloop: custom images need Python 3, git and the configured
harness only. Transport credentials are execution capabilities, never storage
credentials. Uploads commit only after the complete archive validates.
"""

import errno
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import BinaryIO

WORKSPACE_ROOT = Path("/workspace")
EVIDENCE_REFERENCE_PATH = Path("/tmp/preloop-evidence-reference.json")
MAX_EVIDENCE_MEMBERS = 100_000
RESULT_JSON_MAX_BYTES = 256 * 1024
_READ_CHUNK = 65536

# Self-description member written into every evidence pack. Kept in sync
# with preloop.cra.evidence_pack (this file cannot import it: it is
# installed alone inside agent containers). The control plane verifies the
# document this produces, so the shape is a contract, not a convention.
PACK_MANIFEST_NAME = "manifest.json"
PACK_MANIFEST_SCHEMA = "preloop.cra.evidence_manifest/v1"
PACK_MANIFEST_NOTE = (
    "sha256 values cover the members of this archive as packed and the "
    "inputs as delivered to the run. Source commits are declared by the "
    "caller, not attested by the platform."
)
# Facts the control plane knows and the container does not: which files
# were seeded into the workspace and which source the caller declared.
PACK_MANIFEST_ENV = "PRELOOP_EVIDENCE_MANIFEST"

SNAPSHOT_MODE_ENV = "PRELOOP_WORKSPACE_SNAPSHOTS"
EXPECTED_HEADS_ENV = "PRELOOP_CHECKPOINT_EXPECTED_HEADS"
CLONED_HEADS_ENV = "PRELOOP_CHECKPOINT_CLONED_HEADS"
CLEAN_CHECKOUT_MARKER = "PRELOOP_CHECKPOINT skipped clean_checkout"
NEVER_SNAPSHOT_MARKER = "PRELOOP_CHECKPOINT skipped workspace_snapshots_never"
_HEAD_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_EMPTY_FILE_STATE = hashlib.sha256().hexdigest()

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


def is_git_config(path: Path, root: Path) -> bool:
    """True for any git config file, including submodules and worktrees.

    A runtime remote URL can embed the clone credential. ``.git/config`` is
    the obvious copy; ``.git/modules/<name>/config`` (submodules) and
    ``.git/worktrees/<name>/config.worktree`` are the ones a checkpoint used
    to carry anyway. The trusted clone configuration recreates remotes on
    restore, so nothing of value is lost by dropping all of them.
    """
    if not (path.name == "config" or path.name.startswith("config.")):
        return False
    return ".git" in path.relative_to(root).parts


def checkpoint_base(repo: Path, root: Path) -> str | None:
    """Keep a base identity after credential-bearing Git config is redacted."""
    upstream = git_value(repo, "rev-parse", "@{upstream}")
    if upstream:
        return upstream
    try:
        prior = json.loads((root / ".preloop-checkpoint.json").read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        prior = {}
    repositories = prior.get("repositories", []) if isinstance(prior, dict) else []
    for record in repositories if isinstance(repositories, list) else []:
        if not isinstance(record, dict) or record.get("path") != str(
            repo.relative_to(root)
        ):
            continue
        base = record.get("base_sha")
        if (
            isinstance(base, str)
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base)
            and git_value(repo, "merge-base", "--is-ancestor", base, "HEAD") is not None
        ):
            return base
    # A new implementation branch has no upstream until its first push. The
    # clone's remote HEAD still identifies the base it branched from.
    return git_value(repo, "merge-base", "HEAD", "refs/remotes/origin/HEAD")


def snapshot_mode() -> str:
    """``always``, ``when_dirty`` (default), or ``never`` from the environment."""
    value = os.environ.get(SNAPSHOT_MODE_ENV, "when_dirty")
    if value in {"always", "when_dirty", "never"}:
        return value
    return "when_dirty"


def _normalize_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if _HEAD_SHA.fullmatch(lowered):
        return lowered
    return None


def _read_head_map(raw: str) -> dict[str, str]:
    """Parse a path-to-SHA map, dropping anything that is not a commit id."""
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    heads: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not key or key.startswith(("/", "\\")):
            continue
        if ".." in key.split("/"):
            continue
        sha = _normalize_sha(value)
        if sha:
            heads[key] = sha
    return heads


def expected_heads() -> dict[str, str]:
    """Commit recorded at clone time, else the control-plane fallback map.

    The file written after clone is the HEAD that was actually checked out.
    It wins over ``PRELOOP_CHECKPOINT_EXPECTED_HEADS``, which is only the
    trigger or pin SHA known before the clone.
    """
    recorded_path = os.environ.get(CLONED_HEADS_ENV)
    if recorded_path:
        try:
            recorded = _read_head_map(Path(recorded_path).read_text())
        except OSError:
            recorded = {}
        if recorded:
            return recorded
    raw = os.environ.get(EXPECTED_HEADS_ENV, "")
    if not raw:
        return {}
    return _read_head_map(raw)


def record_cloned_head(repo: Path, *, root: Path | None = None) -> None:
    """Remember one repository's HEAD relative to the workspace root."""
    workspace = (root or WORKSPACE_ROOT).resolve()
    checkout = repo.resolve()
    try:
        relative = (
            "." if checkout == workspace else str(checkout.relative_to(workspace))
        )
    except ValueError:
        return
    sha = _normalize_sha(git_value(checkout, "rev-parse", "HEAD"))
    if sha is None:
        return
    path = Path(os.environ.get(CLONED_HEADS_ENV, "/tmp/preloop-cloned-heads.json"))
    try:
        existing = _read_head_map(path.read_text())
    except OSError:
        existing = {}
    existing[relative] = sha
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, sort_keys=True))


def git_repos(root: Path) -> list[Path]:
    """The workspace root and its immediate children that are git checkouts."""
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    return [repo for repo in [root, *children] if (repo / ".git").is_dir()]


def repository_records(root: Path) -> list[dict[str, str | None]]:
    """Branch, head, and base for each checkout the snapshot would describe."""
    records: list[dict[str, str | None]] = []
    for repo in git_repos(root):
        records.append(
            {
                "path": str(repo.relative_to(root)),
                "branch": git_value(repo, "branch", "--show-current"),
                "head_sha": git_value(repo, "rev-parse", "HEAD"),
                "base_sha": checkpoint_base(repo, root),
            }
        )
    return records


def _git_bytes(repo: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _porcelain_paths(repo: Path) -> list[str] | None:
    """Paths from ``git status --porcelain``, or None when status cannot be read."""
    data = _git_bytes(repo, "status", "--porcelain=v1", "-z", "-uall")
    if data is None:
        return None
    paths: list[str] = []
    index = 0
    while index < len(data):
        if index + 3 > len(data):
            return None
        status = data[index : index + 2]
        start = index + 3
        end = data.find(b"\0", start)
        if end < 0:
            return None
        paths.append(data[start:end].decode("utf-8", "surrogateescape"))
        index = end + 1
        if status[:1] in b"RC":
            end = data.find(b"\0", index)
            if end < 0:
                return None
            paths.append(data[index:end].decode("utf-8", "surrogateescape"))
            index = end + 1
    return paths


def _tracked_paths(repo: Path) -> set[str] | None:
    data = _git_bytes(repo, "ls-files", "-z")
    if data is None:
        return None
    return {
        item.decode("utf-8", "surrogateescape") for item in data.split(b"\0") if item
    }


def _status_path_is_recoverable(repo: Path, root: Path, relative: str) -> bool:
    """True when a status path is one the full snapshot would have kept."""
    candidate = repo / relative
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    return permitted(candidate, root)


def _permitted_untracked(repo: Path, root: Path, tracked: set[str]) -> bool:
    """True when a file the snapshot would pack is not in the commit."""
    for directory, subdirs, names in os.walk(repo):
        parent = Path(directory)
        subdirs[:] = [
            name
            for name in subdirs
            if name != ".git" and permitted(parent / name, root)
        ]
        for name in names:
            path = parent / name
            if not permitted(path, root) or not path.is_file():
                continue
            if path.relative_to(repo).as_posix() not in tracked:
                return True
    return False


def _local_only_git_state(repo: Path) -> bool | None:
    """True when a stash or an unpushed branch commit would be lost on reclone.

    ``git status`` and the untracked walk both skip ``.git``, so those refs
    are invisible to the worktree check. None means git could not answer,
    which is treated as not clean.
    """
    stash = _git_bytes(repo, "stash", "list")
    if stash is None:
        return None
    if stash.strip():
        return True
    unpushed = _git_bytes(
        repo, "log", "--branches", "--not", "--remotes", "-1", "--format=%H"
    )
    if unpushed is None:
        return None
    return bool(unpushed.strip())


def repo_is_clean(repo: Path, root: Path, expected: str | None) -> bool:
    """True when this checkout matches the cloned commit and has nothing extra."""
    head = _normalize_sha(git_value(repo, "rev-parse", "HEAD"))
    if not expected or head != expected:
        return False
    local_only = _local_only_git_state(repo)
    if local_only is None or local_only:
        return False
    paths = _porcelain_paths(repo)
    tracked = _tracked_paths(repo)
    if paths is None or tracked is None:
        return False
    if any(_status_path_is_recoverable(repo, root, relative) for relative in paths):
        return False
    return not _permitted_untracked(repo, root, tracked)


def checkout_is_clean(root: Path) -> bool:
    """True when every checkout is clean at the commit that was cloned.

    No recorded SHA, or no git checkout at all, is not clean: there is nothing
    to prove the code host already has.
    """
    repos = git_repos(root)
    if not repos:
        return False
    heads = expected_heads()
    return all(
        repo_is_clean(repo, root, heads.get(str(repo.relative_to(root))))
        for repo in repos
    )


def _metadata_document(root: Path, *, digest: str, metadata_only: bool) -> bytes:
    document: dict[str, object] = {
        "version": 1,
        "repositories": repository_records(root),
        "file_state_sha256": digest,
        "created_at": time.time(),
    }
    if metadata_only:
        document["metadata_only"] = True
    return json.dumps(document).encode()


def _pack_metadata_member(metadata: bytes, *, max_bytes: int) -> bytes:
    buffer = _CheckpointBuffer(max_bytes)
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("workspace/.preloop-checkpoint.json")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
    body = buffer.getvalue()
    if len(body) > max_bytes:
        raise ValueError("checkpoint_oversized")
    return body


def archive_is_metadata_only(body: bytes) -> bool:
    """True when the archive carries the checkpoint document and no files."""
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            files = [member for member in archive.getmembers() if member.isfile()]
            if len(files) != 1 or files[0].name != "workspace/.preloop-checkpoint.json":
                return False
            source = archive.extractfile(files[0])
            if source is None:
                return False
            document = json.loads(source.read().decode())
    except (tarfile.TarError, OSError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(document, dict) and document.get("metadata_only") is True


class _CheckpointBuffer:
    """Enforce the compressed cap while writing, even within a large member."""

    def __init__(self, limit: int) -> None:
        self.buffer = io.BytesIO()
        self.limit = limit

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.limit:
            raise ValueError("checkpoint_oversized")
        return self.buffer.write(data)

    def tell(self) -> int:
        return self.buffer.tell()

    def getvalue(self) -> bytes:
        return self.buffer.getvalue()

    def read(self, size: int = -1) -> bytes:
        return self.buffer.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.buffer.seek(offset, whence)

    def close(self) -> None:
        self.buffer.close()


class _CheckpointReader:
    """Hash file chunks as tarfile streams them, without loading the member."""

    def __init__(self, source: BinaryIO, expected_size: int) -> None:
        self.source = source
        self.remaining = expected_size
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        requested = self.remaining if size < 0 else min(size, self.remaining)
        data = self.source.read(requested)
        if len(data) != requested:
            raise ValueError("checkpoint_workspace_busy")
        self.remaining -= len(data)
        self.digest.update(data)
        return data


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
    mode = snapshot_mode()
    if mode != "always" and checkout_is_clean(root):
        metadata = _metadata_document(
            root, digest=_EMPTY_FILE_STATE, metadata_only=True
        )
        # A write between the two checks must not skip a now-dirty tree.
        if checkout_is_clean(root):
            return _pack_metadata_member(metadata, max_bytes=max_bytes)
    files: list[tuple[Path, os.stat_result]] = []
    for directory, subdirs, names in os.walk(root):
        parent = Path(directory)
        subdirs[:] = sorted(name for name in subdirs if permitted(parent / name, root))
        for name in sorted(names):
            path = parent / name
            if permitted(path, root) and path.is_file():
                files.append((path, path.stat()))
    # base_sha is the commit unpushed work sits on. Without it a reader
    # cannot tell a checkpoint that is only dirty from one that also
    # carries commits the remote never saw.
    repositories = repository_records(root)
    buffer = _CheckpointBuffer(max_bytes)
    digest = hashlib.sha256()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, before in files:
            if is_git_config(path, root):
                # Runtime remotes can embed clone credentials; recreate from
                # trusted repository configuration when resuming.
                continue
            relative = str(path.relative_to(root))
            info = tarfile.TarInfo("workspace/" + relative)
            info.size = before.st_size
            info.mode = before.st_mode & 0o777
            try:
                with path.open("rb") as source:
                    reader = _CheckpointReader(source, before.st_size)
                    archive.addfile(info, reader)
                after = path.stat()
            except FileNotFoundError:
                raise ValueError("checkpoint_workspace_busy") from None
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError("checkpoint_workspace_busy")
            # Restore applies the mode, so a chmod-only change is a new state.
            digest.update(
                relative.encode()
                + b"\0"
                + oct(info.mode).encode()
                + b"\0"
                + reader.digest.digest()
            )
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
        try:
            after = path.stat()
        except FileNotFoundError:
            raise ValueError("checkpoint_workspace_busy") from None
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
    """Read at most ``limit`` bytes from the same inode ``expected`` named.

    ``expected.st_size`` is checked before any read so a huge or sparse file
    cannot be allocated into memory. The path is opened with ``O_NOFOLLOW``
    and the fd is ``fstat``ed so a substituted symlink cannot be followed.
    """
    if expected.st_size > limit:
        raise ValueError("evidence_expansion_limit")
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if getattr(exc, "errno", None) in {errno.ELOOP, errno.EMLINK}:
            raise ValueError("evidence_unsafe_member") from exc
        raise ValueError("evidence_busy") from exc
    try:
        observed = os.fstat(fd)
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            raise ValueError("evidence_unsafe_member")
        if (
            observed.st_ino != expected.st_ino
            or observed.st_dev != expected.st_dev
            or observed.st_size != expected.st_size
            or observed.st_mtime_ns != expected.st_mtime_ns
        ):
            raise ValueError("evidence_busy")
        data = bytearray()
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > expected.st_size or len(data) > limit:
                raise ValueError(
                    "evidence_expansion_limit" if len(data) > limit else "evidence_busy"
                )
        if len(data) != expected.st_size:
            raise ValueError("evidence_busy")
        return bytes(data)
    finally:
        os.close(fd)


def _canonical_json(value):
    """UTF-8 JSON with sorted keys and no insignificant whitespace."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def _manifest_context() -> dict:
    """Read the control-plane facts for the manifest; {} when unavailable."""
    raw = os.environ.get(PACK_MANIFEST_ENV)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pack_manifest(members: list) -> bytes:
    """Build manifest.json for the members just written."""
    ordered = sorted(members, key=lambda item: item["name"])
    context = _manifest_context()
    inputs = context.get("inputs")
    source = context.get("source")
    return _canonical_json(
        {
            "schema": PACK_MANIFEST_SCHEMA,
            "execution_id": context.get("execution_id"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "members": ordered,
            "members_digest": hashlib.sha256(_canonical_json(ordered)).hexdigest(),
            "inputs": inputs if isinstance(inputs, list) else [],
            "source": source if isinstance(source, dict) else {},
            "note": PACK_MANIFEST_NOTE,
        }
    )


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
        # One slot is reserved for manifest.json, which is written last and
        # counts against the same server-side member cap.
        if len(pending) >= MAX_EVIDENCE_MEMBERS - 1:
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
    members: list = []
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
            members.append(
                {
                    "name": relative,
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
            if buffer.tell() > max_bytes:
                raise ValueError("evidence_oversized")
        # Written last so the pack describes exactly what it carries: the
        # digests above are of the bytes that were actually packed.
        manifest = _pack_manifest(members)
        info = tarfile.TarInfo(PACK_MANIFEST_NAME)
        info.size = len(manifest)
        info.mode = 0o600
        info.mtime = 0
        archive.addfile(info, io.BytesIO(manifest))
        if buffer.tell() > max_bytes:
            raise ValueError("evidence_oversized")
    body = buffer.getvalue()
    if not body or len(body) > max_bytes:
        raise ValueError("evidence_oversized" if body else "evidence_empty")
    return body


def response_read_limit(*, url: str | None = None) -> int:
    """Bound this HTTP response by the operation that owns the URL.

    Evidence and workspace checkpoints share ``request()`` but not a size
    cap. Preferring ``PRELOOP_EVIDENCE_MAX_BYTES`` (32 MiB) would truncate a
    legal checkpoint restore once both variables are set.
    """
    default = 32 * 1024 * 1024
    evidence_url = os.environ.get("PRELOOP_EVIDENCE_URL")
    if url is not None and evidence_url and url == evidence_url:
        return int(os.environ.get("PRELOOP_EVIDENCE_MAX_BYTES", str(default)))
    return int(os.environ.get("PRELOOP_CHECKPOINT_MAX_BYTES", str(default)))


def request(
    method: str,
    token: str,
    data: bytes | None = None,
    *,
    url: str | None = None,
    max_bytes: int | None = None,
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
        limit = max_bytes if max_bytes is not None else response_read_limit(url=target)
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("checkpoint_response_oversized")
        return body


HTTP_ERROR_BODY_LIMIT = 4096
_REASON_PATTERN = re.compile(r"[a-z0-9_]{1,64}")


QUOTA_MARKER_FIELDS = (
    ("retained", "retained_bytes"),
    ("quota", "quota_bytes"),
    ("incoming", "incoming_bytes"),
)


def http_error_details(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """Return ``(reason, extra)`` from an HTTP error body, never the body.

    The API answers with ``{"detail": "<code>"}`` or, for the capability
    check, ``{"detail": {"error": "<code>", ...}}``. Anything else (an HTML
    page from a proxy in front of the API, an empty body, free text) is
    reported as a fixed category so the marker still says whether the API
    itself answered. At most ``HTTP_ERROR_BODY_LIMIT`` bytes are read.

    ``extra`` is `` retained=<n> quota=<n> incoming=<n>`` when a quota
    refusal carries all three byte totals (#1339), else ''. Only integers
    are echoed, so nothing else from the body reaches the log.
    """
    try:
        raw = exc.read(HTTP_ERROR_BODY_LIMIT + 1) if exc.fp is not None else b""
    except Exception:
        return "unreadable", ""
    if not raw:
        return "empty", ""
    if len(raw) > HTTP_ERROR_BODY_LIMIT:
        return "not_json", ""
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return "not_json", ""
    candidates = []
    extra = ""
    if isinstance(document, dict):
        detail = document.get("detail")
        if isinstance(detail, dict):
            candidates.append(detail.get("error"))
            values = [detail.get(field) for _, field in QUOTA_MARKER_FIELDS]
            if all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in values
            ):
                extra = "".join(
                    " " + label + "=" + str(value)
                    for (label, _), value in zip(
                        QUOTA_MARKER_FIELDS, values, strict=False
                    )
                )
        candidates.extend([detail, document.get("error")])
    for candidate in candidates:
        if isinstance(candidate, str) and _REASON_PATTERN.fullmatch(candidate):
            return candidate, extra
    return "unrecognized", ""


def http_error_suffix(exc: Exception, operation: str) -> str:
    """`` status=<code> detail=<reason> op=<operation>`` for HTTP errors, else ''.

    A quota refusal appends `` retained=<n> quota=<n> incoming=<n>``.
    """
    if not isinstance(exc, urllib.error.HTTPError):
        return ""
    reason, extra = http_error_details(exc)
    return (
        " status="
        + str(int(exc.code))
        + " detail="
        + reason
        + " op="
        + operation
        + extra
    )


CHECKPOINT_METADATA_NAME = ".preloop-checkpoint.json"


def restored_age_report(metadata: dict) -> str:
    """Say how old the restored checkpoint is, in the restore log line.

    Recovery is "the last complete checkpoint", not "everything the agent
    ever wrote". The age is the size of the window whose writes were lost,
    so it is reported rather than left for the reader to infer. An
    unreadable or absent ``created_at`` reports ``unknown``, never zero.
    """
    created = metadata.get("created_at") if isinstance(metadata, dict) else None
    try:
        age = int(max(0.0, time.time() - float(created)))
    except (TypeError, ValueError):
        return "age_seconds=unknown created_at=unknown"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(created)))
    return "age_seconds=" + str(age) + " created_at=" + stamp


def restore(body: bytes, destination: Path) -> dict:
    """Validate and stage recovery before moving files into the workspace.

    Returns the checkpoint's own metadata document so the caller can report
    what was recovered and how old it is. ``{}`` when the archive predates
    the metadata member or carries an unreadable one.
    """
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
        metadata: dict = {}
        document = staged / CHECKPOINT_METADATA_NAME
        if document.is_file():
            try:
                parsed = json.loads(document.read_text())
            except (ValueError, OSError, UnicodeDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                metadata = parsed
        for path in staged.iterdir():
            path.rename(destination / path.name)
        return metadata


def main() -> None:
    """Run one capture or restore, reporting outcome without credentials."""
    import sys

    import fcntl

    if len(sys.argv) > 2 and sys.argv[1] == "record-head":
        try:
            record_cloned_head(Path(sys.argv[2]))
        except OSError:
            return
        return

    # Serialize periodic, final and prepublication captures in this sandbox.
    with open("/tmp/preloop-checkpoint.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if sys.argv[1] == "restore":
                metadata = restore(
                    request("GET", os.environ["PRELOOP_CHECKPOINT_GET_TOKEN"]),
                    WORKSPACE_ROOT,
                )
                print(
                    "PRELOOP_CHECKPOINT restored " + restored_age_report(metadata),
                    flush=True,
                )
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
                if snapshot_mode() == "never":
                    # The shell also skips the call. This is the backstop when
                    # capture is invoked directly.
                    print(NEVER_SNAPSHOT_MARKER, flush=True)
                    return
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
                if archive_is_metadata_only(body):
                    print(CLEAN_CHECKOUT_MARKER, flush=True)
                    return
                # The server keeps one copy of an unchanged workspace (#1339).
                print(
                    "PRELOOP_CHECKPOINT committed "
                    + reference["artifact_id"]
                    + (
                        " deduplicated" if reference.get("deduplicated") is True else ""
                    ),
                    flush=True,
                )
        except Exception as exc:
            if len(sys.argv) > 1 and sys.argv[1] == "evidence":
                if str(exc) == "evidence_absent":
                    print("PRELOOP_EVIDENCE absent", flush=True)
                    raise SystemExit(2) from None
                print(
                    "PRELOOP_EVIDENCE failed "
                    + type(exc).__name__
                    + http_error_suffix(exc, "evidence"),
                    flush=True,
                )
                raise SystemExit(1) from None
            # The cap is a storage limit, not a failed review. The legacy
            # snapshot path prints a skip and returns 0; a direct upload that
            # cannot fit must do the same or prepublication exits 1 after the
            # agent has already finished.
            if str(exc) == "checkpoint_oversized":
                print(
                    "PRELOOP_CHECKPOINT skipped checkpoint_oversized",
                    flush=True,
                )
                return
            operation = (
                "restore"
                if len(sys.argv) > 1 and sys.argv[1] == "restore"
                else "capture"
            )
            if isinstance(exc, urllib.error.HTTPError):
                # "HTTP Error 413: ..." never matched the reason pattern, so
                # the status and the server's reason were both dropped (#1331).
                detail = http_error_suffix(exc, operation)
            else:
                reason = str(exc)
                detail = " " + reason if re.fullmatch(r"[a-z0-9_]+", reason) else ""
            print(
                "PRELOOP_CHECKPOINT failed " + type(exc).__name__ + detail,
                flush=True,
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
