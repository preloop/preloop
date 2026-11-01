"""In-account sharing of a personal runner with a team or named users (EE #186).

A personal runner belongs to the member who registered it. The open source
rule is that only that member or an account admin may route work or start
sessions on it. ``runner_share_grant`` records an explicit grant that the EE
runner sharing plugin reads through the authorization hooks in
:mod:`preloop.plugins.account_hooks`; without the plugin nothing reads it and
the rule stays owner-or-admin.

``runner_sharing_policy`` holds the owner consent policy: one account-wide
row (``runner_id`` NULL) and optional per-runner rows. A missing per-runner
row inherits the account row; a missing account row means owner consent is
required.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

#: What a grant lets the grantee do on the runner.
RUNNER_SHARE_SCOPES = ("flows", "sessions")
#: Who a grant is for.
RUNNER_SHARE_GRANTEE_TYPES = ("team", "user")
#: ``active`` grants are enforced; ``pending_owner_consent`` grants wait for
#: the runner owner; ``declined`` grants were refused by the owner.
RUNNER_SHARE_CONSENT_STATES = ("active", "pending_owner_consent", "declined")
#: How an active grant got its consent.
RUNNER_SHARE_CONSENT_SOURCES = ("owner", "owner_accepted", "admin_override")
#: Owner consent policy values. ``required`` is the default.
RUNNER_OWNER_CONSENT_POLICIES = ("required", "not_required")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


class RunnerShareGrant(Base):
    """One grant of a runner to a team or a user in the runner's account."""

    __tablename__ = "runner_share_grant"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    runner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_runner.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        comment="Runner owner when the grant was made",
    )
    grantee_type: Mapped[str] = mapped_column(String(8), nullable=False)
    grantee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="team.id or user.id, by grantee_type",
    )
    scopes: Mapped[List[str]] = mapped_column(ARRAY(String(16)), nullable=False)
    harnesses: Mapped[Optional[List[str]]] = mapped_column(
        ARRAY(String(64)),
        nullable=True,
        comment="Harness ids the grant covers; NULL covers every harness",
    )
    directory_ids: Mapped[Optional[List[str]]] = mapped_column(
        ARRAY(String(128)),
        nullable=True,
        comment=(
            "Authorized directory ids the grant covers; NULL covers every "
            "authorized directory and tracker checkouts"
        ),
    )
    consent_state: Mapped[str] = mapped_column(String(32), nullable=False)
    consent_source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    consented_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            _in("grantee_type", RUNNER_SHARE_GRANTEE_TYPES),
            name="ck_runner_share_grant_grantee_type",
        ),
        CheckConstraint(
            _in("consent_state", RUNNER_SHARE_CONSENT_STATES),
            name="ck_runner_share_grant_consent_state",
        ),
        CheckConstraint(
            "consent_source IS NULL OR "
            + _in("consent_source", RUNNER_SHARE_CONSENT_SOURCES),
            name="ck_runner_share_grant_consent_source",
        ),
        CheckConstraint(
            "cardinality(scopes) > 0 AND scopes <@ ARRAY['flows', 'sessions']"
            "::varchar[]",
            name="ck_runner_share_grant_scopes",
        ),
        Index(
            "ix_runner_share_grant_live",
            "account_id",
            "runner_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class RunnerSharingPolicy(Base):
    """Owner consent policy for runner sharing, account-wide or per runner."""

    __tablename__ = "runner_sharing_policy"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    runner_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_runner.id", ondelete="CASCADE"),
        nullable=True,
        comment="NULL for the account-wide row",
    )
    owner_consent: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            _in("owner_consent", RUNNER_OWNER_CONSENT_POLICIES),
            name="ck_runner_sharing_policy_owner_consent",
        ),
        Index(
            "uq_runner_sharing_policy_account",
            "account_id",
            unique=True,
            postgresql_where=text("runner_id IS NULL"),
        ),
        Index(
            "uq_runner_sharing_policy_runner",
            "account_id",
            "runner_id",
            unique=True,
            postgresql_where=text("runner_id IS NOT NULL"),
        ),
    )
