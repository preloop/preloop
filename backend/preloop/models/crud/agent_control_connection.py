"""Short, ownership-guarded control transactions.

Lock order is managed agent, credential, user, then runtime/command rows.
Callers finish the transaction before socket or broker I/O. A generation is a
fence for database effects; network delivery remains at least once.
"""

from dataclasses import dataclass
import uuid

from sqlalchemy import Connection, event, text
from sqlalchemy.orm import Session

from preloop.models import models


@dataclass(frozen=True)
class AgentControlConnectionContext:
    """Immutable scalar identity; no ORM entity crosses a worker boundary."""

    account_id: str
    managed_agent_id: str
    runtime_session_id: str
    session_source_type: str
    session_source_id: str
    managed_agent_session_source_type: str
    managed_agent_session_source_id: str
    api_key_id: str = ""
    user_id: str = ""
    agent_kind: str = ""
    session_reference: str | None = None
    runtime_principal_id: str | None = None
    connection_id: str = ""


def configure_transaction(db: Session) -> None:
    """Bound database waits locally; pool checkout retains its engine timeout."""

    def set_limits(
        session: Session, transaction: object, connection: Connection
    ) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SET LOCAL lock_timeout = '1500ms'"))
            connection.execute(text("SET LOCAL statement_timeout = '5000ms'"))

    event.listen(db, "after_begin", set_limits)


def authorize(
    db: Session, connection: AgentControlConnectionContext, *, claim: bool = False
) -> models.ManagedAgent | None:
    """Lock and revalidate the current owner until the caller commits."""
    agent = (
        db.query(models.ManagedAgent)
        .filter(
            models.ManagedAgent.id == connection.managed_agent_id,
            models.ManagedAgent.account_id == connection.account_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if agent is None or agent.lifecycle_state != "active":
        return None
    if not claim and str(agent.control_connection_id) != connection.connection_id:
        return None
    key = (
        db.query(models.ApiKey)
        .filter(
            models.ApiKey.id == connection.api_key_id,
            models.ApiKey.account_id == connection.account_id,
            models.ApiKey.user_id == connection.user_id,
        )
        .populate_existing()
        .with_for_update(read=True)
        .first()
    )
    if key is None or not key.is_active or key.is_expired:
        return None
    context = key.context_data if isinstance(key.context_data, dict) else {}
    if str(context.get("managed_agent_id")) != connection.managed_agent_id:
        return None
    # Account suspension was historically not checked by runtime auth. Read
    # it here without a row lock: account lifecycle writers do not share this
    # lock order. A concurrent suspension may finish after this read, but the
    # next control unit rejects it (as with the post-authorization I/O window).
    active_account = (
        db.query(models.Account.id)
        .filter(
            models.Account.id == connection.account_id,
            models.Account.is_active.is_(True),
        )
        .first()
    )
    if active_account is None:
        return None
    user = (
        db.query(models.User)
        .filter(
            models.User.id == connection.user_id,
            models.User.account_id == connection.account_id,
        )
        .populate_existing()
        .with_for_update(read=True)
        .first()
    )
    if user is None or not user.is_active:
        return None
    if claim:
        agent.control_connection_id = uuid.UUID(connection.connection_id)
        agent.runtime_session_id = uuid.UUID(connection.runtime_session_id)
        db.flush()
    return agent


def retire(db: Session, connection: AgentControlConnectionContext) -> bool:
    """Clear presence and binding atomically, only for the persisted owner.

    Cleanup is allowed after revocation so the revoked socket can retire its
    own presence; it can never retire a replacement's generation.
    """
    agent = (
        db.query(models.ManagedAgent)
        .filter(
            models.ManagedAgent.id == connection.managed_agent_id,
            models.ManagedAgent.account_id == connection.account_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if agent is None or str(agent.control_connection_id) != connection.connection_id:
        return False
    agent.control_connection_id = None
    agent.control_last_heartbeat_at = None
    agent.control_session_mode = None
    if str(agent.runtime_session_id) == connection.runtime_session_id:
        agent.runtime_session_id = None
    db.commit()
    return True


def commit(db: Session) -> None:
    """Finish an authorized unit without leaking its locks to network I/O."""
    db.commit()


def release_read_transaction(db: Session) -> None:
    """Release a producer's post-commit reads before a sender needs the pool.

    Do not commit unrelated caller edits. Durable command creation already
    committed its write; the remaining transaction must only contain reads.
    """
    if len(db.new) or len(db.dirty) or len(db.deleted):
        raise RuntimeError("Cannot release a control producer with pending writes")
    db.rollback()
