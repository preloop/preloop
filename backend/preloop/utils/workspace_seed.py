"""Seed /workspace files from the trigger payload (``workspace_files``).

Design partners drive flows with webhook payloads that carry test fixtures.
Embedding those fixtures into the prompt via ``{{trigger_event.payload.*}}``
is brittle and expensive (large template expansions, token cost), so the
trigger payload may instead declare files to materialize in the agent's
``/workspace`` volume before the agent starts:

.. code-block:: json

    {
      "workspace_files": [
        {"path": "fixtures/input.json", "content_base64": "eyJrZXkiOiAi..."}
      ]
    }

v1 is inline-only (no URLs). Contents are base64 so arbitrary bytes survive
the JSON payload and the JSONB trigger snapshot. Validation is strict:
relative paths only (no ``..`` traversal, no absolute paths, no ``.git``
segment at any depth), a total size cap, and a file-count cap. The size cap
is enforced on the base64-ENCODED form — that is what the v1 transport
embeds into the container launch command, which on Kubernetes lives in the
Job spec and must stay well under etcd's ~1.5 MiB object limit alongside
the prompt and MCP config — and is deliberately at the low end (1 MiB
encoded, ~768 KiB decoded).

Lexical path validation cannot see symlinks in the cloned workspace, so the
emitted materialization block re-checks physical containment at runtime;
see :func:`build_workspace_seed_shell`.
"""

from __future__ import annotations

import base64
import binascii
import posixpath
import shlex
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

# Key on the trigger payload declaring files to seed.
WORKSPACE_FILES_KEY = "workspace_files"

# Reserved key under which the validated path list is stamped into
# ``FlowExecution.trigger_event_details`` for audit (same pattern as the
# ``_subject`` key attached by preloop.sync.event_normalizer).
WORKSPACE_FILE_PATHS_KEY = "_workspace_file_paths"

# Total base64-ENCODED bytes allowed across all seeded files. The encoded
# form (not the decoded content) is what travels inside the container launch
# command — on Kubernetes that lives in the Job spec, which must stay well
# under etcd's ~1.5 MiB object limit alongside the prompt and MCP config —
# so the cap is enforced on the encoded size directly. Decoded content is
# therefore capped at ~768 KiB (base64 is a 4/3 expansion).
MAX_TOTAL_SEED_ENCODED_BYTES = 1 * 1024 * 1024  # 1 MiB

# Bound on the number of files, to keep the init-command prelude sane.
MAX_SEED_FILES = 50

# Where files are written inside agent containers.
WORKSPACE_ROOT = "/workspace"


class WorkspaceSeedError(ValueError):
    """A ``workspace_files`` declaration is invalid; the execution must fail
    before any agent container is started."""


class WorkspaceSeedFile(BaseModel):
    """One validated file to materialize under ``/workspace``."""

    path: str
    content_base64: str

    @property
    def decoded_size(self) -> int:
        """Decoded size in bytes (content is validated strict base64)."""
        return len(base64.b64decode(self.content_base64, validate=True))


def _validate_path(raw_path: Any) -> str:
    """Validate one seed path; return it normalized. Relative paths only."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise WorkspaceSeedError("workspace_files entry has an empty 'path'")
    path = raw_path.strip()
    if "\x00" in path or "\n" in path or "\r" in path:
        raise WorkspaceSeedError(
            f"workspace_files path contains control characters: {path!r}"
        )
    if "\\" in path:
        # Reject backslashes outright rather than guessing at Windows
        # semantics; containers are POSIX and this closes `..\` traversal.
        raise WorkspaceSeedError(
            f"workspace_files path must use '/' separators: {path!r}"
        )
    if path.startswith("/") or path.startswith("~"):
        raise WorkspaceSeedError(
            f"workspace_files path must be relative to /workspace: {path!r}"
        )
    normalized = posixpath.normpath(path)
    if normalized == ".." or normalized.startswith("../"):
        raise WorkspaceSeedError(f"workspace_files path escapes /workspace: {path!r}")
    if normalized in (".", ""):
        raise WorkspaceSeedError(f"workspace_files path is not a file: {path!r}")
    if ".git" in normalized.split("/"):
        # Reject a `.git` segment at ANY depth, not just the first: flows may
        # clone repositories into sub-paths of /workspace (relative
        # clone_path), so a nested path like `client/.git/hooks/post-commit`
        # would land inside a real repository's git metadata and could plant
        # executable git hooks.
        raise WorkspaceSeedError(f"workspace_files path may not target .git: {path!r}")
    return normalized


def parse_workspace_files(
    payload: Optional[Dict[str, Any]],
) -> List[WorkspaceSeedFile]:
    """Parse and validate ``payload["workspace_files"]``.

    Returns an empty list when the key is absent. Raises
    :class:`WorkspaceSeedError` on any invalid declaration so the execution
    fails loudly before an agent is started.
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get(WORKSPACE_FILES_KEY)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkspaceSeedError("workspace_files must be a list of objects")
    if len(raw) > MAX_SEED_FILES:
        raise WorkspaceSeedError(
            f"workspace_files declares {len(raw)} files; max is {MAX_SEED_FILES}"
        )

    files: List[WorkspaceSeedFile] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise WorkspaceSeedError(
                f"workspace_files[{index}] must be an object with "
                "'path' and 'content_base64'"
            )
        path = _validate_path(entry.get("path"))
        if path in seen_paths:
            raise WorkspaceSeedError(f"workspace_files duplicates path: {path!r}")
        seen_paths.add(path)

        content = entry.get("content_base64")
        if not isinstance(content, str):
            raise WorkspaceSeedError(
                f"workspace_files[{index}] is missing 'content_base64' "
                "(URLs are not supported)"
            )
        content = "".join(content.split())  # tolerate wrapped base64

        # Cap the ENCODED size BEFORE decoding: the base64 text is what is
        # embedded in the container launch command (K8s Job spec / etcd
        # limit), so that is the size that matters for the transport, and
        # oversized payloads should be rejected without decode work.
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_SEED_ENCODED_BYTES:
            raise WorkspaceSeedError(
                "workspace_files total base64-encoded size exceeds the "
                f"{MAX_TOTAL_SEED_ENCODED_BYTES // (1024 * 1024)} MiB inline "
                "cap (the encoded form is embedded in the container launch "
                "command); use fewer/smaller files"
            )
        try:
            base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WorkspaceSeedError(
                f"workspace_files[{index}] content_base64 is not valid base64: {exc}"
            ) from exc
        files.append(WorkspaceSeedFile(path=path, content_base64=content))
    return files


def workspace_seed_paths(payload: Optional[Dict[str, Any]]) -> List[str]:
    """Validated seed paths for the audit trail on the execution record."""
    return [seed.path for seed in parse_workspace_files(payload)]


def attach_workspace_file_paths(
    trigger_details: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Stamp the validated seed path list onto the trigger snapshot for audit.

    Called at execution-creation time (alongside ``attach_trigger_subject``)
    so the execution record lists which paths were seeded without callers
    re-parsing base64 blobs. Mutates and returns the same dict. An invalid
    declaration is left unstamped here; the orchestrator rejects it with a
    user-visible error before any agent starts.
    """
    if not isinstance(trigger_details, dict):
        return trigger_details
    try:
        paths = workspace_seed_paths(trigger_details.get("payload"))
    except WorkspaceSeedError:
        return trigger_details
    if paths:
        trigger_details[WORKSPACE_FILE_PATHS_KEY] = paths
    return trigger_details


def build_workspace_seed_shell(
    files: List[WorkspaceSeedFile], workspace_root: str = WORKSPACE_ROOT
) -> str:
    """Build the init-command block that writes seeds under ``/workspace``.

    Runs after git clone (which relocates pre-existing files) and before
    custom commands (which may consume the seeds). Contents travel as quoted
    base64 so arbitrary bytes survive the shell.

    Path validation is lexical and cannot see the filesystem, but the seeds
    are materialized *after* git clone and a cloned repository may contain
    symlinks (e.g. ``fixtures -> /etc``). The emitted block therefore
    re-checks containment at runtime before every write:

    - the deepest existing ancestor of the target is resolved physically
      (``cd -P`` + ``pwd``, POSIX; agent images may lack GNU
      ``realpath -m``) and must resolve inside the workspace root, so
      writes cannot escape through a symlinked parent directory — and
      ``mkdir -p`` only runs after that check, so no directories are
      created outside the root either (components below the existing
      ancestor cannot be symlinks: they don't exist yet);
    - the target itself must not be a symlink (``-L``), so
      ``> target`` cannot write through a link left by the clone.

    The workspace root itself is canonicalized once at the top of the
    subshell (``cd -P`` + ``pwd``, falling back to the lexical path if the
    root does not exist yet), so containment is compared
    resolved-to-resolved. Without this, a root that is itself a symlink —
    e.g. an image where ``/workspace`` resolves through a link — would make
    every physically-resolved ancestor fail the lexical prefix match and
    abort all seeds spuriously.

    Any violation prints a diagnostic and aborts the init prelude (the
    block runs in a ``set -e`` subshell), failing the execution loudly.
    """
    if not files:
        return ""
    # One POSIX shell function, called once per seed. `$w` is the workspace
    # root; `$1` the validated relative path; `$2` the base64 content.
    helper = (
        "__pl_seed() { "
        't="$w/$1"; d="${t%/*}"; e="$d"; '
        'while [ ! -d "$e" ]; do e="${e%/*}"; '
        '[ -n "$e" ] || { echo "workspace_files: $w does not exist" >&2; exit 1; }; '
        "done; "
        'r="$(cd -P "$e" && pwd)"; '
        'case "$r" in "$w"|"$w"/*) ;; *) '
        'echo "workspace_files: $1 resolves outside $w (symlink escape)" >&2; '
        "exit 1;; esac; "
        'if [ -L "$t" ]; then '
        'echo "workspace_files: refusing to write through symlink: $1" >&2; '
        "exit 1; fi; "
        'mkdir -p "$d"; '
        'printf \'%s\' "$2" | base64 -d > "$t"; }'
    )
    calls = "; ".join(
        f"__pl_seed {shlex.quote(seed.path)} {shlex.quote(seed.content_base64)}"
        for seed in files
    )
    # Canonicalize the root once so the containment check compares
    # resolved-to-resolved; a symlinked root would otherwise fail the
    # prefix match for every seed. If the root does not exist yet, keep
    # the lexical path: the helper then fails with its own diagnostic.
    resolve_root = (
        f"w0={shlex.quote(workspace_root)}; "
        'w="$(cd -P "$w0" 2>/dev/null && pwd || printf \'%s\' "$w0")"'
    )
    return f"( set -e; {resolve_root}; {helper}; {calls} )"
