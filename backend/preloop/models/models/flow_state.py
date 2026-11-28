"""Per-flow persistent state: small JSON documents a flow keeps across runs."""

from typing import Any, Optional
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class FlowState(Base):
    """One named JSON value owned by a flow.

    A flow run reads the values its previous runs wrote (a ledger, a cursor,
    a baseline) instead of re-deriving them. ``version`` increases on every
    write so concurrent writers can use compare-and-set. Keys under the
    reserved ``preloop.`` prefix are written only by platform features.
    """

    __tablename__ = "flow_state"
    __table_args__ = (UniqueConstraint("flow_id", "key", name="uq_flow_state_key"),)

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    flow_id: Mapped[UUID] = mapped_column(
        ForeignKey("flow.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[Any] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    updated_by_execution_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
