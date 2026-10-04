"""Agents reported by opt-in workstation discovery that are not governed yet.

A workstation running ``preloop agents discover --report`` sends one row per
agent tool it found. Nothing here identifies a person or a machine in clear:
the workstation is an HMAC of its machine id under the account's discovery
salt, and the config location is an HMAC of a home-relative path under the
same salt. See ``docs/guide/agent-discovery-reporting.md`` for the full list
of what is and is not sent.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .managed_agent import ManagedAgent

#: Reported, nobody has acted on it.
CANDIDATE_STATUS_NEW = "new"
#: An enrollment from the same workstation was validated for this kind.
CANDIDATE_STATUS_ONBOARDED = "onboarded"
#: An admin decided this candidate does not need governing.
CANDIDATE_STATUS_IGNORED = "ignored"

CANDIDATE_STATUSES: tuple[str, ...] = (
    CANDIDATE_STATUS_NEW,
    CANDIDATE_STATUS_ONBOARDED,
    CANDIDATE_STATUS_IGNORED,
)


class DiscoveredAgentCandidate(Base):
    """One agent tool seen on one workstation, keyed by salted hashes."""

    __tablename__ = "discovered_agent_candidate"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "workstation_fingerprint",
            "agent_kind",
            "config_path_hash",
            name="uq_discovered_agent_candidate_key",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Hex HMAC-SHA256 of the machine id under the account discovery salt.
    workstation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Hex HMAC-SHA256 of the home-relative config path under the same salt.
    config_path_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    mcp_server_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The CLI's own view: a local or server enrollment existed at report time.
    reported_enrolled: Mapped[bool] = mapped_column(nullable=False, default=False)
    os_family: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    cli_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CANDIDATE_STATUS_NEW
    )
    managed_agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("managed_agent.id", ondelete="SET NULL"),
        nullable=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(nullable=False, index=True)

    managed_agent: Mapped[Optional["ManagedAgent"]] = relationship("ManagedAgent")


class AccountDiscoverySalt(Base):
    """Per-account secret the CLI keys its workstation fingerprint with.

    Issued on first request and never rotated by the server: rotating it
    would turn every workstation into a new candidate.
    """

    __tablename__ = "account_discovery_salt"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    salt: Mapped[str] = mapped_column(String(64), nullable=False)
