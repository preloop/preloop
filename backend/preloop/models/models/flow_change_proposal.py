"""Immutable review documents for proposed flow configuration changes."""

from uuid import UUID

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class FlowChangeProposal(Base):
    """A proposal binds its author, target, base and review content by digest."""

    __tablename__ = "flow_change_proposal"

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE")
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE")
    )
    flow_id: Mapped[UUID] = mapped_column(ForeignKey("flow.id", ondelete="CASCADE"))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    document: Mapped[str] = mapped_column(Text)
