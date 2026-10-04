"""Reference-only logging (#1124).

For calls in scope of a ``sensitive_data.reference_only`` rule, no store
holds the tool arguments or the result. It holds a reference record: tool,
server, principal, rule id and decision, the values named by
``keep_fields``, HMAC-SHA256 fingerprints of the arguments and the result,
byte sizes, key names, timing and cost.

Fingerprints are keyed with a per-account secret salt stored encrypted on
the account (``utils/encryption``), identified by ``salt_id`` so a rotation
leaves old records verifiable. Plain SHA-256 is not used: an unsalted hash
of a low-entropy value (an id, a date) is brute-forceable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.models.crud import crud_account
from preloop.services.policy.schema import (
    ApproverView,
    ReferenceOnlyRule,
    SensitiveDataConfig,
)
from preloop.utils.encryption import decrypt_value, encrypt_value

logger = logging.getLogger(__name__)

#: Account metadata key holding the encrypted salts (newest last).
SALTS_META_KEY = "sensitive_data_hmac_salts"
#: Marker key on a reference record so readers can tell it from arguments.
REFERENCE_MARKER = "_preloop_reference"
#: Key under which the encrypted original arguments ride on a pending
#: approval when ``approver_view`` is ``original_until_decided``.
SEALED_ARGS_KEY = "_preloop_sealed_args"
#: Schema tag written into every record.
RECORD_SCHEMA = "preloop.sensitive_data.reference/v1"
HMAC_DOMAIN = b"preloop.sensitive_data.reference/v1\n"
MAX_KEY_NAMES = 50

_salt_lock = threading.Lock()
_salt_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_SALT_CACHE_TTL = 30.0


# ---------------------------------------------------------------------------
# Salts
# ---------------------------------------------------------------------------


def _new_salt_entry() -> Dict[str, Any]:
    return {
        "salt_id": f"salt-{uuid.uuid4().hex[:12]}",
        "encrypted": encrypt_value(secrets.token_hex(32)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def load_salts(
    db: Session, account_id: Any, *, create: bool = True
) -> List[Dict[str, Any]]:
    """The account's salt entries, oldest first. Creates the first on demand."""
    account = crud_account.get(db, id=account_id)
    if account is None:
        raise ValueError(f"Account {account_id} not found")
    meta = dict(account.meta_data or {})
    salts = list(meta.get(SALTS_META_KEY) or [])
    if not salts and create:
        salts = [_new_salt_entry()]
        meta[SALTS_META_KEY] = salts
        account.meta_data = meta
        flag_modified(account, "meta_data")
        db.add(account)
        db.commit()
        invalidate_salt_cache(account_id)
    return salts


def rotate_salt(db: Session, account_id: Any) -> str:
    """Add a new active salt. Older salts stay so old records keep verifying."""
    account = crud_account.get(db, id=account_id)
    if account is None:
        raise ValueError(f"Account {account_id} not found")
    meta = dict(account.meta_data or {})
    salts = list(meta.get(SALTS_META_KEY) or [])
    entry = _new_salt_entry()
    salts.append(entry)
    meta[SALTS_META_KEY] = salts
    account.meta_data = meta
    flag_modified(account, "meta_data")
    db.add(account)
    db.commit()
    invalidate_salt_cache(account_id)
    return entry["salt_id"]


def invalidate_salt_cache(account_id: Any = None) -> None:
    """Drop cached salts for one account or all."""
    with _salt_lock:
        if account_id is None:
            _salt_cache.clear()
        else:
            _salt_cache.pop(str(account_id), None)


def _cached_salts(account_id: Any, db: Optional[Session]) -> List[Dict[str, Any]]:
    key = str(account_id)
    now = time.monotonic()
    with _salt_lock:
        cached = _salt_cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
    if db is not None:
        salts = load_salts(db, account_id)
    else:
        from preloop.models.db.session import get_session_factory

        session = get_session_factory()()
        try:
            salts = load_salts(session, account_id)
        finally:
            session.close()
    with _salt_lock:
        _salt_cache[key] = (now + _SALT_CACHE_TTL, salts)
    return salts


def _secret(entry: Dict[str, Any]) -> bytes:
    return bytes.fromhex(decrypt_value(entry["encrypted"]))


def salt_ids(account_id: Any, db: Optional[Session] = None) -> List[str]:
    """Salt identifiers only, oldest first. Never the salts."""
    return [entry["salt_id"] for entry in _cached_salts(account_id, db)]


def fingerprint(secret: bytes, payload: Any) -> str:
    """HMAC-SHA256 over the canonical JSON of ``payload``."""
    return hmac.new(
        secret, HMAC_DOMAIN + canonical_manifest_json(payload), hashlib.sha256
    ).hexdigest()


def compute_hmac(
    account_id: Any, payload: Any, *, db: Optional[Session] = None
) -> Tuple[str, str]:
    """``(hmac_hex, salt_id)`` under the account's active (newest) salt."""
    salts = _cached_salts(account_id, db)
    entry = salts[-1]
    return fingerprint(_secret(entry), payload), entry["salt_id"]


def verify_hmac(
    account_id: Any,
    payload: Any,
    expected: str,
    *,
    salt_id: Optional[str] = None,
    db: Optional[Session] = None,
) -> Tuple[bool, Optional[str]]:
    """Equality check without storing anything.

    With ``salt_id`` only that salt is tried (old rows keep verifying after
    a rotation); without it every salt the account has is tried.
    Returns ``(matched, salt_id_that_matched)``.
    """
    salts = _cached_salts(account_id, db)
    candidates = [s for s in salts if salt_id is None or s["salt_id"] == salt_id]
    for entry in candidates:
        if hmac.compare_digest(fingerprint(_secret(entry), payload), str(expected)):
            return True, entry["salt_id"]
    return False, None


# ---------------------------------------------------------------------------
# keep_fields (JSONPath subset)
# ---------------------------------------------------------------------------


def _path_tokens(path: str) -> List[Any]:
    tokens: List[Any] = []
    rest = path[1:]  # drop "$"
    while rest:
        if rest.startswith("."):
            end = len(rest)
            for index, char in enumerate(rest[1:], start=1):
                if char in ".[":
                    end = index
                    break
            tokens.append(rest[1:end])
            rest = rest[end:]
        elif rest.startswith("[*]"):
            tokens.append("*")
            rest = rest[3:]
        elif rest.startswith("["):
            close = rest.index("]")
            tokens.append(int(rest[1:close]))
            rest = rest[close + 1 :]
        else:  # pragma: no cover - rejected by the schema regex
            raise ValueError(f"bad keep_fields path {path!r}")
    return tokens


def _walk(value: Any, tokens: Sequence[Any]) -> List[Any]:
    if not tokens:
        return [value]
    head, tail = tokens[0], tokens[1:]
    if head == "*":
        if isinstance(value, list):
            found: List[Any] = []
            for item in value:
                found.extend(_walk(item, tail))
            return found
        return []
    if isinstance(head, int):
        if isinstance(value, list) and 0 <= head < len(value):
            return _walk(value[head], tail)
        return []
    if isinstance(value, dict) and head in value:
        return _walk(value[head], tail)
    return []


def extract_keep_fields(arguments: Any, keep_fields: Sequence[str]) -> Dict[str, Any]:
    """Values named by ``keep_fields``; a ``[*]`` path yields a list."""
    kept: Dict[str, Any] = {}
    for path in keep_fields:
        values = _walk(arguments, _path_tokens(path))
        if not values:
            continue
        kept[path] = values if "[*]" in path else values[0]
    return kept


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def reference_rule_for(
    config: Optional[SensitiveDataConfig],
    *,
    tool_name: Optional[str],
    server_name: Optional[str] = None,
    managed_agent_id: Optional[str] = None,
) -> Optional[ReferenceOnlyRule]:
    """The first enabled reference-only rule whose scope covers the call."""
    if config is None or not tool_name:
        return None
    for rule in config.enabled_reference_rules():
        if rule.scope.matches(
            tool_name=tool_name,
            server_name=server_name,
            managed_agent_id=managed_agent_id,
        ):
            return rule
    return None


def _byte_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="replace"))
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _key_names(value: Any) -> List[str]:
    if isinstance(value, dict):
        names = [str(key) for key in value if not str(key).startswith("_preloop_")]
        return names[:MAX_KEY_NAMES]
    return []


def is_reference_record(value: Any) -> bool:
    """True for a record built by :func:`build_reference_record`."""
    return isinstance(value, dict) and value.get(REFERENCE_MARKER) is True


def build_reference_record(
    *,
    account_id: Any,
    rule: ReferenceOnlyRule,
    tool_name: str,
    server_name: Optional[str] = None,
    principal: Optional[Dict[str, Any]] = None,
    arguments: Any = None,
    result: Any = None,
    decision: Optional[str] = None,
    timing_ms: Optional[int] = None,
    cost: Optional[Any] = None,
    kept_redactor: Optional[Any] = None,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """Build the record that replaces arguments and results in every store.

    ``kept_redactor`` (optional callable) runs the kept values through the
    account's redact rules so a kept field cannot smuggle a value a redact
    rule would have masked.
    """
    record: Dict[str, Any] = {
        REFERENCE_MARKER: True,
        "schema": RECORD_SCHEMA,
        "rule_id": rule.id,
        "tool_name": tool_name,
        "server_name": server_name,
        "principal": principal or {},
        "decision": decision,
        "kept": {},
        "args_hmac": None,
        "result_hmac": None,
        "salt_id": None,
        "args_bytes": _byte_size(arguments),
        "result_bytes": _byte_size(result),
        "arg_keys": _key_names(arguments),
        "timing_ms": timing_ms,
        "cost": cost,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if arguments is not None:
        kept = extract_keep_fields(arguments, rule.keep_fields)
        if kept and kept_redactor is not None:
            kept = kept_redactor(kept)
        record["kept"] = kept
        record["args_hmac"], record["salt_id"] = compute_hmac(
            account_id, arguments, db=db
        )
    if result is not None:
        result_hmac, salt_id = compute_hmac(account_id, result, db=db)
        record["result_hmac"] = result_hmac
        record["salt_id"] = record["salt_id"] or salt_id
    return record


def reference_summary(record: Dict[str, Any]) -> str:
    """One-line text form for string columns (activity summaries)."""
    return (
        f"[reference-only:{record.get('rule_id')}] {record.get('tool_name')} "
        f"args_hmac={str(record.get('args_hmac') or '')[:16]} "
        f"result_hmac={str(record.get('result_hmac') or '')[:16]} "
        f"salt_id={record.get('salt_id')}"
    )


# ---------------------------------------------------------------------------
# Sealed originals for original_until_decided approvals
# ---------------------------------------------------------------------------


def wants_original_until_decided(rule: Optional[ReferenceOnlyRule]) -> bool:
    """True when approvers may see the raw arguments while pending."""
    return (
        rule is not None
        and rule.approver_view_value() == ApproverView.ORIGINAL_UNTIL_DECIDED.value
    )


def seal_original(arguments: Any) -> str:
    """Encrypt the original arguments for the pending approval row."""
    return encrypt_value(json.dumps(arguments, default=str))


def unseal_original(token: Optional[str]) -> Optional[Any]:
    """Decrypt a sealed original, or ``None`` when absent or unreadable."""
    if not token:
        return None
    try:
        return json.loads(decrypt_value(token))
    except Exception:  # noqa: BLE001 - a bad seal reads as absent
        logger.warning("Sealed approval arguments could not be read", exc_info=True)
        return None


def tool_args_for_replay(stored: Any) -> Any:
    """Arguments to re-execute after approval.

    A genuine empty original replays as ``{}``. A seal that cannot be
    decrypted is the same failure as a reference record with no original:
    replaying ``{}`` would run a side-effecting tool with the wrong input.
    """
    sealed = stored.get(SEALED_ARGS_KEY) if isinstance(stored, dict) else None
    if sealed:
        unsealed = unseal_original(sealed)
        if unsealed is None:
            raise RuntimeError(
                "This approval is reference-only: the sealed original could "
                "not be read, so the call cannot be replayed asynchronously. "
                "Re-issue the tool call while the approval is pending."
            )
        return unsealed
    if is_reference_record(stored):
        raise RuntimeError(
            "This approval is reference-only: the original arguments were "
            "never stored, so the call cannot be replayed asynchronously. "
            "Re-issue the tool call while the approval is pending."
        )
    return stored or {}


def strip_sealed_original(tool_args: Any) -> Tuple[Any, bool]:
    """Remove the sealed copy from stored arguments. Returns (args, removed)."""
    if isinstance(tool_args, dict) and SEALED_ARGS_KEY in tool_args:
        cleaned = {k: v for k, v in tool_args.items() if k != SEALED_ARGS_KEY}
        return cleaned, True
    return tool_args, False
