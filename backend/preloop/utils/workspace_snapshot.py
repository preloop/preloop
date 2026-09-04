"""Shell builders for hosted workspace snapshots, restore and setup commands.

Three container-side concerns share this module because they all produce
POSIX-sh fragments that are spliced into the agent entrypoint script (Docker)
or into the artifact-emission wrapper (Kubernetes):

``build_workspace_snapshot_shell``
    Packs ``/workspace`` into a size-capped ``tar.gz`` at the end of a run so
    an execution that failed before its push can be restored. ``.git`` is kept
    whole (that is where the unpushed commits are) and a ``git bundle`` of
    every repository is written first as a second, independent copy of the
    commit graph, mirroring the evidence-pack recovery artifacts.

``build_setup_commands_shell``
    Runs ``git_clone_config.setup_commands`` after clone/restore and before
    the agent, with all output captured to ``/workspace/evidence/setup.log``
    and a machine-readable marker on failure so "setup broke" never reads as
    "the agent failed".

The size cap is enforced INSIDE the container: the tar stream is piped through
``head -c`` so an oversized workspace never writes more than the cap (plus one
byte) to disk and never travels to the control plane.
"""

from __future__ import annotations

import shlex
from typing import Iterable, Sequence

# Where the snapshot is built inside the container. Deliberately outside
# /workspace so the archive can never contain itself.
WORKSPACE_SNAPSHOT_PATH = "/tmp/preloop-workspace.tar.gz"

# Emitted (stdout) when the snapshot was dropped because it exceeded the cap.
WORKSPACE_SNAPSHOT_SKIPPED_MARKER = "PRELOOP_WORKSPACE_SNAPSHOT_SKIPPED"

# Directories that are always re-creatable from the repository plus the setup
# commands, and are the usual reason a workspace is measured in gigabytes.
# ``.git`` is NOT excluded: unpushed commits are the whole point.
WORKSPACE_SNAPSHOT_EXCLUDES: tuple[str, ...] = (
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".next",
    ".cache",
    "*.pyc",
)

# Where setup command output is captured (inside the evidence pack, so it is
# retrievable even when the snapshot itself was too large to keep).
SETUP_LOG_PATH = "/workspace/evidence/setup.log"

# Marker line printed when a setup command fails; classified as the
# ``setup_failed`` failure category.
SETUP_FAILED_MARKER = "PRELOOP_SETUP_FAILED"


def build_workspace_snapshot_shell(
    *,
    max_bytes: int,
    out_path: str = WORKSPACE_SNAPSHOT_PATH,
    excludes: Iterable[str] = WORKSPACE_SNAPSHOT_EXCLUDES,
) -> str:
    """Return sh that writes a capped tar.gz of /workspace to ``out_path``.

    Never fails the surrounding script: every step is best effort, and an
    oversized workspace removes the partial archive and prints
    ``PRELOOP_WORKSPACE_SNAPSHOT_SKIPPED`` with the observed size.
    """

    exclude_args = " ".join(f"--exclude={shlex.quote(pattern)}" for pattern in excludes)
    quoted_out = shlex.quote(out_path)
    limit = int(max_bytes)
    return f"""
_preloop_snapshot_workspace() {{
    [ -d /workspace ] || return 0
    for _pl_repo in /workspace /workspace/*; do
        if [ -d "$_pl_repo/.git" ]; then
            git -C "$_pl_repo" bundle create \
                "$_pl_repo/.git/preloop-workspace.bundle" --all \
                >/dev/null 2>&1 || true
        fi
    done
    rm -f {quoted_out}
    tar -czf - {exclude_args} -C / workspace 2>/dev/null \
        | head -c {limit + 1} > {quoted_out} 2>/dev/null
    _pl_wsize=$(wc -c < {quoted_out} 2>/dev/null | tr -d ' ')
    [ -n "$_pl_wsize" ] || _pl_wsize=0
    if [ "$_pl_wsize" -gt {limit} ] 2>/dev/null; then
        rm -f {quoted_out}
        echo "{WORKSPACE_SNAPSHOT_SKIPPED_MARKER} \
size_exceeds_limit limit={limit}"
        return 0
    fi
    if [ "$_pl_wsize" -eq 0 ] 2>/dev/null; then
        rm -f {quoted_out}
        echo "{WORKSPACE_SNAPSHOT_SKIPPED_MARKER} empty_or_failed"
    fi
    return 0
}}
_preloop_snapshot_workspace
"""


def build_setup_commands_shell(
    commands: Sequence[str], *, working_dir: str = "/workspace"
) -> str:
    """Return sh running ``commands`` with output captured to setup.log.

    The commands run in a subshell with ``set -e`` so the first failure stops
    the rest; the whole block then prints the setup marker, echoes the log
    tail so the failure is visible without downloading the evidence pack, and
    exits non-zero to fail the execution before the agent starts.
    """

    cleaned = [str(cmd).strip() for cmd in commands if str(cmd).strip()]
    if not cleaned:
        return ""

    body = "\n".join(f"    {cmd}" for cmd in cleaned)
    quoted_log = shlex.quote(SETUP_LOG_PATH)
    quoted_dir = shlex.quote(working_dir)
    return f"""
mkdir -p "$(dirname {quoted_log})" && \
{{
  echo "Running {len(cleaned)} setup command(s), output -> {SETUP_LOG_PATH}"
  (
    set -e
    cd {quoted_dir}
{body}
  ) > {quoted_log} 2>&1
  _pl_setup_rc=$?
  if [ "$_pl_setup_rc" -ne 0 ]; then
    echo "{SETUP_FAILED_MARKER} exit=$_pl_setup_rc"
    echo "Setup commands failed (exit $_pl_setup_rc). Last lines of \
{SETUP_LOG_PATH}:"
    tail -n 50 {quoted_log} 2>/dev/null || true
    exit "$_pl_setup_rc"
  fi
  echo "Setup commands completed"
}}
"""
