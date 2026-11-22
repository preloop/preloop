"""Tenant-scoped account decisions and stable pagination helpers."""

import base64
import builtins
import binascii
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, exists, func, literal, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.discovered_extension import (
    ExtensionApprovalUpdate,
    ExtensionPinCreate,
)

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
        for key, value in payload.model_dump(exclude={"pin"}).items():
            setattr(row, key, value)
        row.decided_by_user_id = decided_by_user_id
        row.decided_at = datetime.now(UTC)
        db.flush()
        if payload.pin is not None and not self._has_pin(
            db, approval_id=row.id, content_hash=payload.pin.content_hash
        ):
            # "Approve this exact content": an existing identical pin is kept.
            self.add_pin(
                db, approval=row, pin=payload.pin, pinned_by_user_id=decided_by_user_id
            )
        return row

    def _has_pin(self, db: Session, *, approval_id: UUID, content_hash: str) -> bool:
        """Report whether a hash is already pinned under an approval."""
        pin = models.ExtensionApprovalPin
        return bool(
            db.scalar(
                select(
                    exists().where(
                        pin.approval_id == approval_id,
                        pin.content_hash == content_hash,
                    )
                )
            )
        )

    def add_pin(
        self,
        db: Session,
        *,
        approval: models.ExtensionApproval,
        pin: ExtensionPinCreate,
        pinned_by_user_id: UUID,
    ) -> models.ExtensionApprovalPin:
        """Pin an approved decision to one content hash; caller owns the commit.

        Raises:
            PinStateError: The approval is not ``approved``.
            PinConflictError: The hash is already pinned, or the limit is reached.
        """
        # Lock the approval row so concurrent pins cannot pass the limit check.
        db.execute(
            select(models.ExtensionApproval.id)
            .where(models.ExtensionApproval.id == approval.id)
            .with_for_update()
        )
        if approval.approval_state != "approved":
            raise PinStateError("Only an approved extension can be pinned")
        if self._has_pin(db, approval_id=approval.id, content_hash=pin.content_hash):
            raise PinConflictError("Content hash is already pinned")
        model = models.ExtensionApprovalPin
        count = db.scalar(select(func.count()).where(model.approval_id == approval.id))
        if (count or 0) >= MAX_PINS_PER_APPROVAL:
            raise PinConflictError(
                f"An approval holds at most {MAX_PINS_PER_APPROVAL} pins"
            )
        row = model(
            approval_id=approval.id,
            content_hash=pin.content_hash,
            version=pin.version,
            note=pin.note,
            pinned_by_user_id=pinned_by_user_id,
            pinned_at=datetime.now(UTC),
        )
        db.add(row)
        db.flush()
        return row

    def delete_pin(
        self, db: Session, *, approval: models.ExtensionApproval, pin_id: UUID
    ) -> bool:
        """Delete a pin only through its own approval; False when not found."""
        model = models.ExtensionApprovalPin
        row = db.scalar(
            select(model).where(model.id == pin_id, model.approval_id == approval.id)
        )
        if row is None:
            return False
        db.delete(row)
        db.flush()
        return True

    def pins_for(
        self, db: Session, *, approval_ids: Iterable[UUID]
    ) -> dict[UUID, builtins.list[dict[str, Any]]]:
        """Return pins per approval, oldest first, with the pinning user's name."""
        ids = builtins.list(approval_ids)
        if not ids:
            return {}
        model, user = models.ExtensionApprovalPin, models.User
        rows = db.execute(
            select(model, user.username)
            .outerjoin(user, user.id == model.pinned_by_user_id)
            .where(model.approval_id.in_(ids))
            .order_by(model.pinned_at, model.id)
        )
        result: dict[UUID, builtins.list[dict[str, Any]]] = defaultdict(builtins.list)
        for pin, username in rows:
            result[pin.approval_id].append(
                dict(
                    id=pin.id,
                    content_hash=pin.content_hash,
                    version=pin.version,
                    note=pin.note,
                    pinned_by_user_id=pin.pinned_by_user_id,
                    pinned_by=username,
                    pinned_at=pin.pinned_at,
                )
            )
        return result

    def summary(
        self, db: Session, rows: builtins.list[models.ExtensionApproval]
    ) -> builtins.list[dict[str, Any]]:
        """Serialize approvals together with their pins in one extra query."""
        pins = self.pins_for(db, approval_ids=[row.id for row in rows])
        return [
            dict(
                id=row.id,
                agent_kind=row.agent_kind,
                extension_kind=row.extension_kind,
                extension_id=row.extension_id,
                approval_state=row.approval_state,
                version_constraint=row.version_constraint,
                note=row.note,
                decided_by_user_id=row.decided_by_user_id,
                decided_at=row.decided_at,
                pins=pins.get(row.id, []),
            )
            for row in rows
        ]


MAX_PINS_PER_APPROVAL = 50


class PinStateError(ValueError):
    """A pin was requested on an approval that is not approved."""


class PinConflictError(ValueError):
    """The hash is already pinned or the approval has no pin capacity left."""


def effective_state(row: Any, approval: Any | None, pins: Iterable[Any] | None) -> str:
    """Derive the state a device row is in under its account decision.

    This is the single definition; ``effective_state_sql`` mirrors it for
    filtering and the tests assert both agree on every row of the table.

    Args:
        row: Discovered extension (anything with ``content_hash``).
        approval: The matching approval, or None.
        pins: Pinned hashes, as strings or objects with ``content_hash``.

    Returns:
        ``unknown``, ``unapproved``, ``approved``, ``content_changed`` or
        ``content_unverified``.
    """
    if approval is None:
        return "unknown"
    if approval.approval_state != "approved":
        return str(approval.approval_state)
    hashes = {pin if isinstance(pin, str) else pin.content_hash for pin in pins or ()}
    if not hashes:
        return "approved"
    if row.content_hash is None:
        return "content_unverified"
    return "approved" if row.content_hash in hashes else "content_changed"


def effective_state_sql() -> Any:
    """SQL form of ``effective_state`` for an extension outer-joined to approval."""
    extension, approval = models.DiscoveredExtension, models.ExtensionApproval
    pin = models.ExtensionApprovalPin
    has_pins = exists().where(pin.approval_id == approval.id)
    pinned = exists().where(
        pin.approval_id == approval.id, pin.content_hash == extension.content_hash
    )
    return case(
        (approval.id.is_(None), literal("unknown")),
        (approval.approval_state != "approved", approval.approval_state),
        (~has_pins, literal("approved")),
        (extension.content_hash.is_(None), literal("content_unverified")),
        (pinned, literal("approved")),
        else_=literal("content_changed"),
    )


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
