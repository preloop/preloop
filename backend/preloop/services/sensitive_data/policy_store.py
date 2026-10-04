"""Persistence of the ``sensitive_data`` policy block.

The block lives on ``account.meta_data['sensitive_data']`` next to
``model_io_rules`` so the YAML editor, the console and the evaluators share
one store. Readers never raise: a missing or malformed block resolves to the
empty configuration so a bad write can never take detection or the gateway
down.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.models.crud import crud_account
from preloop.services.policy.schema import SensitiveDataConfig
from preloop.services.sensitive_data.detectors import DetectorConfig

logger = logging.getLogger(__name__)

SENSITIVE_DATA_META_KEY = "sensitive_data"


class SensitiveDataPolicyError(RuntimeError):
    """The stored block could not be read or parsed (strict readers only)."""


def parse_sensitive_data_config(
    raw: Any, *, strict: bool = False
) -> SensitiveDataConfig:
    """Parse the stored block.

    Lenient (default): anything invalid becomes the empty config, for
    readers that must never fail (exports, the console). Strict: a
    malformed block raises :class:`SensitiveDataPolicyError`, for enforcement
    paths that must fail closed rather than run unscanned.
    """
    if not isinstance(raw, dict) or not raw:
        return SensitiveDataConfig()
    try:
        return SensitiveDataConfig.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - a bad block must not break reads
        if strict:
            raise SensitiveDataPolicyError(
                f"stored sensitive_data block is invalid: {exc}"
            ) from exc
        logger.warning("Ignoring invalid sensitive_data policy block: %s", exc)
        return SensitiveDataConfig()


def serialize_sensitive_data_config(config: SensitiveDataConfig) -> Dict[str, Any]:
    """JSON form stored on the account and exported to YAML."""
    return config.model_dump(exclude_none=True, mode="json")


def load_sensitive_data_config(
    db: Session, account_id: Any, *, strict: bool = False
) -> SensitiveDataConfig:
    """Load the account's block. Missing account or block: empty config.

    With ``strict=True`` a database error or a malformed block raises
    :class:`SensitiveDataPolicyError` instead of reading as "no rules".
    """
    try:
        account = crud_account.get(db, id=account_id)
    except Exception as exc:  # noqa: BLE001 - never fail a lenient read
        if strict:
            raise SensitiveDataPolicyError(
                f"sensitive_data config unavailable: {type(exc).__name__}"
            ) from exc
        logger.warning("sensitive_data config unavailable: %s", type(exc).__name__)
        return SensitiveDataConfig()
    meta = getattr(account, "meta_data", None) if account is not None else None
    if not isinstance(meta, dict):
        return SensitiveDataConfig()
    return parse_sensitive_data_config(meta.get(SENSITIVE_DATA_META_KEY), strict=strict)


def replace_sensitive_data_config(
    db: Session, account_id: Any, config: Optional[SensitiveDataConfig]
) -> SensitiveDataConfig:
    """Replace the account's block and flush. ``None`` or empty removes it."""
    account = crud_account.get(db, id=account_id)
    if account is None:
        raise ValueError(f"Account {account_id} not found")
    meta = dict(account.meta_data or {})
    serialized = serialize_sensitive_data_config(config) if config else {}
    if serialized:
        meta[SENSITIVE_DATA_META_KEY] = serialized
    else:
        meta.pop(SENSITIVE_DATA_META_KEY, None)
    account.meta_data = meta
    flag_modified(account, "meta_data")
    db.add(account)
    db.flush()
    return config or SensitiveDataConfig()


def detector_config_from(config: Optional[SensitiveDataConfig]) -> DetectorConfig:
    """Runtime detector configuration for a stored block."""
    if config is None or config.detectors is None:
        return DetectorConfig()
    return DetectorConfig.from_mapping(
        config.detectors.model_dump(exclude_none=True, mode="json")
    )


def load_detector_config(db: Session, account_id: Any) -> DetectorConfig:
    """Runtime detector configuration for ``account_id``. Never raises."""
    return detector_config_from(load_sensitive_data_config(db, account_id))
