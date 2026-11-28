"""Persistence for per-flow state documents (``flow_state``).

Every read and write is scoped by account and flow. Writes enforce the key
grammar, the value size cap and the per-flow key cap, and support
compare-and-set through ``expected_version`` so two runs of the same flow
cannot silently overwrite each other.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models

#: Keys: lowercase, digits, dot, dash, underscore; 1-128 characters.
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
#: Keys under this prefix belong to platform features (for example the CI
#: health ledger). Agents and the REST API may read them but not write them.
RESERVED_PREFIX = "preloop."
#: Serialized JSON size cap per value.
MAX_VALUE_BYTES = 256 * 1024
#: Distinct keys one flow may hold.
MAX_KEYS_PER_FLOW = 32


class FlowStateError(ValueError):
    """A write was refused; ``code`` is stable and safe to show to callers."""

    def __init__(self, code: str, message: str) -> None:
        """Store the stable code next to the message.

        Args:
            code: Machine-readable refusal code.
            message: Human-readable explanation.
        """
        super().__init__(message)
        self.code = code


def validate_key(key: Any) -> str:
    """Return ``key`` when it matches the key grammar.

    Args:
        key: Candidate key.

    Returns:
        The key.

    Raises:
        FlowStateError: ``flow_state_key_invalid`` for anything else.
    """
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        raise FlowStateError(
            "flow_state_key_invalid",
            "key must be 1-128 characters of a-z, 0-9, '.', '-' or '_' and "
            "start with a letter or digit",
        )
    return key


def is_reserved(key: str) -> bool:
    """Whether ``key`` is owned by a platform feature.

    Args:
        key: A validated key.

    Returns:
        True for keys under :data:`RESERVED_PREFIX`.
    """
    return key.startswith(RESERVED_PREFIX)


def encoded_size(value: Any) -> int:
    """Size in bytes of ``value`` as compact JSON.

    Args:
        value: A JSON-serializable value.

    Returns:
        The UTF-8 length of its compact JSON encoding.

    Raises:
        FlowStateError: ``flow_state_value_invalid`` when not serializable.
    """
    try:
        encoded = json.dumps(value, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FlowStateError(
            "flow_state_value_invalid", "value must be JSON (no NaN or Infinity)"
        ) from exc
    return len(encoded.encode("utf-8"))


def get_state(
    db: Session, *, account_id: UUID, flow_id: UUID, key: str
) -> Optional[models.FlowState]:
    """Read one value of one flow.

    Args:
        db: Database session.
        account_id: Owning account; a row of another account is never returned.
        flow_id: Owning flow.
        key: State key.

    Returns:
        The row, or None.
    """
    return db.scalar(
        select(models.FlowState).filter_by(
            account_id=account_id, flow_id=flow_id, key=key
        )
    )


def list_states(
    db: Session, *, account_id: UUID, flow_id: UUID
) -> list[models.FlowState]:
    """List the values of one flow, ordered by key.

    Args:
        db: Database session.
        account_id: Owning account.
        flow_id: Owning flow.

    Returns:
        The rows.
    """
    return list(
        db.scalars(
            select(models.FlowState)
            .filter_by(account_id=account_id, flow_id=flow_id)
            .order_by(models.FlowState.key)
        )
    )


def put_state(
    db: Session,
    *,
    account_id: UUID,
    flow_id: UUID,
    key: str,
    value: Any,
    expected_version: Optional[int] = None,
    execution_id: Optional[UUID] = None,
    commit: bool = True,
) -> models.FlowState:
    """Create or replace one value.

    Args:
        db: Database session.
        account_id: Owning account.
        flow_id: Owning flow (must belong to ``account_id``).
        key: State key (validated here).
        value: JSON value, at most :data:`MAX_VALUE_BYTES` encoded.
        expected_version: Compare-and-set guard. ``0`` means "only if the key
            does not exist yet"; a positive number must equal the stored
            version. None writes unconditionally.
        execution_id: The flow execution that wrote the value, if any.
        commit: Commit the transaction (False to let the caller commit).

    Returns:
        The stored row with its new version.

    Raises:
        FlowStateError: invalid key or value, value too large, too many keys,
            unknown flow, or ``flow_state_version_conflict``.
    """
    validate_key(key)
    size = encoded_size(value)
    if size > MAX_VALUE_BYTES:
        raise FlowStateError(
            "flow_state_value_too_large",
            f"value is {size} bytes; the cap is {MAX_VALUE_BYTES}",
        )
    flow = db.scalar(
        select(models.Flow.id).filter_by(id=flow_id, account_id=account_id)
    )
    if flow is None:
        raise FlowStateError("flow_state_flow_not_found", "flow not found")

    if expected_version is None or expected_version == 0:
        existing = get_state(db, account_id=account_id, flow_id=flow_id, key=key)
        if existing is None:
            count = db.scalar(
                select(func.count())
                .select_from(models.FlowState)
                .filter_by(flow_id=flow_id)
            )
            if (count or 0) >= MAX_KEYS_PER_FLOW:
                raise FlowStateError(
                    "flow_state_too_many_keys",
                    f"a flow holds at most {MAX_KEYS_PER_FLOW} keys",
                )
            inserted = db.execute(
                insert(models.FlowState)
                .values(
                    account_id=account_id,
                    flow_id=flow_id,
                    key=key,
                    value=value,
                    version=1,
                    size_bytes=size,
                    updated_by_execution_id=execution_id,
                )
                .on_conflict_do_nothing(constraint="uq_flow_state_key")
                .returning(models.FlowState.id)
            ).scalar()
            if inserted is not None:
                row = db.get(models.FlowState, inserted)
                assert row is not None
                if commit:
                    db.commit()
                else:
                    db.flush()
                return row
        if expected_version == 0:
            raise FlowStateError(
                "flow_state_version_conflict", "the key already exists"
            )

    row = db.scalar(
        select(models.FlowState)
        .filter_by(account_id=account_id, flow_id=flow_id, key=key)
        .with_for_update()
    )
    if row is None:
        raise FlowStateError(
            "flow_state_version_conflict", "the key does not exist yet"
        )
    if expected_version is not None and row.version != expected_version:
        raise FlowStateError(
            "flow_state_version_conflict",
            f"stored version is {row.version}, expected {expected_version}",
        )
    row.value = value
    row.version = row.version + 1
    row.size_bytes = size
    row.updated_by_execution_id = execution_id
    row.updated_at = func.now()
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(row)
    return row


def delete_state(db: Session, *, account_id: UUID, flow_id: UUID, key: str) -> bool:
    """Delete one value.

    Args:
        db: Database session.
        account_id: Owning account.
        flow_id: Owning flow.
        key: State key.

    Returns:
        True when a row was deleted.
    """
    row = get_state(db, account_id=account_id, flow_id=flow_id, key=key)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
