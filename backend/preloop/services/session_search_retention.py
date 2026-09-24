"""Make the session search corpus follow the rules its sources already follow.

A search index is a second copy of the data, and a second copy is where a
compliance guarantee quietly stops being true. Two failures this module
exists to prevent:

- a purge deletes a session and its chunks survive, so the deletion did not
  happen and a search can still quote the record the account was told was
  gone;
- a hold preserves a session and the purge takes its chunks anyway, so the
  evidence is incomplete in a way nobody notices until it matters.

Both are one rule: **chunks follow their session**. What deletes a session
deletes its chunks, and what preserves a session preserves them. The purge
calls in here per batch of ids rather than repeating chunk SQL per class, and
:func:`orphan_chunk_report` is how the rule is checked instead of assumed.

The one deliberate asymmetry is usage. Gateway chunks quote an ``api_usage``
row, so purging the usage class takes them too, except where the chunk's
session is under legal hold: a hold is an instruction to preserve the record
and it outranks a retention cutoff. The held session's chunks then outlive the
usage row they quote for as long as the hold lasts. Once the hold is released,
the next usage pass reclaims those orphans: the original usage ids cannot
appear in a later batch, so the pass sweeps gateway chunks whose source row
is gone and whose session is no longer held.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from preloop.models.crud import crud_session_search_document
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.models.runtime_session_artifact import RuntimeSessionArtifact
from preloop.models.models.session_search_document import (
    SOURCE_KIND_GATEWAY_INTERACTION,
)
from preloop.services.retention_policy import CLASS_RUNTIME_SESSIONS, CLASS_USAGE

logger = logging.getLogger(__name__)


@dataclass
class OrphanChunkReport:
    """What :func:`orphan_chunk_report` found. Empty is the only good answer."""

    #: Chunks whose runtime session no longer exists.
    orphaned_sessions: int = 0
    #: Gateway chunks whose ``api_usage`` row no longer exists.
    orphaned_usage: int = 0

    @property
    def total(self) -> int:
        return self.orphaned_sessions + self.orphaned_usage

    @property
    def clean(self) -> bool:
        """True when the corpus quotes nothing that has been deleted."""
        return self.total == 0

    def as_dict(self) -> dict[str, int]:
        return {
            "orphaned_sessions": self.orphaned_sessions,
            "orphaned_usage": self.orphaned_usage,
            "total": self.total,
        }


def delete_chunks_for_sessions(db: Session, *, ids: Sequence[Any]) -> int:
    """Remove the corpus rows of sessions this batch is about to delete.

    The foreign key cascades, so the purge's ``DELETE FROM runtime_session``
    would take these rows regardless. Doing it explicitly, first, buys three
    things: the count is known and can be audited, the guarantee holds on a
    database whose constraint predates the cascade, and the behaviour is
    stated in code a reader can find rather than implied by DDL they have to
    go looking for.
    """
    if not ids:
        return 0
    return crud_session_search_document.delete_for_sessions(
        db, runtime_session_ids=list(ids)
    )


def delete_chunks_for_usage(
    db: Session, *, ids: Sequence[Any], account_id: Optional[Any] = None
) -> int:
    """Remove gateway chunks quoting usage rows this batch is about to delete.

    Nothing cascades here: a gateway chunk names its ``api_usage`` row by id
    in a text column, not by foreign key, because the corpus indexes several
    source kinds whose keys have different types. Without this the usage purge
    would leave chunks quoting rows that no longer exist.

    Chunks of a session under legal hold are kept. A hold says preserve this
    session's record, and a retention cutoff on a different record class does
    not get to overrule it. The cost is a chunk that outlives the usage row it
    quotes for as long as the hold lasts. After the hold is released, this
    pass also deletes gateway chunks whose usage row is already gone and whose
    session is no longer held, because those usage ids can never appear in a
    later batch.
    """
    deleted = 0
    if ids:
        deleted += crud_session_search_document.delete_for_sources(
            db,
            source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
            source_ids=list(ids),
            excluding_held_sessions=True,
        )
    deleted += crud_session_search_document.delete_orphans_for_sources(
        db,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_model=ApiUsage,
        excluding_held_sessions=True,
        account_id=account_id,
    )
    return deleted


#: Per record class, the function that removes the corpus rows derived from a
#: batch of that class's ids. A class that is absent derives no chunks. The
#: purge consults this mapping instead of naming the corpus per class, so
#: adding a derived table later is one entry rather than an edit in five
#: places, four of which somebody forgets.
DERIVED_CHUNK_DELETES: dict[str, Callable[..., int]] = {
    CLASS_RUNTIME_SESSIONS: delete_chunks_for_sessions,
    CLASS_USAGE: delete_chunks_for_usage,
}


def delete_derived_chunks(
    db: Session,
    *,
    record_class: str,
    ids: Sequence[Any],
    account_id: Optional[Any] = None,
) -> int:
    """Remove the corpus rows derived from one purge batch of one class."""
    handler = DERIVED_CHUNK_DELETES.get(record_class)
    if handler is None:
        return 0
    if record_class == CLASS_USAGE:
        return handler(db, ids=list(ids), account_id=account_id)
    if not ids:
        return 0
    return handler(db, ids=list(ids))


def orphan_chunk_report(db: Session) -> OrphanChunkReport:
    """Count corpus rows whose source is gone. The invariant, made checkable.

    Deployment-wide rather than account scoped on purpose: an orphan produced
    by one account's purge is a bug in the purge, not a property of that
    account, and scoping the check to the account under test would hide a
    cross-account mistake, which is the mistake worth catching.

    Usage orphans share the hold exclusion with
    :func:`delete_chunks_for_usage`. A held session's gateway chunk that
    outlives the usage row it quotes is the hold working, not a purge bug,
    so it does not count. After the hold is released, the next usage pass
    reclaims that chunk and this check is clean again.
    """
    return OrphanChunkReport(
        orphaned_sessions=crud_session_search_document.count_orphans_for_sessions(db),
        orphaned_usage=crud_session_search_document.count_orphans_for_sources(
            db,
            source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
            source_model=ApiUsage,
            excluding_held_sessions=True,
        ),
    )


def orphan_session_artifact_count(db: Session) -> int:
    """Artifacts whose ``runtime_session_id`` has no ``runtime_session`` row.

    The foreign key is ``ON DELETE CASCADE``, so this is zero unless a session
    was removed without its artifacts. An artifact row that outlives its
    session is the failure the cascade exists to prevent.

    Args:
        db: Database session.

    Returns:
        Orphan ``runtime_session_artifact`` rows, deployment-wide.
    """
    return int(
        db.execute(
            select(func.count())
            .select_from(RuntimeSessionArtifact)
            .where(
                ~select(RuntimeSession.id)
                .where(RuntimeSession.id == RuntimeSessionArtifact.runtime_session_id)
                .exists()
            )
        ).scalar_one()
    )


def assert_no_orphan_chunks(db: Session, *, context: Optional[str] = None) -> None:
    """Raise if the corpus quotes anything deleted. For tests and operators.

    Also raises when an artifact row has no session. Kept next to the purge
    rather than in a test helper so an operator can run it against a real
    database after a pass, which is when the answer matters.
    """
    report = orphan_chunk_report(db)
    artifact_orphans = orphan_session_artifact_count(db)
    if report.clean and artifact_orphans == 0:
        return
    where = f" after {context}" if context else ""
    parts: list[str] = []
    if not report.clean:
        parts.append(
            f"Session search corpus has {report.total} orphan chunks{where}: "
            f"{report.as_dict()}. A chunk outliving its source means a purge "
            "deleted a record the search index can still quote."
        )
    if artifact_orphans:
        parts.append(
            f"runtime_session_artifact has {artifact_orphans} orphan rows{where}. "
            "An artifact row outliving its session means a purge deleted the "
            "session and left the artifact behind."
        )
    raise AssertionError(" ".join(parts))
