"""REST traffic records: per-minute rollups plus individual problem events.

Every ``/api/v1`` request is counted, whatever credential it carried (login
session, API key, runtime key, CI identity) and whatever its status. Success
traffic is aggregated per minute so a client that polls one route many times
a minute costs at most one row per minute and series. Every 4xx and 5xx is
also kept as its own row, as is a list that came back empty right after a
create in the same scope.

Only route templates are stored (``/api/v1/flows/{flow_id}``), never the raw
path, query values, headers or bodies.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger, DateTime, Integer, SmallInteger, String

from .base import Base

#: Principal kinds recorded on traffic rows.
PRINCIPAL_KINDS = (
    "session",
    "api_key",
    "runtime_key",
    "ci_identity",
    "anonymous",
)

#: Event kinds kept as individual rows.
EVENT_KIND_ERROR = "error"
EVENT_KIND_EMPTY_AFTER_CREATE = "empty_after_create"


class ApiTrafficRollup(Base):
    """Request counts for one minute, account, principal kind, route and class.

    Attributes:
        bucket_start: Start of the UTC minute the requests began in.
        account_id: Account of the authenticated principal; NULL when the
            request could not be attributed (for example a rejected token).
        principal_kind: One of ``PRINCIPAL_KINDS``.
        method: HTTP method.
        route_template: Matched route template, or ``<unmatched>``.
        status_class: First digit of the status (2, 3, 4 or 5).
        request_count: Requests in the bucket.
        total_duration_ms: Sum of request durations in milliseconds.
        max_duration_ms: Slowest request in the bucket, in milliseconds.
    """

    __tablename__ = "api_traffic_rollup"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, index=False, default=uuid.uuid4
    )
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=True,
    )
    principal_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    route_template: Mapped[str] = mapped_column(String(255), nullable=False)
    status_class: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_duration_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    max_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "bucket_start",
            "principal_kind",
            "method",
            "route_template",
            "status_class",
            name="uq_api_traffic_rollup_key",
            postgresql_nulls_not_distinct=True,
        ),
        # Retention deletes by age across accounts.
        Index("ix_api_traffic_rollup_bucket", "bucket_start"),
    )


class ApiTrafficEvent(Base):
    """One problem request: a 4xx or 5xx, or an empty list after a create.

    Attributes:
        occurred_at: When the request began (UTC).
        kind: ``error`` or ``empty_after_create``.
        account_id: Account of the principal; NULL when unattributed.
        principal_kind: One of ``PRINCIPAL_KINDS``.
        principal_id: User, API key or CI principal id, when known.
        method: HTTP method.
        route_template: Matched route template, or ``<unmatched>``.
        status_code: HTTP status returned.
        duration_ms: Request duration in milliseconds.
        reason_code: Short first-party code explaining the outcome, when the
            handler set one (for example a flow delete blocker).
    """

    __tablename__ = "api_traffic_event"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, index=False, default=uuid.uuid4
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=True,
    )
    principal_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    principal_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    route_template: Mapped[str] = mapped_column(String(255), nullable=False)
    status_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # The account health read: one account, a bounded window.
        Index("ix_api_traffic_event_account_time", "account_id", "occurred_at"),
        # Retention deletes by age across accounts.
        Index("ix_api_traffic_event_time", "occurred_at"),
    )
