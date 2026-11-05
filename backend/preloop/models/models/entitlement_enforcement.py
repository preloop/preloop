"""Application-owned global and account entitlement enforcement decisions."""

import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EntitlementEnforcementSetting(Base):
    """A null decision inherits the deployment default or global setting."""

    __tablename__ = "entitlement_enforcement_setting"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'global' AND account_id IS NULL) OR "
            "(account_id IS NOT NULL AND scope = CAST(account_id AS VARCHAR))",
            name="ck_entitlement_enforcement_scope",
        ),
    )

    scope: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), nullable=True
    )
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
