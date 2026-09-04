"""Account model."""

import uuid
from datetime import datetime

# Use TYPE_CHECKING to avoid circular imports
from typing import TYPE_CHECKING, Dict, List, Optional

from sqlalchemy import DateTime, func, String, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
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
