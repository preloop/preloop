"""Persist and restore native CLI agent sessions across correlated resumes.

A PR-comment restart of an issue-implementation flow used to be a cold agent:
the OpenCode/Codex session files lived in the container's data directory and
died with the emptyDir. This module is the shared plumbing for carrying a CLI
session across the restart:

Capture (container -> control plane)
    The agent script extracts the CLI session id of the run (OpenCode: a
    ``ses_...`` id picked out of the JSON event stream; Codex: the session
    uuid in the newest rollout filename) and prints it on stdout as

        PRELOOP_AGENT_SESSION <agent_type> <session_id>

    The orchestrator parses the marker from the live log stream (and, as a
    fallback, rescans the output summary on the terminal path) and stores
    ``{"agent_type": ..., "session_id": ...}`` on ``FlowExecution.cli_session``.

Packing (container -> workspace snapshot)
    The agent script copies the CLI session storage (OpenCode data dir, Codex
    ``sessions/``) into ``/workspace/.preloop-agent-session/<agent>/`` before
    the workspace snapshot is captured, so the existing snapshot machinery
    (persist, restore, TTL reaper) carries it for free. Credentials never
    travel: ``auth.json`` and log directories are excluded.

Restore (snapshot -> new container)
    On a correlated resume the workspace snapshot is unpacked into the fresh
    container (Docker pre-start restore), or the ``.preloop-agent-session``
    subtree is embedded into the pod script when the runner cannot seed the
    filesystem pre-start (Kubernetes emptyDir). The agent script then moves
    the storage back into the CLI's data directory and invokes the CLI resume
    flag for the recorded session id. If anything is missing, the resume args
    stay empty and the run starts cold — restore is strictly an optimization,
    never a failure mode.

The PR-comment correlation in ``flow_pr_binding`` remains the trigger: the
``_resume`` metadata it attaches to the trigger event carries the prior
execution's ``cli_session`` so the script builders know what to resume.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import shlex
import tarfile
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Stdout marker printed by the agent script once the CLI session id is known.
# Space-separated rather than JSON so the shell can emit it without quoting
# gymnastics: PRELOOP_AGENT_SESSION opencode ses_ab12cd34
AGENT_SESSION_MARKER = "PRELOOP_AGENT_SESSION"

# Where the agent script parks the CLI session storage inside /workspace so
# the workspace snapshot carries it to the next run. One subdirectory per
# agent type, matching the agent_type in the marker.
SESSION_PACK_ROOT = "/workspace/.preloop-agent-session"

# Cap for the session pack embedded verbatim into a Kubernetes pod script.
# The Kubernetes runner cannot seed the emptyDir before start, so the pack
# has to travel inside the script itself; anything past the cap is dropped
# and the resume falls back to a cold session.
MAX_EMBEDDED_SESSION_ARCHIVE_BYTES = 256 * 1024

# Defense-in-depth for extract_session_pack(): the workspace snapshot is
# written by this system, but it is still an archive parsed from the database.
MAX_SESSION_PACK_MEMBERS = 10_000
MAX_SESSION_PACK_UNCOMPRESSED_BYTES = 32 * 1024 * 1024

# Session ids are embedded in shell command lines, so both the marker parser
# and the per-agent resume builders validate them strictly. The per-agent
# patterns below are the ids the CLIs actually mint; anything else is treated
# as "no session" and the run starts cold.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_AGENT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
OPENCODE_SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]{4,}$")
CODEX_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

SESSION_ID_PATTERNS = {
    "opencode": OPENCODE_SESSION_ID_RE,
    "codex": CODEX_SESSION_ID_RE,
}


def parse_agent_session_marker(line: Optional[str]) -> Optional[Dict[str, str]]:
    """Parse ``PRELOOP_AGENT_SESSION <agent_type> <session_id>``.

    Returns ``{"agent_type": ..., "session_id": ...}`` or None when the line
    is not the marker or either field fails its validation pattern. The
    marker may be preceded by other text (the log filter can interleave).
    """

    if not line or not isinstance(line, str):
        return None
    idx = line.find(AGENT_SESSION_MARKER)
    if idx < 0:
        return None
    fields = line[idx + len(AGENT_SESSION_MARKER) :].split()
    if len(fields) < 2:
        return None
    agent_type, session_id = fields[0], fields[1]
    if not _AGENT_TYPE_RE.fullmatch(agent_type):
        return None
    if not _SESSION_ID_RE.fullmatch(session_id):
        return None
    return {"agent_type": agent_type, "session_id": session_id}


def valid_session_id(agent_type: str, session_id: str) -> bool:
    """Whether ``session_id`` is a well-formed id for ``agent_type``."""

    pattern = SESSION_ID_PATTERNS.get(agent_type)
    if pattern is None:
        return False
    return bool(pattern.fullmatch(session_id or ""))


def resume_cli_session(
    execution_context: Dict[str, Any], agent_type: str
) -> Optional[str]:
    """CLI session id this run should resume, or None to start cold.

    Reads ``trigger_event_data._resume.cli_session`` (attached by the PR
    comment correlation) and only returns ids that are valid for the agent
    type building the script, so a mismatched or malformed id can never
    reach a shell command line.
    """

    trigger_data = execution_context.get("trigger_event_data")
    if not isinstance(trigger_data, dict):
        return None
    resume = trigger_data.get("_resume")
    if not isinstance(resume, dict):
        return None
    cli_session = resume.get("cli_session")
    if not isinstance(cli_session, dict):
        return None
    if str(cli_session.get("agent_type") or "") != agent_type:
        return None
    session_id = str(cli_session.get("session_id") or "")
    if not valid_session_id(agent_type, session_id):
        logger.warning(
            "Ignoring malformed CLI session id for %s; starting a fresh session",
            agent_type,
        )
        return None
    return session_id


def build_session_archive_decode_shell(archive: bytes) -> str:
    """Shell that unpacks an embedded session pack into SESSION_PACK_ROOT.

    Used by runners that cannot write the filesystem before the entrypoint
    (Kubernetes emptyDir): the pack travels base64-encoded inside the script.
    Never fails the surrounding script.
    """

    encoded = base64.b64encode(archive).decode()
    quoted_root = shlex.quote(SESSION_PACK_ROOT)
    return (
        f"mkdir -p {quoted_root} 2>/dev/null || true\n"
        f"echo '{encoded}' | base64 -d | tar xzf - -C {quoted_root} 2>/dev/null || true"
    )


def build_session_restore_shell(agent_dir: str, data_dir_expr: str) -> str:
    """Shell that moves a packed CLI session back into the CLI data dir.

    Sets ``PRELOOP_CLI_SESSION_RESTORED=1`` when the pack existed and was
    moved. The workspace copy is only removed on success, so a failed copy
    stays visible instead of silently losing the session. Never fails the
    surrounding script.

    Args:
        agent_dir: Subdirectory of SESSION_PACK_ROOT for this agent type.
        data_dir_expr: Shell expression (already quoted) expanding to the
            CLI's data directory, e.g. ``'"$CODEX_HOME/sessions"'``.
    """

    quoted_src = shlex.quote(f"{SESSION_PACK_ROOT}/{agent_dir}")
    return f"""
PRELOOP_CLI_SESSION_RESTORED=0
if [ -d {quoted_src} ]; then
    _pl_session_dst={data_dir_expr}
    if mkdir -p "$_pl_session_dst" 2>/dev/null \\
        && (cd {quoted_src} && tar cf - . 2>/dev/null) \\
        | (cd "$_pl_session_dst" && tar xf - 2>/dev/null); then
        PRELOOP_CLI_SESSION_RESTORED=1
        rm -rf {quoted_src}
        echo "Restored CLI session storage into $_pl_session_dst"
    else
        echo "CLI session restore failed; continuing with a fresh session"
    fi
fi
"""


def build_session_pack_shell(
    agent_dir: str,
    data_dir_expr: str,
    excludes: tuple[str, ...] = ("auth.json", "log", "logs"),
) -> str:
    """Shell that copies the CLI data dir into SESSION_PACK_ROOT.

    Runs after the harness (and its in-place nudge) so the packed copy holds
    the whole conversation, and before the post-execution git block so it can
    never interfere with a push. Credentials and logs are excluded; the
    workspace snapshot's own size cap bounds the result. Never fails the
    surrounding script.

    Args:
        agent_dir: Subdirectory of SESSION_PACK_ROOT for this agent type.
        data_dir_expr: Shell expression (already quoted) expanding to the
            CLI's data directory.
        excludes: Entry names to skip while packing.
    """

    quoted_src = shlex.quote(f"{SESSION_PACK_ROOT}/{agent_dir}")
    exclude_args = " ".join(f"--exclude={shlex.quote(name)}" for name in excludes)
    return f"""
_pl_pack_cli_session() {{
    _pl_session_src={data_dir_expr}
    _pl_session_dst={quoted_src}
    [ -d "$_pl_session_src" ] || return 0
    mkdir -p "$_pl_session_dst" 2>/dev/null || return 0
    (cd "$_pl_session_src" && tar cf - {exclude_args} . 2>/dev/null) \\
        | (cd "$_pl_session_dst" && tar xf - 2>/dev/null) || true
}}
_pl_pack_cli_session
"""


def extract_session_pack(workspace_snapshot: bytes) -> Optional[bytes]:
    """Extract the packed CLI session subtree from a workspace snapshot.

    Takes the tar.gz stored on ``FlowExecution.workspace_snapshot`` and
    returns a tar.gz of just the ``.preloop-agent-session`` subtree (members
    relative to that root), ready for :func:`build_session_archive_decode_shell`.
    Returns None when the snapshot carries no pack or the extraction would be
    too large to embed in a pod script.
    """

    if not workspace_snapshot:
        return None
    # Snapshot members are "workspace/..." (tar -C / on the /workspace dir);
    # SESSION_PACK_ROOT without its leading slash already spells that prefix.
    prefix = SESSION_PACK_ROOT.strip("/") + "/"
    try:
        with tarfile.open(
            fileobj=io.BytesIO(workspace_snapshot), mode="r:gz"
        ) as snapshot:
            members = [
                member
                for member in snapshot.getmembers()
                if member.name.startswith(prefix)
            ]
    except (tarfile.TarError, OSError, EOFError) as e:
        logger.warning("Could not read workspace snapshot for session pack: %s", e)
        return None
    if not members:
        return None
    if len(members) > MAX_SESSION_PACK_MEMBERS:
        logger.warning("Session pack has %d members; refusing to embed", len(members))
        return None

    total = sum(member.size for member in members)
    if total > MAX_SESSION_PACK_UNCOMPRESSED_BYTES:
        logger.warning(
            "Session pack is %d bytes uncompressed; refusing to embed", total
        )
        return None

    out = io.BytesIO()
    try:
        with (
            tarfile.open(fileobj=io.BytesIO(workspace_snapshot), mode="r:gz") as src,
            tarfile.open(fileobj=out, mode="w:gz") as pack,
        ):
            for member in members:
                rel = member.name[len(prefix) :]
                if not rel or rel.startswith("/") or ".." in rel.split("/"):
                    continue
                if not (member.isfile() or member.isdir()):
                    continue
                packed = tarfile.TarInfo(name=rel)
                packed.type = member.type
                packed.mode = member.mode
                packed.mtime = member.mtime
                if member.isfile():
                    fileobj = src.extractfile(member)
                    if fileobj is None:
                        continue
                    data = fileobj.read()
                    packed.size = len(data)
                    pack.addfile(packed, io.BytesIO(data))
                else:
                    packed.size = 0
                    pack.addfile(packed)
    except (tarfile.TarError, OSError) as e:
        logger.warning("Could not build session pack from workspace snapshot: %s", e)
        return None

    archive = out.getvalue()
    if len(archive) > MAX_EMBEDDED_SESSION_ARCHIVE_BYTES:
        logger.info(
            "Session pack is %d bytes compressed; too large to embed in a pod "
            "script, resume will start a cold session",
            len(archive),
        )
        return None
    return archive
