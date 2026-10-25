"""Tenant-scoped MCP review persistence. The caller owns the transaction."""

import logging
from datetime import datetime
from uuid import UUID, uuid4
from typing import Any

from sqlalchemy import String, and_, case, cast, func, or_, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from preloop.models import models

logger = logging.getLogger(__name__)


class CRUDMCPInventory:
    """Store declarations and evidence without changing execution permissions."""

    def reconcile_configured(
        self, db: Session, *, account_id: UUID, at: datetime, limit: int = 500
    ) -> None:
        """Lock current owned revisions and upsert configuration in one batch."""
        servers = db.execute(
            select(models.MCPServer.id, models.MCPServer.updated_at)
            .where(models.MCPServer.account_id == account_id)
            .order_by(models.MCPServer.id)
            .limit(limit + 1)
            .with_for_update()
        ).all()
        if not servers:
            return
        if len(servers) > limit:
            logger.warning("MCP registry reconciliation is incomplete: bound exceeded")
        stmt = insert(models.MCPInventoryFinding).values(
            [
                {
                    "id": uuid4(),
                    "account_id": account_id,
                    "server_id": server_id,
                    "registry_revision": updated_at.isoformat(),
                    "evidence_class": "configured_route",
                    "first_seen_at": at,
                    "last_seen_at": at,
                }
                for server_id, updated_at in servers[:limit]
            ]
        )
        db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_mcp_inventory_finding",
                set_={
                    "first_seen_at": func.least(
                        models.MCPInventoryFinding.first_seen_at, at
                    ),
                    "last_seen_at": func.greatest(
                        models.MCPInventoryFinding.last_seen_at, at
                    ),
                },
            )
        )

    def list_with_context(
        self, db: Session, *, account_id: UUID, limit: int = 500
    ) -> list[tuple[models.MCPInventoryFinding, str | None, datetime | None]]:
        """Read bounded evidence and tenant-scoped declaration/currentness at once."""
        finding = models.MCPInventoryFinding
        review = models.MCPInventoryReview
        server = models.MCPServer
        rows = db.execute(
            select(finding, review.approval_state, server.updated_at)
            .outerjoin(
                review,
                and_(
                    review.account_id == finding.account_id,
                    review.server_id == finding.server_id,
                    review.registry_revision == finding.registry_revision,
                ),
            )
            .outerjoin(
                server,
                and_(
                    server.account_id == finding.account_id,
                    server.id == finding.server_id,
                ),
            )
            .where(finding.account_id == account_id)
            .order_by(finding.first_seen_at.desc(), finding.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return [(row[0], row[1], row[2]) for row in rows]

    def digest_context(
        self, db: Session, *, account_id: UUID, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Read names, safe attribution and revision history in one SELECT.

        All identity joins prove membership in the finding's account. No
        registry URL or credential leaves this projection.
        """
        finding, review, server = (
            models.MCPInventoryFinding,
            models.MCPInventoryReview,
            models.MCPServer,
        )
        agent, user = models.ManagedAgent, models.User
        previous = aliased(finding)
        prior = (
            select(previous.id)
            .where(
                previous.account_id == finding.account_id,
                previous.server_id == finding.server_id,
                previous.registry_revision != finding.registry_revision,
                previous.first_seen_at < finding.first_seen_at,
            )
            .exists()
        )
        audit = models.AuditLog
        change_event = (
            select(
                case(
                    (audit.details["action"].astext == "deleted", audit.timestamp),
                    else_=None,
                ).label("removed_at"),
                audit.details["action"].astext.label("change_action"),
                audit.user_id.label("actor_id"),
                func.coalesce(
                    audit.details["new_value"]["name"].astext,
                    audit.details["old_value"]["name"].astext,
                ).label("server_name"),
            )
            .where(
                audit.account_id == finding.account_id,
                audit.action == "configuration_change",
                audit.resource_type == "configuration",
                audit.resource_id == "mcp_server",
                audit.status == "success",
                audit.details["action"].astext.in_(["created", "updated", "deleted"]),
                or_(
                    audit.details["old_value"]["id"].astext
                    == cast(finding.server_id, String),
                    audit.details["new_value"]["id"].astext
                    == cast(finding.server_id, String),
                ),
                audit.timestamp >= start,
                audit.timestamp < end,
            )
            .order_by(audit.timestamp.desc(), audit.id)
            .limit(1)
            .correlate(finding)
            .lateral("change_event")
        )
        query = (
            select(
                finding,
                review.approval_state.label("approval"),
                server.updated_at.label("updated_at"),
                func.coalesce(server.name, change_event.c.server_name).label(
                    "server_name"
                ),
                change_event.c.removed_at.label("removed_at"),
                change_event.c.change_action.label("change_action"),
                agent.display_name.label("agent_name"),
                user.id.label("user_id"),
                func.coalesce(user.full_name, user.username).label("user_name"),
                prior.label("prior_revision"),
            )
            .select_from(finding)
            .outerjoin(change_event, true())
            .outerjoin(
                review,
                and_(
                    review.account_id == finding.account_id,
                    review.server_id == finding.server_id,
                    review.registry_revision == finding.registry_revision,
                ),
            )
            .outerjoin(
                server,
                and_(
                    server.account_id == finding.account_id,
                    server.id == finding.server_id,
                ),
            )
            .outerjoin(
                agent,
                and_(
                    agent.account_id == finding.account_id,
                    agent.id == finding.agent_id,
                ),
            )
            .outerjoin(
                user,
                and_(
                    user.account_id == finding.account_id,
                    or_(
                        and_(
                            change_event.c.actor_id.is_not(None),
                            or_(
                                finding.evidence_class == "configured_route",
                                server.id.is_(None),
                            ),
                            user.id == change_event.c.actor_id,
                        ),
                        and_(
                            or_(
                                change_event.c.actor_id.is_(None),
                                and_(
                                    finding.evidence_class == "observed_call",
                                    server.id.is_not(None),
                                ),
                            ),
                            user.person_id == finding.person_id,
                        ),
                    ),
                ),
            )
            .where(
                finding.account_id == account_id,
                or_(
                    and_(finding.first_seen_at >= start, finding.first_seen_at < end),
                    and_(server.id.is_(None), change_event.c.removed_at.is_not(None)),
                ),
            )
            .order_by(finding.first_seen_at.desc(), finding.id)
            .limit(500)
        )
        return [dict(row) for row in db.execute(query).mappings()]

    def lock(self, db: Session, *, account_id: UUID) -> None:
        """Serialize observations and decisions for one account.

        NO KEY UPDATE keeps holders exclusive without blocking child-row
        foreign-key checks on the account.
        """
        if (
            db.scalar(
                select(models.Account.id)
                .where(models.Account.id == account_id)
                .with_for_update(key_share=True)
            )
            is None
        ):
            raise ValueError("Account not found")

    def server(
        self, db: Session, *, account_id: UUID, server_id: UUID
    ) -> models.MCPServer | None:
        """Lock and refresh the tenant-owned revision against concurrent edits."""
        return db.scalar(
            select(models.MCPServer)
            .where(
                models.MCPServer.account_id == account_id,
                models.MCPServer.id == server_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def review(
        self, db: Session, *, account_id: UUID, server_id: UUID, revision: str
    ) -> models.MCPInventoryReview | None:
        """Read an exact registry revision declaration."""
        return db.scalar(
            select(models.MCPInventoryReview).where(
                models.MCPInventoryReview.account_id == account_id,
                models.MCPInventoryReview.server_id == server_id,
                models.MCPInventoryReview.registry_revision == revision,
            )
        )

    def declare(
        self,
        db: Session,
        *,
        account_id: UUID,
        server_id: UUID,
        revision: str,
        state: str,
    ) -> None:
        """Upsert an explicit inventory-only declaration."""
        server = self.server(db, account_id=account_id, server_id=server_id)
        if server is None or server.updated_at.isoformat() != revision:
            raise ValueError("Registry revision is not owned by this account")
        if state not in {"approved", "unknown", "unapproved"}:
            raise ValueError("Invalid inventory state")
        stmt = insert(models.MCPInventoryReview).values(
            id=uuid4(),
            account_id=account_id,
            server_id=server_id,
            registry_revision=revision,
            approval_state=state,
        )
        db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_mcp_inventory_revision",
                set_={"approval_state": state},
            )
        )
        db.flush()

    def observe(
        self,
        db: Session,
        *,
        account_id: UUID,
        server_id: UUID,
        revision: str,
        evidence_class: str,
        at: datetime,
        agent_id: UUID | None = None,
        person_id: UUID | None = None,
    ) -> models.MCPInventoryFinding:
        """Keep first observation immutable and update the most recent evidence."""
        server = self.server(db, account_id=account_id, server_id=server_id)
        if server is None or server.updated_at.isoformat() != revision:
            raise ValueError("Registry revision is not owned by this account")
        if evidence_class not in {"configured_route", "observed_call"}:
            raise ValueError("Invalid evidence class")
        stmt = insert(models.MCPInventoryFinding).values(
            id=uuid4(),
            account_id=account_id,
            server_id=server_id,
            registry_revision=revision,
            evidence_class=evidence_class,
            first_seen_at=at,
            last_seen_at=at,
            agent_id=agent_id,
            person_id=person_id,
        )
        db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_mcp_inventory_finding",
                set_={
                    "first_seen_at": func.least(
                        models.MCPInventoryFinding.first_seen_at, at
                    ),
                    "last_seen_at": func.greatest(
                        models.MCPInventoryFinding.last_seen_at, at
                    ),
                    "agent_id": case(
                        (models.MCPInventoryFinding.last_seen_at <= at, agent_id),
                        else_=models.MCPInventoryFinding.agent_id,
                    ),
                    "person_id": case(
                        (models.MCPInventoryFinding.last_seen_at <= at, person_id),
                        else_=models.MCPInventoryFinding.person_id,
                    ),
                },
            )
        )
        row = db.scalar(
            select(models.MCPInventoryFinding)
            .where(
                models.MCPInventoryFinding.account_id == account_id,
                models.MCPInventoryFinding.server_id == server_id,
                models.MCPInventoryFinding.registry_revision == revision,
                models.MCPInventoryFinding.evidence_class == evidence_class,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise RuntimeError("MCP observation was not persisted")
        return row

    def list(
        self, db: Session, *, account_id: UUID, limit: int = 500
    ) -> list[models.MCPInventoryFinding]:
        """Read a bounded, deterministic account inventory."""
        return list(
            db.scalars(
                select(models.MCPInventoryFinding)
                .where(
                    models.MCPInventoryFinding.account_id == account_id,
                )
                .order_by(
                    models.MCPInventoryFinding.first_seen_at.desc(),
                    models.MCPInventoryFinding.id,
                )
                .limit(limit)
            )
        )

    def get(
        self, db: Session, *, account_id: UUID, finding_id: UUID
    ) -> models.MCPInventoryFinding | None:
        """Find evidence only in its owning tenant."""
        return db.scalar(
            select(models.MCPInventoryFinding).where(
                models.MCPInventoryFinding.account_id == account_id,
                models.MCPInventoryFinding.id == finding_id,
            )
        )

    def ignore(
        self, db: Session, *, row: models.MCPInventoryFinding, until: datetime
    ) -> None:
        """Preserve all evidence while attaching an expiration."""
        row.ignored_until = until
        db.flush()


crud_mcp_inventory = CRUDMCPInventory()
