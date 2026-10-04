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


def parse_sensitive_data_config(raw: Any) -> SensitiveDataConfig:
    """Parse the stored block; anything invalid becomes the empty config."""
    if not isinstance(raw, dict) or not raw:
        return SensitiveDataConfig()
    try:
        return SensitiveDataConfig.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - a bad block must not break reads
        logger.warning("Ignoring invalid sensitive_data policy block: %s", exc)
        return SensitiveDataConfig()


def serialize_sensitive_data_config(config: SensitiveDataConfig) -> Dict[str, Any]:
    """JSON form stored on the account and exported to YAML.

    Defaults are left out so an empty block serialises to ``{}`` and a
    stored rule carries only what the operator wrote.
    """
    return config.model_dump(exclude_none=True, exclude_defaults=True, mode="json")


def load_sensitive_data_config(db: Session, account_id: Any) -> SensitiveDataConfig:
    """Load the account's block. Missing account or block: empty config."""
    try:
        account = crud_account.get(db, id=account_id)
    except Exception as exc:  # noqa: BLE001 - never fail the caller on a read
        logger.warning("sensitive_data config unavailable: %s", type(exc).__name__)
        return SensitiveDataConfig()
    meta = getattr(account, "meta_data", None) if account is not None else None
    if not isinstance(meta, dict):
        return SensitiveDataConfig()
    return parse_sensitive_data_config(meta.get(SENSITIVE_DATA_META_KEY))


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
    from preloop.services.sensitive_data.storage import invalidate_cache

    invalidate_cache(account_id)
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
