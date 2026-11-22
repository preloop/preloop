"""Tenant-scoped account decisions and stable pagination helpers."""

import base64
import binascii
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.discovered_extension import ExtensionApprovalUpdate

from .expected_device import crud_expected_device


def encode_cursor(values: list[str]) -> str:
    """Encode a public sort key without database offsets."""
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode()


def decode_cursor(cursor: str | None, size: int) -> list[str] | None:
    """Reject malformed cursors before querying storage."""
    if cursor is None:
        return None
    try:
        values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(values, list)
            or len(values) != size
            or any(not isinstance(v, str) or len(v) > 128 for v in values)
        ):
            raise ValueError("Invalid cursor")
        return values
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid cursor") from exc


class CRUDExtensionApproval:
    """Name approvals survive every discovery data purge."""

    def get(
        self,
        db: Session,
        *,
        account_id: UUID,
        agent_kind: str,
        extension_kind: str,
        extension_id: str,
    ) -> models.ExtensionApproval | None:
        """Get an account decision by its complete natural key."""
        model = models.ExtensionApproval
        return db.scalar(
            select(model).where(
                model.account_id == account_id,
                model.agent_kind == agent_kind,
                model.extension_kind == extension_kind,
                model.extension_id == extension_id,
            )
        )

    def list(
        self,
        db: Session,
        *,
        account_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[models.ExtensionApproval], str | None]:
        """Read decisions in UUID order; no cap hides subsequent pages."""
        model = models.ExtensionApproval
        query = select(model).where(model.account_id == account_id)
        key = decode_cursor(cursor, 1)
        if key:
            query = query.where(model.id > UUID(key[0]))
        rows = list(db.scalars(query.order_by(model.id).limit(limit + 1)))
        return rows[:limit], encode_cursor([str(rows[limit - 1].id)]) if len(
            rows
        ) > limit else None

    def list_all(
        self, db: Session, *, account_id: UUID
    ) -> Sequence[models.ExtensionApproval]:
        """Return every decision for an account, past the list page size."""
        model = models.ExtensionApproval
        return list(
            db.scalars(
                select(model)
                .where(model.account_id == account_id)
                .order_by(model.agent_kind, model.extension_kind, model.extension_id)
            )
        )

    def set_state(
        self,
        db: Session,
        *,
        account_id: UUID,
        agent_kind: str,
        extension_kind: str,
        extension_id: str,
        decided_by_user_id: UUID,
        payload: ExtensionApprovalUpdate,
    ) -> models.ExtensionApproval:
        """Replace a decision atomically; the caller owns the transaction."""
        crud_expected_device.lock_account(db, account_id=account_id)
        row = self.get(
            db,
            account_id=account_id,
            agent_kind=agent_kind,
            extension_kind=extension_kind,
            extension_id=extension_id,
        )
        if row is None:
            row = models.ExtensionApproval(
                account_id=account_id,
                agent_kind=agent_kind,
                extension_kind=extension_kind,
                extension_id=extension_id,
            )
            db.add(row)
        for key, value in payload.model_dump().items():
            setattr(row, key, value)
        row.decided_by_user_id = decided_by_user_id
        row.decided_at = datetime.now(UTC)
        db.flush()
        return row


def approval_join() -> Any:
    """Share the exact tenant and name join with inventory and future findings."""
    extension, approval = models.DiscoveredExtension, models.ExtensionApproval
    return (
        (extension.account_id == approval.account_id)
        & (extension.agent_kind == approval.agent_kind)
        & (extension.extension_kind == approval.extension_kind)
        & (extension.extension_id == approval.extension_id)
    )


crud_extension_approval = CRUDExtensionApproval()
