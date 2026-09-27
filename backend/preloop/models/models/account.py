"""Account model."""

import uuid
from datetime import datetime

# Use TYPE_CHECKING to avoid circular imports
from typing import TYPE_CHECKING, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Session
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .base import Base

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from .audit_log import AuditLog
    from .managed_agent import ManagedAgent
    from .managed_agent_credential import ManagedAgentCredential
    from .managed_agent_enrollment import ManagedAgentEnrollment
    from .organization import Organization
    from .tracker import Tracker
    from .client_version_log import ClientVersionLog
    from .ai_model import AIModel
    from .plan import Subscription
    from .flow import Flow
    from .tool_configuration import ToolConfiguration
    from .mcp_server import MCPServer
    from .approval_request import ApprovalRequest
    from .tool_access_rule import ToolAccessRule
    from .team import Team
    from .user import User
    from .user_invitation import UserInvitation
    from .event import Event
    from .github_app_installation import OAuthAppInstallation
    from .github_oauth_token import OAuthToken
    from .policy_snapshot import PolicySnapshot
    from .runtime_session import RuntimeSession
    from .secret_reference import SecretReference


class Account(Base):
    """Account model for multi-user organizations.

    In the multi-user system, Account represents an organization that contains
    multiple Users. Resources (flows, tools, trackers, etc.) are owned by the
    Account and accessible to users with appropriate permissions.

    Attributes:
        id: Unique identifier for the account.
        organization_name: Optional display name for the organization.
        primary_user_id: Reference to the primary user (account owner/creator).
        email_verified: Whether the account email has been verified.
        is_active: Whether the account is active.
        is_superuser: Whether this is a platform admin account.
        meta_data: Generic metadata field for extensibility.
        stripe_customer_id: Stripe customer ID for billing.
        parent_account_id: Parent account (NULL for a root account).
        root_account_id: Root of this account's tree (``id`` for a root).
        hierarchy_path: Account ids from the root down to this account,
            both included.
        hierarchy_depth: Number of levels below the root (0 for a root).
        created: When the account was created.
        last_updated: When the account was last updated.
    """

    __tablename__ = "account"

    # Organization details
    organization_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, comment="Display name for the organization"
    )

    primary_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL", name="fk_account_primary_user"),
        nullable=True,
        comment="The account owner/creator",
    )

    # Account-level verification and status
    email_verified: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    is_superuser: Mapped[bool] = mapped_column(default=False)

    # Timestamps
    created: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Generic metadata field for extensibility
    meta_data: Mapped[Dict] = mapped_column(JSON, nullable=True, default=dict)
    # Durable policy/billing state must survive unrelated metadata replacements.
    subscription_history_retention_days: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    billing_seat_sync_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    billing_seat_sync_generation: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True
    )
    billing_seat_sync_attempted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    billing_pending_change: Mapped[Optional[Dict]] = mapped_column(JSONB, nullable=True)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, unique=True
    )
    default_runner_pool: Mapped[Optional[str]] = mapped_column(
        String(200),
        nullable=True,
        comment=(
            "Account default runner pool: a runner id, name, or label; "
            "the literal 'server' for Preloop hosted; NULL means any "
            "online private runner."
        ),
    )

    # Account hierarchy (#986). Walk the tree only through ``ancestors`` and
    # ``descendants`` in preloop.models.models.hierarchy, never through
    # ``parent_account_id``: those helpers read ``hierarchy_path`` and do not
    # care how deep the tree is, so the depth limit lives in exactly one
    # place, the ``ck_account_hierarchy_depth_max`` CHECK below.
    parent_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="RESTRICT", name="fk_account_parent"),
        nullable=True,
        index=True,
        comment="Parent account; NULL for a root account",
    )
    root_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", name="fk_account_root"),
        nullable=False,
        index=True,
        comment="Root of this account's tree; equals id for a root account",
    )
    hierarchy_path: Mapped[List[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        comment="Account ids from the root to this account, both included",
    )
    hierarchy_depth: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="Levels below the root; 0 for a root account",
    )

    __table_args__ = (
        CheckConstraint(
            "(parent_account_id IS NULL) = (hierarchy_depth = 0)",
            name="ck_account_parent_iff_nonroot",
        ),
        # The only place tree depth is limited. Relaxing it needs no other
        # change to schema or helpers.
        CheckConstraint(
            "hierarchy_depth <= 1",
            name="ck_account_hierarchy_depth_max",
        ),
        CheckConstraint(
            "hierarchy_depth >= 0"
            " AND cardinality(hierarchy_path) = hierarchy_depth + 1"
            " AND hierarchy_path[1] = root_account_id"
            " AND hierarchy_path[cardinality(hierarchy_path)] = id",
            name="ck_account_hierarchy_path_shape",
        ),
        # parent_account_id and hierarchy_path encode the same edge: keep
        # them equal so the FK that guards deletes and the helpers agree.
        CheckConstraint(
            "hierarchy_depth = 0"
            " OR parent_account_id = hierarchy_path[hierarchy_depth]",
            name="ck_account_parent_is_path_tail",
        ),
        Index(
            "ix_account_hierarchy_path",
            "hierarchy_path",
            postgresql_using="gin",
        ),
    )

    # Relationships
    # Multi-user relationships
    users: Mapped[List["User"]] = relationship(
        "User",
        back_populates="account",
        cascade="all, delete-orphan",
        foreign_keys="[User.account_id]",
    )
    teams: Mapped[List["Team"]] = relationship(
        "Team", back_populates="account", cascade="all, delete-orphan"
    )
    invitations: Mapped[List["UserInvitation"]] = relationship(
        "UserInvitation", back_populates="account", cascade="all, delete-orphan"
    )

    # Resource relationships (owned by account)
    trackers: Mapped[List["Tracker"]] = relationship(
        "Tracker", back_populates="account", cascade="all, delete-orphan"
    )
    ai_models: Mapped[List["AIModel"]] = relationship(
        "AIModel", back_populates="account", cascade="all, delete-orphan"
    )
    client_version_logs: Mapped[List["ClientVersionLog"]] = relationship(
        "ClientVersionLog", back_populates="account", cascade="all, delete-orphan"
    )
    subscriptions: Mapped[List["Subscription"]] = relationship(
        "Subscription", back_populates="account", cascade="all, delete-orphan"
    )
    flows: Mapped[List["Flow"]] = relationship(
        "Flow",
        back_populates="account",
        cascade="all, delete-orphan",
        foreign_keys="[Flow.account_id]",
    )
    tool_configurations: Mapped[List["ToolConfiguration"]] = relationship(
        "ToolConfiguration", back_populates="account", cascade="all, delete-orphan"
    )
    mcp_servers: Mapped[List["MCPServer"]] = relationship(
        "MCPServer", back_populates="account", cascade="all, delete-orphan"
    )
    approval_requests: Mapped[List["ApprovalRequest"]] = relationship(
        "ApprovalRequest", back_populates="account", cascade="all, delete-orphan"
    )
    tool_access_rules: Mapped[List["ToolAccessRule"]] = relationship(
        "ToolAccessRule", back_populates="account", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[List["AuditLog"]] = relationship(
        "AuditLog", back_populates="account", cascade="all, delete-orphan"
    )
    events: Mapped[List["Event"]] = relationship(
        "Event",
        cascade="all, delete-orphan",
        foreign_keys="[Event.account_id]",
    )

    # OAuth App relationships
    oauth_app_installations: Mapped[List["OAuthAppInstallation"]] = relationship(
        "OAuthAppInstallation", back_populates="account", cascade="all, delete-orphan"
    )
    oauth_tokens: Mapped[List["OAuthToken"]] = relationship(
        "OAuthToken", back_populates="account", cascade="all, delete-orphan"
    )

    # Policy versioning
    policy_snapshots: Mapped[List["PolicySnapshot"]] = relationship(
        "PolicySnapshot", back_populates="account", cascade="all, delete-orphan"
    )
    secret_references: Mapped[List["SecretReference"]] = relationship(
        "SecretReference", back_populates="account", cascade="all, delete-orphan"
    )
    runtime_sessions: Mapped[List["RuntimeSession"]] = relationship(
        "RuntimeSession", back_populates="account", cascade="all, delete-orphan"
    )
    managed_agents: Mapped[List["ManagedAgent"]] = relationship(
        "ManagedAgent", back_populates="account", cascade="all, delete-orphan"
    )
    managed_agent_credentials: Mapped[List["ManagedAgentCredential"]] = relationship(
        "ManagedAgentCredential",
        back_populates="account",
        cascade="all, delete-orphan",
    )
    managed_agent_enrollments: Mapped[List["ManagedAgentEnrollment"]] = relationship(
        "ManagedAgentEnrollment",
        back_populates="account",
        cascade="all, delete-orphan",
    )

    # Many-to-many relationship helper for organizational roles

    # Property to get organizations this account owns through trackers
    @property
    def owned_organizations(self) -> List["Organization"]:
        """Get organizations owned by this account through trackers."""
        owned_orgs = []
        for tracker in self.trackers:
            owned_orgs.extend(tracker.organizations)
        return owned_orgs

    def get_active_subscription(
        self, db_session: "Session"
    ) -> Optional["Subscription"]:
        """Returns the active subscription for the account, if one exists."""
        from .plan import Subscription

        return (
            db_session.query(Subscription)
            .filter(Subscription.account_id == self.id, Subscription.status == "active")
            .first()
        )
