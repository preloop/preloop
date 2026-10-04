"""Storage-time redaction hook (#1123).

``apply_storage_redaction(account_id, obj, scope=...)`` resolves the
account's enabled ``sensitive_data`` rules with action ``redact`` that
cover the given scope and rewrites string leaves of ``obj`` before a row is
written. Call it where a write path already runs the credential scrubbers
(``redact_dict``, ``scrub_secrets``, ``redact_text``): credentials first,
then this.

The account block is cached per process for a few seconds and dropped on
every write through ``policy_store.replace_sensitive_data_config``, so a
policy change takes effect on the next call in the same process and within
the TTL elsewhere. Every failure degrades to "no PII redaction" and is
logged: a broken policy read must not lose the row, and the credential
scrub already ran.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from preloop.services.policy.schema import (
    ConditionAction,
    SensitiveDataConfig,
    SensitiveDataRule,
)
from preloop.services.sensitive_data.detectors import DetectorConfig
from preloop.services.sensitive_data.redact import Counts, redact_structure

logger = logging.getLogger(__name__)

REDACT = ConditionAction.REDACT.value

#: How long a resolved account block is reused before it is re-read.
CACHE_TTL_SECONDS = 5.0

_lock = threading.Lock()
_cache: Dict[str, Tuple[float, Optional[SensitiveDataConfig]]] = {}


@dataclass(frozen=True)
class StorageScope:
    """Where a stored value came from, for rule scope matching.

    ``target`` narrows to rules watching that payload (``tool.args``,
    ``tool.result``, ``model.request``, ``model.response``); ``None`` means
    any redact rule in scope applies, which is right for mixed stores such
    as session search documents and flow logs.
    """

    target: Optional[str] = None
    tool_name: Optional[str] = None
    server_name: Optional[str] = None
    managed_agent_id: Optional[str] = None


def invalidate_cache(account_id: Any = None) -> None:
    """Drop the cached block for one account, or for every account."""
    with _lock:
        if account_id is None:
            _cache.clear()
        else:
            _cache.pop(str(account_id), None)


def _load_config(account_id: Any) -> Optional[SensitiveDataConfig]:
    """Read the account block with its own short-lived session."""
    from preloop.models.db.session import get_session_factory
    from preloop.services.sensitive_data.policy_store import (
        load_sensitive_data_config,
    )

    session = get_session_factory()()
    try:
        return load_sensitive_data_config(session, account_id)
    finally:
        session.close()


def resolve_config(account_id: Any) -> Optional[SensitiveDataConfig]:
    """Cached account block. ``None`` when it cannot be read."""
    if account_id is None:
        return None
    key = str(account_id)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
    try:
        config = _load_config(account_id)
    except Exception as exc:  # noqa: BLE001 - degrade to no PII redaction
        logger.warning(
            "sensitive_data policy unavailable for storage redaction: %s",
            type(exc).__name__,
        )
        return None
    with _lock:
        _cache[key] = (now + CACHE_TTL_SECONDS, config)
    return config


def redact_rules_for(
    config: Optional[SensitiveDataConfig], scope: Optional[StorageScope]
) -> List[SensitiveDataRule]:
    """Enabled redact rules whose scope and (when set) target match."""
    if config is None:
        return []
    scope = scope or StorageScope()
    rules: List[SensitiveDataRule] = []
    for rule in config.enabled_rules():
        if rule.action_value() != REDACT:
            continue
        if scope.target is not None and scope.target not in rule.target_values():
            continue
        if not rule.scope.matches(
            tool_name=scope.tool_name,
            server_name=scope.server_name,
            managed_agent_id=scope.managed_agent_id,
        ):
            continue
        rules.append(rule)
    return rules


def detector_config_for(
    config: SensitiveDataConfig, rules: List[SensitiveDataRule]
) -> DetectorConfig:
    """Detector configuration covering the union of the rules' types."""
    from preloop.services.sensitive_data.policy_store import detector_config_from

    types: List[str] = []
    for rule in rules:
        for item in config.types_for_rule(rule):
            if item not in types:
                types.append(item)
    return detector_config_from(config).with_types(types)


def redact_for_storage(
    account_id: Any, obj: Any, *, scope: Optional[StorageScope] = None
) -> Tuple[Any, Counts, List[SensitiveDataRule]]:
    """``(redacted, counts_by_type, rules_applied)`` for one value."""
    if obj is None or obj == "" or obj == {} or obj == []:
        return obj, {}, []
    config = resolve_config(account_id)
    rules = redact_rules_for(config, scope)
    if not rules or config is None:
        return obj, {}, []
    try:
        redacted, counts = redact_structure(obj, detector_config_for(config, rules))
    except Exception:  # noqa: BLE001 - never lose the row over a detector bug
        logger.warning("Storage redaction failed; storing unredacted", exc_info=True)
        return obj, {}, []
    return redacted, counts, rules


def apply_storage_redaction(
    account_id: Any, obj: Any, *, scope: Optional[StorageScope] = None
) -> Any:
    """Return ``obj`` with the account's redact rules applied.

    Byte-identical to the input when no redact rule is in scope.
    """
    redacted, _counts, _rules = redact_for_storage(account_id, obj, scope=scope)
    return redacted
