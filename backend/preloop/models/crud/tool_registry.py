"""Transactional registry persistence; no service queries the database directly."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models


def get_or_create_seen(
    db: Session,
    *,
    account_id: UUID,
    source: str,
    server_id: UUID | None,
    server_label: str | None,
    tool_name: str,
    agent_id: UUID | None,
) -> tuple[models.ToolRegistryEntry, bool]:
    """Atomically observe a route and lock its row until the caller commits.

    The NULLS NOT DISTINCT identity and conflicting insert/update serialize
    concurrent first sightings. The row lock also guards approval attachment.
    """
    if source != "mcp" or (server_id is None and not server_label):
        raise ValueError("An MCP server id or hook server label is required")
    now = datetime.now(timezone.utc)
    new_id = uuid4()
    table = models.ToolRegistryEntry
    stmt = insert(table).values(
        id=new_id,
        account_id=account_id,
        tool_source=source,
        mcp_server_id=server_id,
        server_label=None if server_id else server_label,
        tool_name=tool_name,
        state="pending",
        first_seen_at=now,
        first_seen_agent_id=agent_id,
        last_seen_at=now,
        seen_count=1,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_tool_registry_entry",
        set_={
            "last_seen_at": now,
            "seen_count": table.seen_count + 1,
            "updated_at": now,
        },
    )
    db.execute(stmt)
    entry = db.execute(
        select(table)
        .where(
            table.account_id == account_id,
            table.tool_source == source,
            table.mcp_server_id == server_id,
            table.server_label == (None if server_id else server_label),
            table.tool_name == tool_name,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    return entry, entry.id == new_id


def get(
    db: Session, *, account_id: UUID, entry_id: UUID, lock: bool = False
) -> models.ToolRegistryEntry | None:
    """Fetch an account-owned entry, optionally locking it for a decision."""
    stmt = select(models.ToolRegistryEntry).where(
        models.ToolRegistryEntry.id == entry_id,
        models.ToolRegistryEntry.account_id == account_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(
        stmt.execution_options(populate_existing=True)
    ).scalar_one_or_none()


def attach_approval(
    db: Session, *, account_id: UUID, entry_id: UUID, request_id: UUID
) -> None:
    """Attach before ApprovalService commits the new request in this transaction."""
    entry = get(db, account_id=account_id, entry_id=entry_id, lock=True)
    if entry is None or entry.state != "pending":
        raise ValueError("Registry entry is not pending")
    entry.approval_request_id = request_id
    db.flush()


def decide(
    db: Session, *, account_id: UUID, entry_id: UUID, state: str, user_id: UUID | None
) -> models.ToolRegistryEntry | None:
    """Approve or deny an owned route; denied also revokes a prior approval."""
    if state not in ("approved", "denied"):
        raise ValueError("state must be approved or denied")
    entry = get(db, account_id=account_id, entry_id=entry_id, lock=True)
    if entry is None:
        return None
    entry.state = state
    entry.decided_at = datetime.now(timezone.utc)
    entry.decided_by_user_id = user_id
    db.flush()
    return entry


def resolve_approval(db: Session, *, request: Any, user_id: UUID | None) -> None:
    """Mirror only the currently attached request's terminal execution verdict."""
    if request.status not in ("approved", "declined"):
        return
    context = request.rule_context or {}
    entry_id = context.get("registry_entry_id")
    if not entry_id:
        return
    entry = get(db, account_id=request.account_id, entry_id=UUID(entry_id), lock=True)
    if entry is not None and entry.approval_request_id == request.id:
        decide(
            db,
            account_id=request.account_id,
            entry_id=entry.id,
            state="approved" if request.status == "approved" else "denied",
            user_id=user_id,
        )


def list_entries(
    db: Session,
    *,
    account_id: UUID,
    state: str | None = None,
    page: int = 0,
    page_size: int = 50,
) -> list[models.ToolRegistryEntry]:
    """Return one stable page without exposing entries from another account."""
    stmt = select(models.ToolRegistryEntry).where(
        models.ToolRegistryEntry.account_id == account_id
    )
    if state:
        stmt = stmt.where(models.ToolRegistryEntry.state == state)
    return list(
        db.scalars(
            stmt.order_by(
                models.ToolRegistryEntry.first_seen_at.desc(),
                models.ToolRegistryEntry.id,
            )
            .offset(page * page_size)
            .limit(page_size)
        )
    )


def find(
    db: Session,
    *,
    account_id: UUID,
    server_id: UUID | None,
    server_label: str | None,
    tool_name: str,
) -> models.ToolRegistryEntry | None:
    """Read an existing route without recording a sighting (policy simulation)."""
    table = models.ToolRegistryEntry
    return db.execute(
        select(table).where(
            table.account_id == account_id,
            table.tool_source == "mcp",
            table.mcp_server_id == server_id,
            table.server_label == (None if server_id else server_label),
            table.tool_name == tool_name,
        )
    ).scalar_one_or_none()


def display_names(
    db: Session, *, account_id: UUID, entries: list[models.ToolRegistryEntry]
) -> tuple[dict[UUID, str], dict[UUID, str]]:
    """Batch names within the owning account without changing route identity."""
    server_ids = {entry.mcp_server_id for entry in entries if entry.mcp_server_id}
    agent_ids = {
        entry.first_seen_agent_id for entry in entries if entry.first_seen_agent_id
    }
    servers = (
        dict(
            db.execute(
                select(models.MCPServer.id, models.MCPServer.name).where(
                    models.MCPServer.account_id == account_id,
                    models.MCPServer.id.in_(server_ids),
                )
            ).all()
        )
        if server_ids
        else {}
    )
    agents = (
        dict(
            db.execute(
                select(models.ManagedAgent.id, models.ManagedAgent.display_name).where(
                    models.ManagedAgent.account_id == account_id,
                    models.ManagedAgent.id.in_(agent_ids),
                )
            ).all()
        )
        if agent_ids
        else {}
    )
    return servers, agents
