"""Durable allocation cursor for best-effort execution updates."""

from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ExecutionRealtimeCursor(Base):
    """One allocation watermark per execution, independent of execution row locks."""

    __tablename__ = "execution_realtime_cursor"

    id: Mapped[UUID] = mapped_column(
        ForeignKey("flow_execution.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    lifecycle_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
