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


def prime_cache(account_id: Any, config: Optional[SensitiveDataConfig]) -> None:
    """Store a block a caller already read off-loop, so later redaction calls
    on the same request do not touch the database."""
    if account_id is None:
        return
    with _lock:
        _cache[str(account_id)] = (time.monotonic() + CACHE_TTL_SECONDS, config)


def cached_config(account_id: Any) -> Optional[SensitiveDataConfig]:
    """The cached block when present and fresh, else ``None`` (no read)."""
    if account_id is None:
        return None
    with _lock:
        cached = _cache.get(str(account_id))
    if cached is None or cached[0] <= time.monotonic():
        return None
    return cached[1]


def has_cached_config(account_id: Any) -> bool:
    """True when :func:`cached_config` would not need a database read."""
    if account_id is None:
        return False
    with _lock:
        cached = _cache.get(str(account_id))
    return cached is not None and cached[0] > time.monotonic()


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
    account_id: Any,
    obj: Any,
    *,
    scope: Optional[StorageScope] = None,
    config: Optional[SensitiveDataConfig] = None,
) -> Tuple[Any, Counts, List[SensitiveDataRule]]:
    """``(redacted, counts_by_type, rules_applied)`` for one value.

    ``config`` lets a caller that already read the block (off-loop) pass it
    in; otherwise the cached block is used, read on a miss.
    """
    if obj is None or obj == "" or obj == {} or obj == []:
        return obj, {}, []
    if config is None:
        config = resolve_config(account_id)
    if config is None:
        return obj, {}, []
    reference = reference_record_for_storage(
        account_id, obj, scope=scope, config=config
    )
    if reference is not None:
        return reference, {}, []
    rules = redact_rules_for(config, scope)
    if not rules:
        return obj, {}, []
    try:
        redacted, counts = redact_structure(obj, detector_config_for(config, rules))
    except Exception:  # noqa: BLE001 - never lose the row over a detector bug
        logger.warning("Storage redaction failed; storing unredacted", exc_info=True)
        return obj, {}, []
    return redacted, counts, rules


def reference_record_for_storage(
    account_id: Any,
    obj: Any,
    *,
    scope: Optional[StorageScope],
    config: SensitiveDataConfig,
) -> Optional[Any]:
    """The reference record that replaces ``obj`` for a reference-only call.

    Applies when the scope names a tool covered by an enabled
    ``reference_only`` rule (#1124). ``tool.args`` scopes fingerprint the
    arguments, ``tool.result`` scopes the result, other scopes whatever
    was handed in. Kept fields pass through the redact rules in scope.
    String stores receive the one-line summary. ``None`` when no rule
    applies or the record cannot be built (the caller then redacts).
    """
    from preloop.services.sensitive_data import reference as reference_module

    if scope is None or not scope.tool_name:
        return None
    if reference_module.is_reference_record(obj):
        return obj
    rule = reference_module.reference_rule_for(
        config,
        tool_name=scope.tool_name,
        server_name=scope.server_name,
        managed_agent_id=scope.managed_agent_id,
    )
    if rule is None:
        return None
    redact_rules = redact_rules_for(config, scope)

    def kept_redactor(kept: Dict[str, Any]) -> Dict[str, Any]:
        if not redact_rules:
            return kept
        return redact_structure(kept, detector_config_for(config, redact_rules))[0]

    try:
        if scope.target == "tool.result":
            record = reference_module.build_reference_record(
                account_id=account_id,
                rule=rule,
                tool_name=scope.tool_name,
                server_name=scope.server_name,
                principal={"managed_agent_id": scope.managed_agent_id},
                result=obj,
                kept_redactor=kept_redactor,
            )
        else:
            record = reference_module.build_reference_record(
                account_id=account_id,
                rule=rule,
                tool_name=scope.tool_name,
                server_name=scope.server_name,
                principal={"managed_agent_id": scope.managed_agent_id},
                arguments=obj,
                kept_redactor=kept_redactor,
            )
    except Exception:  # noqa: BLE001 - fall back to redaction, never to raw
        logger.warning("Reference record could not be built", exc_info=True)
        return None
    if isinstance(obj, str):
        return reference_module.reference_summary(record)
    return record


def apply_storage_redaction(
    account_id: Any,
    obj: Any,
    *,
    scope: Optional[StorageScope] = None,
    config: Optional[SensitiveDataConfig] = None,
) -> Any:
    """Return ``obj`` with the account's redact rules applied.

    Byte-identical to the input when no redact rule is in scope. Callers on
    an event loop should pass ``config`` (read off-loop) or prime the cache
    first; a cache miss reads the policy synchronously.
    """
    redacted, _counts, _rules = redact_for_storage(
        account_id, obj, scope=scope, config=config
    )
    return redacted
