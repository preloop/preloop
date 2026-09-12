"""Explicit transaction boundaries for an HTTP-owned model gateway session."""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from preloop.models import models


def release_gateway_session(db: Session, *, preserve: Iterable[Any]) -> None:
    """Finish request preparation and preserve detached values for provider waits.

    Only an HTTP request that owns its entire session may use this boundary.
    Internal gateways can share a caller transaction and must not call it.
    Credential refresh must finish its serialized rotation before this boundary.

    Materialize the small model/auth graph while it is still attached. Preserve
    loaded bookkeeping objects too: a nested summary completion can run while
    its caller still needs a usage or runtime-session row. Disabling expiration
    on this owned session prevents commits in existing CRUD helpers from making
    those values implicitly query again. Expunging before close means rollback
    cannot expire values retained by stream generators.
    """
    # Authentication can release its phase before a gateway service exists.
    # Keep the loaded values valid across the final commit and detachment.
    db.expire_on_commit = False
    try:
        for instance in preserve:
            state = inspect(instance, raiseerr=False) if instance is not None else None
            if state is None:
                continue
            # Detached snapshots have already been materialized by a prior phase.
            if state.persistent:
                for column in state.mapper.column_attrs:
                    getattr(instance, column.key)
            if isinstance(instance, models.AIModel):
                secret = instance.credentials_secret
                secret_state = (
                    inspect(secret, raiseerr=False) if secret is not None else None
                )
                if secret_state is not None and secret_state.persistent:
                    for column in secret_state.mapper.column_attrs:
                        getattr(secret, column.key)
        # Unlike embedding.create_embeddings, this HTTP-owned boundary must
        # persist preparation writes (runtime session, OAuth sibling, dirty
        # auth/model graph). A clean-session ValueError would discard them.
        # Pending identity is this request's unit of work — empty (idle
        # checkout) or those preparation writes — never a reason to skip
        # commit. Sole caller: OpenAIGatewayService.release_db_for_wait after
        # request preparation or persisted accounting. Do not assert on
        # db.new/dirty/deleted here: those collections are ORM instances, not
        # a None-able identity, and a vacuous check would look like a guard.
        if db.in_transaction():
            db.commit()
        db.expunge_all()
    finally:
        db.close()


def has_runtime_session_summary_columns(db: Session) -> bool:
    """Inspect schema using the existing checkout, never a second pool slot."""
    columns = {
        column["name"]
        for column in inspect(db.connection()).get_columns("runtime_session")
    }
    return {"summary", "summary_updated_at"}.issubset(columns)
