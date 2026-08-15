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
relative paths only (no ``..`` traversal, no absolute paths), a total decoded
size cap, and a file-count cap. The cap is deliberately at the low end
(1 MiB) because the v1 transport embeds base64 into the container launch
command, which on Kubernetes lives in the Job spec and must stay well under
etcd's ~1.5 MiB object limit alongside the prompt and MCP config.
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

# Total decoded bytes allowed across all seeded files.
MAX_TOTAL_SEED_BYTES = 1 * 1024 * 1024  # 1 MiB

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
    if normalized == ".git" or normalized.startswith(".git/"):
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
        try:
            decoded = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WorkspaceSeedError(
                f"workspace_files[{index}] content_base64 is not valid base64: {exc}"
            ) from exc

        total_bytes += len(decoded)
        if total_bytes > MAX_TOTAL_SEED_BYTES:
            raise WorkspaceSeedError(
                "workspace_files total decoded size exceeds the "
                f"{MAX_TOTAL_SEED_BYTES // (1024 * 1024)} MiB inline cap; "
                "use fewer/smaller files"
            )
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


def build_workspace_seed_shell(files: List[WorkspaceSeedFile]) -> str:
    """Build the init-command block that writes seeds under ``/workspace``.

    Runs after git clone (which relocates pre-existing files) and before
    custom commands (which may consume the seeds). Contents travel as quoted
    base64 so arbitrary bytes survive the shell.
    """
    if not files:
        return ""
    commands: List[str] = []
    for seed in files:
        target = f"{WORKSPACE_ROOT}/{seed.path}"
        parent = posixpath.dirname(target)
        commands.append(f"mkdir -p {shlex.quote(parent)}")
        commands.append(
            f"printf '%s' {shlex.quote(seed.content_base64)} "
            f"| base64 -d > {shlex.quote(target)}"
        )
    return " && ".join(commands)
