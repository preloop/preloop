"""Runtime session lineage for runs one agent starts from a shell (#1045).

A conductor agent that spawns workers with ``claude -p ... &`` knows an OS
pid and nothing else. The harness puts no lineage on the wire for a child
started from a shell, so the child's ``runtime_session`` had no
``parent_session_id`` and the default ``send_note`` scope (descendants)
reached nothing.

This module closes that gap with three pieces, all account scoped:

* :func:`register_session_start` is what an agent's SessionStart hook calls.
  It creates (or finds) the runtime session the gateway and the permission
  hook already key on, records the parent the hook read from
  ``PRELOOP_PARENT_SESSION_ID``, and fills the list fields that make two
  same-second sessions distinguishable.
* :func:`resolve_external_session` maps the harness's own session id (the
  Claude Code ``session_id``, the transcript file name) to the runtime
  session that carries it, so a spawner that only has that id can still name
  the target.
* :func:`caller_session_ids` and :func:`session_descends_from` give the note
  scope a session lineage to walk beside the execution lineage it already
  had.

Lineage is a claim the child makes about itself. Claiming a parent widens
only what the claimed parent may do to the claimer, never what the claimer
may do to anyone else, so an agent gains nothing by lying about it. The
parent must still live in the same account, and the column stays write-once.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_flow_runner,
    crud_runner_remote_session,
    crud_runtime_session,
)
from preloop.models.models.runtime_session import RuntimeSession
from preloop.services.agent_session_headers import normalize_session_id

logger = logging.getLogger(__name__)

#: Longest title derived from a first prompt.
TITLE_MAX_CHARS = 120

#: Bound on the parent walk; a cycle written by a bug must not spin here.
MAX_SESSION_ANCESTOR_WALK = 32

#: Request headers that carry the calling harness's own session id on an MCP
#: call. Claude Code stamps ``X-Mcp-Client-Session-Id`` on its MCP requests
#: with the same id it sends on model requests as
#: ``X-Claude-Code-Session-Id``. Both are vendor specific, and both are read
#: only to find a session the caller's own credential already owns, so a
#: forged value can at most point at another run of the same principal.
CALLER_SESSION_HEADERS = ("x-mcp-client-session-id", "x-claude-code-session-id")


@dataclass(frozen=True)
class SessionStartResult:
    """What a SessionStart hook is told about the session it registered."""

    runtime_session_id: str
    parent_session_id: Optional[str]
    started_at: datetime
    created: bool
    alias: Optional[str] = None


def as_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def session_source_id_for(principal_id: str, external_session_id: str) -> str:
    """The source key the gateway uses for one harness conversation."""
    return f"{principal_id}:{external_session_id}"


def title_from_prompt(prompt: Optional[str]) -> Optional[str]:
    """A list title from a first prompt: redacted, one line, 120 characters."""
    from preloop.services.session_search_index import redact_text

    if not isinstance(prompt, str):
        return None
    collapsed = " ".join(prompt.split())
    if not collapsed:
        return None
    redacted, _ = redact_text(collapsed)
    if len(redacted) <= TITLE_MAX_CHARS:
        return redacted
    return redacted[: TITLE_MAX_CHARS - 3].rstrip() + "..."


def principal_label(agent_kind: Optional[str], cwd: Optional[str]) -> Optional[str]:
    """``<agent kind> in <cwd basename>``, the label a list shows for a run."""
    kind = (agent_kind or "").strip()
    base = os.path.basename((cwd or "").rstrip("/\\")) if cwd else ""
    if kind and base:
        return f"{kind} in {base}"[:255]
    return (kind or base)[:255] or None


def resolve_external_session(
    db: Session,
    *,
    account_id: Any,
    external_session_id: str,
    principal_type: Optional[str] = None,
    principal_id: Optional[str] = None,
) -> List[RuntimeSession]:
    """Runtime sessions in the account that carry this harness session id.

    The gateway keys a natively identified conversation as
    ``<principal id>:<external id>`` and the usage importers key it as the
    bare id, so both shapes match. When the caller's own principal is known,
    an exact match on its key wins and is returned alone: that is the session
    the caller's credential created. Otherwise every match is returned and the
    caller decides what more than one means (``send_note`` refuses).

    Returns:
        Matching sessions, newest first. Empty when the id is malformed.
    """
    normalized = normalize_session_id(external_session_id)
    if normalized is None:
        return []
    hosted = runner_session_for_external(
        db, account_id=account_id, external_session_id=normalized
    )
    if hosted is not None:
        return [hosted]
    if principal_type and principal_id:
        exact = crud_runtime_session.get_by_source(
            db,
            account_id=account_id,
            session_source_type=principal_type,
            session_source_id=session_source_id_for(principal_id, normalized),
        )
        if exact is not None:
            return [exact]
    return _sessions_carrying(db, account_id=account_id, external=normalized)


def _sessions_carrying(
    db: Session, *, account_id: Any, external: str
) -> List[RuntimeSession]:
    """Sessions keyed by this harness id, bare or principal-prefixed."""
    normalized = external
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return (
        db.query(RuntimeSession)
        .filter(
            RuntimeSession.account_id == account_id,
            or_(
                RuntimeSession.session_source_id == normalized,
                RuntimeSession.session_source_id.like(f"%:{escaped}", escape="\\"),
            ),
        )
        .order_by(RuntimeSession.started_at.desc())
        .limit(5)
        .all()
    )


def runner_session_for_external(
    db: Session,
    *,
    account_id: Any,
    external_session_id: Any,
    actor_user_id: Any = None,
) -> Optional[RuntimeSession]:
    """The runner session's runtime session when a live one hosts this id.

    A runner-hosted harness (#1482) runs under a session id the runner
    minted and reported as ``harness_session_id``. The harness's own hooks
    know only that id, so their registrations, approvals and notes are
    redirected here instead of opening a second session. Account scoped,
    and ended runner sessions never match. When the caller's user is known
    (``actor_user_id``) it must be the user who started the runner session or
    the user who registered the hosting runner: an admin may start a session
    on a member's runner (#1483), and the hooks there run as the member.
    """
    external = normalize_session_id(external_session_id)
    if external is None:
        return None
    runner = crud_runner_remote_session.get_live_by_harness_session(
        db, account_id=account_id, harness_session_id=external
    )
    if runner is None:
        return None
    if actor_user_id is not None and not _may_join_runner_session(
        db, account_id=account_id, runner=runner, user_id=actor_user_id
    ):
        return None
    return crud_runtime_session.get_account_session(
        db, account_id=account_id, runtime_session_id=str(runner.runtime_session_id)
    )


def _may_join_runner_session(
    db: Session, *, account_id: Any, runner: Any, user_id: Any
) -> bool:
    """Whether this user's hook may attach to the runner session."""
    if str(runner.actor_user_id) == str(user_id):
        return True
    host = crud_flow_runner.get(db, id=runner.runner_id)
    return (
        host is not None
        and str(host.account_id) == str(account_id)
        and str(host.registered_by_user_id) == str(user_id)
    )


def fold_hook_sessions_into_runner(
    db: Session, *, account_id: Any, external_session_id: Any, target: RuntimeSession
) -> int:
    """Fold sessions a hook opened under this harness id into ``target``.

    The hook can register before the runner's ``harness_session_id`` reaches
    the server. Whatever it recorded meanwhile moves to the runner session
    and the duplicate row is removed. Flushes; the caller commits.

    Returns:
        How many sessions were folded.
    """
    external = normalize_session_id(external_session_id)
    if external is None:
        return 0
    if not target.session_reference:
        # Set here so it does not depend on whether the hook or the runner's
        # state frame arrived first.
        target.session_reference = external[:255]
        db.add(target)
    folded = 0
    for session in _sessions_carrying(db, account_id=account_id, external=external):
        if session.id == target.id:
            continue
        if crud_runtime_session.fold_into(
            db,
            account_id=account_id,
            source_session_id=session.id,
            target_session_id=target.id,
        ):
            folded += 1
    return folded


def session_ancestor_chain(
    db: Session, *, account_id: Any, runtime_session_id: Any
) -> List[uuid.UUID]:
    """The session and its parents, nearest first, inside one account."""
    current = as_uuid(runtime_session_id)
    chain: List[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    while current is not None and len(chain) < MAX_SESSION_ANCESTOR_WALK:
        if current in seen:
            logger.warning("Runtime session lineage cycle at %s", current)
            break
        session = crud_runtime_session.get_account_session(
            db, account_id=account_id, runtime_session_id=current
        )
        if session is None:
            break
        seen.add(current)
        chain.append(current)
        current = as_uuid(session.parent_session_id)
    return chain


def session_descends_from(
    db: Session,
    *,
    account_id: Any,
    target_session_id: Any,
    ancestor_session_ids: Iterable[Any],
) -> bool:
    """Whether the target was started, at any depth, by one of these sessions.

    The target itself does not count: a session is not its own descendant.
    """
    ancestors = {as_uuid(value) for value in ancestor_session_ids} - {None}
    if not ancestors:
        return False
    chain = session_ancestor_chain(
        db, account_id=account_id, runtime_session_id=target_session_id
    )
    return any(session_id in ancestors for session_id in chain[1:])


def caller_session_ids(
    db: Session,
    *,
    account_id: Any,
    bound_runtime_session_id: Any = None,
    principal_type: Optional[str] = None,
    principal_id: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    caller_session_id: Optional[str] = None,
) -> List[uuid.UUID]:
    """The runtime sessions an MCP call was made from, as far as we can tell.

    A session-bound credential names its session outright. A durable
    credential (one per machine for an enrolled CLI agent) does not, but the
    harness stamps its own session id on the request; that id is resolved
    only against the caller's own principal, so it cannot name a session the
    credential did not already own.
    """
    found: List[uuid.UUID] = []
    bound = as_uuid(bound_runtime_session_id)
    if bound is not None:
        # A bound credential cannot switch sessions through an input or header.
        return [bound]
    if principal_type and principal_id:
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        external = normalize_session_id(caller_session_id)
        if external is None:
            external = next(
                (
                    value
                    for name in CALLER_SESSION_HEADERS
                    if (value := normalize_session_id(lowered.get(name))) is not None
                ),
                None,
            )
        if external is not None:
            session = crud_runtime_session.get_by_source(
                db,
                account_id=account_id,
                session_source_type=principal_type,
                session_source_id=session_source_id_for(principal_id, external),
            )
            if session is None:
                hosted = runner_session_for_external(
                    db, account_id=account_id, external_session_id=external
                )
                if (
                    hosted is not None
                    and hosted.runtime_principal_type == principal_type
                    and str(hosted.runtime_principal_id) == str(principal_id)
                ):
                    session = hosted
            if session is not None:
                found.append(session.id)
    return found


def live_children(
    db: Session,
    *,
    account_id: Any,
    parent_session_ids: Sequence[Any],
    limit: int = 50,
) -> List[RuntimeSession]:
    """Open sessions whose recorded parent is one of these, newest first."""
    parents = [p for p in (as_uuid(v) for v in parent_session_ids) if p]
    if not parents:
        return []
    return (
        db.query(RuntimeSession)
        .filter(
            RuntimeSession.account_id == account_id,
            RuntimeSession.parent_session_id.in_(parents),
            RuntimeSession.ended_at.is_(None),
        )
        .order_by(RuntimeSession.started_at.desc())
        .limit(limit)
        .all()
    )


def register_session_start(
    db: Session,
    *,
    account_id: Any,
    principal_type: str,
    principal_id: str,
    principal_name: Optional[str],
    external_session_id: str,
    agent_kind: Optional[str] = None,
    cwd: Optional[str] = None,
    parent_session_id: Any = None,
    first_prompt: Optional[str] = None,
    alias: Optional[str] = None,
    actor_user_id: Any = None,
) -> Optional[SessionStartResult]:
    """Create or find the run's session and record what the hook knows.

    Keyed exactly as the gateway keys the same conversation, so the model
    traffic, the permission hook and this call land on one row whichever
    arrives first. ``parent_session_id`` must name a session in the same
    account and is ignored otherwise; it is write-once like every other
    lineage write. ``title`` and ``runtime_principal_name`` are filled only
    while empty, so a title a person or the summariser set is kept.

    When a live runner session in the account hosts this harness id (#1482),
    the registration resolves to that runner session instead; ``actor_user_id``
    (the hook credential's user) must then match the user who started it.

    Returns:
        The registered session, or None when the external id is unusable.
    """
    external = normalize_session_id(external_session_id)
    if external is None:
        return None
    alias = re.sub(r"[^A-Za-z0-9._-]", "-", alias or "")[:64] or None
    hosted = runner_session_for_external(
        db,
        account_id=account_id,
        external_session_id=external,
        actor_user_id=actor_user_id,
    )
    if hosted is not None:
        return _register_runner_hosted_start(
            db,
            account_id=account_id,
            session=hosted,
            external=external,
            agent_kind=agent_kind,
            principal_name=principal_name,
            cwd=cwd,
        )
    crud_runtime_session.lock_alias_account(db, account_id=account_id)
    source_id = session_source_id_for(principal_id, external)
    parent_id: Optional[uuid.UUID] = None
    parent_uuid = as_uuid(parent_session_id)
    if parent_uuid is not None:
        parent = crud_runtime_session.get_account_session(
            db, account_id=account_id, runtime_session_id=parent_uuid
        )
        if parent is not None:
            parent_id = parent.id
        else:
            logger.info(
                "Ignoring unknown parent session %s on session start", parent_uuid
            )
    existing = crud_runtime_session.get_by_source(
        db,
        account_id=account_id,
        session_source_type=principal_type,
        session_source_id=source_id,
    )
    if existing is not None and parent_id == existing.id:
        parent_id = None
    if existing is not None and existing.alias:
        if parent_id is not None and existing.parent_session_id is None:
            existing.parent_session_id = parent_id
        crud_runtime_session.assign_spawn_alias(
            db,
            session=existing,
            alias=alias,
            principal_type=principal_type,
            principal_id=principal_id,
        )
    now = datetime.now(UTC)
    upsert_kwargs = dict(
        account_id=account_id,
        session_source_type=principal_type,
        session_source_id=source_id,
        runtime_principal_type=principal_type,
        runtime_principal_id=source_id,
        started_at=now,
        last_activity_at=now,
        reopen_if_ended=True,
        parent_session_id=parent_id,
    )
    raced = False
    try:
        session = crud_runtime_session.upsert_by_source(db, **upsert_kwargs)
    except IntegrityError:
        raced = True
        # The gateway created the same row between our read and our insert.
        # One retry finds it and goes through the update path instead.
        db.rollback()
        crud_runtime_session.lock_alias_account(db, account_id=account_id)
        session = crud_runtime_session.upsert_by_source(db, **upsert_kwargs)
    if cwd and not session.cwd:
        session.cwd = cwd[:1024]
    label = principal_label(agent_kind or principal_type, cwd or session.cwd)
    if not session.runtime_principal_name:
        session.runtime_principal_name = label or principal_name
    crud_runtime_session.assign_spawn_alias(
        db,
        session=session,
        alias=alias,
        principal_type=principal_type,
        principal_id=principal_id,
    )
    title = session.alias or title_from_prompt(first_prompt)
    if title and not session.title:
        session.title = title
    db.add(session)
    db.commit()
    db.refresh(session)
    return SessionStartResult(
        runtime_session_id=str(session.id),
        parent_session_id=(
            str(session.parent_session_id) if session.parent_session_id else None
        ),
        started_at=session.started_at,
        created=existing is None and not raced,
        alias=session.alias,
    )


def _register_runner_hosted_start(
    db: Session,
    *,
    account_id: Any,
    session: RuntimeSession,
    external: str,
    agent_kind: Optional[str],
    principal_name: Optional[str],
    cwd: Optional[str],
) -> SessionStartResult:
    """Attach a hook's session start to the runner session hosting it.

    The runner session keeps its title, principal and lineage. The hook adds
    what it alone knows: the harness id as ``session_reference`` and, when
    the row has none, a working directory and principal label. Any row the
    hook path opened earlier under this id is folded in.
    """
    fold_hook_sessions_into_runner(
        db, account_id=account_id, external_session_id=external, target=session
    )
    if not session.session_reference:
        session.session_reference = external[:255]
    if cwd and not session.cwd:
        session.cwd = cwd[:1024]
    if not session.runtime_principal_name:
        label = principal_label(agent_kind, cwd or session.cwd)
        session.runtime_principal_name = label or principal_name
    session.last_activity_at = datetime.now(UTC)
    db.add(session)
    db.commit()
    db.refresh(session)
    return SessionStartResult(
        runtime_session_id=str(session.id),
        parent_session_id=(
            str(session.parent_session_id) if session.parent_session_id else None
        ),
        started_at=session.started_at,
        created=False,
        alias=session.alias,
    )
