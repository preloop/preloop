"""Extension content for review: the account setting, limits and verification.

A collector uploads the text behind an extension's content hash so an approver
can read what they approve. The text is redacted on the device before it is
sent. This module holds what does not need a database session:

* the account setting in ``Account.meta_data["extension_content"]``
  (``collect_enabled``, default True, and the one-time administrator notice);
* the server-side size limits;
* :func:`verify_item`, which recomputes the inventory content hash from the
  uploaded manifest and checks every text that was sent unredacted against
  its own hash, so a stored row shows the bytes behind its hash wherever that
  can be proven.

Nothing here logs or returns uploaded text. Error values are fixed codes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional
from uuid import UUID

META_KEY = "extension_content"

AUDIT_SETTINGS_UPDATED = "extension_content.settings_updated"
AUDIT_NOTICE_ACKNOWLEDGED = "extension_content.notice_acknowledged"
AUDIT_READ = "extension_content.read"
AUDIT_DELETED = "extension_content.deleted"

#: Text of one file or hook command, UTF-8 bytes after redaction.
MAX_TEXT_BYTES = 256 * 1024
#: All stored text of one extension.
MAX_ITEM_BYTES = 1024 * 1024
#: File entries of one extension: the bound of the inventory tree hash.
MAX_ITEM_ENTRIES = 500
#: Hook commands of one extension.
MAX_ITEM_HOOKS = 200
#: Extensions in one upload request.
MAX_UPLOAD_ITEMS = 100
#: All stored text of one upload request.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
#: Hashes in one "which do you lack" request.
MAX_QUERY_HASHES = 1000
#: All stored text of one account. Uploads past it are refused, not evicted.
MAX_ACCOUNT_BYTES = 256 * 1024 * 1024
#: Content nobody reports any more is deleted after the discovery window, or
#: sooner when the account's plan keeps history for less.
RETENTION_DAYS = 90

REJECT_HASH_MISMATCH = "hash_mismatch"
REJECT_TEXT_MISMATCH = "text_mismatch"
REJECT_TOO_LARGE = "too_large"
REJECT_MALFORMED = "malformed"
REJECT_PRIVATE_KEY = "private_key_present"
REJECT_NOT_REPORTED = "not_in_inventory"
REJECT_BUDGET = "account_budget_exhausted"

_PRIVATE_KEY = re.compile(
    r"-----\s*BEGIN[\sA-Z0-9_-]{0,100}PRIVATE\s+KEY", re.IGNORECASE
)
_TREE_KINDS = frozenset({"skill", "plugin"})


class ContentRejectedError(ValueError):
    """An uploaded item failed a server-side check. ``code`` is a fixed word."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExtensionContentSettings:
    """Resolved account setting."""

    collect_enabled: bool
    notice_acknowledged_at: Optional[str]
    updated_by_user_id: Optional[str]
    updated_at: Optional[str]


@dataclass
class VerifiedItem:
    """Outcome of :func:`verify_item`: the document to encrypt and its counts."""

    document: dict[str, Any]
    stored_bytes: int = 0
    entry_count: int = 0
    stored_count: int = 0
    withheld_count: int = 0
    redaction_count: int = 0
    verification: str = "none"
    redacted: bool = False
    withheld_reason: Optional[str] = None
    verified_texts: int = field(default=0, repr=False)


def _store(meta_data: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    raw = (meta_data or {}).get(META_KEY) if isinstance(meta_data, Mapping) else None
    return raw if isinstance(raw, Mapping) else {}


def resolve(meta_data: Optional[Mapping[str, Any]]) -> ExtensionContentSettings:
    """Read the setting. Collection is on unless it was explicitly turned off."""
    store = _store(meta_data)
    return ExtensionContentSettings(
        # Only a literal False opts out; the founder decision is on by default.
        collect_enabled=store.get("collect_enabled") is not False,
        notice_acknowledged_at=store.get("notice_acknowledged_at"),
        updated_by_user_id=store.get("updated_by_user_id"),
        updated_at=store.get("updated_at"),
    )


def is_enabled(account: Any) -> bool:
    """Whether ``account`` accepts extension content."""
    return resolve(getattr(account, "meta_data", None)).collect_enabled


def apply_update(
    meta_data: Optional[Mapping[str, Any]],
    *,
    collect_enabled: Optional[bool],
    acknowledge_notice: bool,
    user_id: UUID,
    now: datetime,
) -> dict[str, Any]:
    """Return new ``meta_data`` with the change applied; other keys untouched.

    Changing the switch also acknowledges the notice: whoever flips it has
    seen what it controls.
    """
    updated = dict(meta_data or {})
    store = dict(_store(meta_data))
    if collect_enabled is None and not acknowledge_notice:
        return updated
    if collect_enabled is not None:
        store["collect_enabled"] = bool(collect_enabled)
        store["updated_by_user_id"] = str(user_id)
        store["updated_at"] = now.isoformat()
    if not store.get("notice_acknowledged_at"):
        store["notice_acknowledged_at"] = now.isoformat()
        store["notice_acknowledged_by_user_id"] = str(user_id)
    updated[META_KEY] = store
    return updated


def retention_days(plan_days: Optional[int]) -> int:
    """Days content is kept after its last report.

    Args:
        plan_days: The account's plan history retention; ``-1`` is unlimited
            and ``0`` or None means the plan states none.

    Returns:
        The discovery window, shortened to the plan's retention when the plan
        keeps history for less. A longer plan never lengthens it.
    """
    if plan_days is None or plan_days <= 0:
        return RETENTION_DAYS
    return min(RETENTION_DAYS, int(plan_days))


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_hash(entries: list[tuple[str, Optional[str]]]) -> str:
    """Recompute the collector's tree hash from ``(path, sha256 or None)``.

    A None digest is a symlink entry. Entries are sorted by path as raw bytes.
    """
    lines = []
    for path, digest in sorted(entries, key=lambda item: item[0].encode("utf-8")):
        lines.append(
            path.encode("utf-8") + b"\x00" + (digest or "symlink").encode() + b"\n"
        )
    return "sha256:" + _sha256_hex(b"".join(lines))


def hook_hash(command_hashes: list[str]) -> str:
    """Recompute the collector's hook hash from its per-command hashes."""
    return "sha256:" + _sha256_hex("\n".join(sorted(command_hashes)).encode())


def matcher_set(hooks: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Describe which matcher each command of a hook runs under.

    The hook content hash covers the command strings only, because that is
    what the inventory hash covers. The matcher decides when a command runs
    and is not in that hash, so it is recorded beside the content as a set
    with its own digest. A pin on the content hash does not cover it.
    """
    pairs = sorted((hook.get("matcher") or "", hook["command_hash"]) for hook in hooks)
    digest = _sha256_hex("\n".join(f"{m}\x00{h}" for m, h in pairs).encode("utf-8"))
    return {
        "digest": "sha256:" + digest,
        "matchers": [{"matcher": m or None, "command_hash": h} for m, h in pairs],
    }


def _safe_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or len(path.encode()) > 1024:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _check_text(text: str, digest_hex: Optional[str], redactions: int) -> bool:
    """Validate one stored text; return True when it matched its hash.

    Raises:
        ContentRejectedError: too large, holds a private key, or was sent as
            unredacted but does not hash to the digest the inventory reported.
    """
    raw = text.encode("utf-8")
    if len(raw) > MAX_TEXT_BYTES:
        raise ContentRejectedError(REJECT_TOO_LARGE)
    if "\x00" in text:
        raise ContentRejectedError(REJECT_MALFORMED)
    if _PRIVATE_KEY.search(text):
        # The collector withholds such files. One that arrives anyway came
        # from a broken or modified collector and is not stored.
        raise ContentRejectedError(REJECT_PRIVATE_KEY)
    if redactions:
        return False
    if digest_hex is None or _sha256_hex(raw) != digest_hex:
        raise ContentRejectedError(REJECT_TEXT_MISMATCH)
    return True


def stored_text_bytes(document: Mapping[str, Any]) -> int:
    """Count UTF-8 file, command and matcher text, including matcher history."""
    texts = [entry.get("text") or "" for entry in document.get("files") or []]
    for hook in document.get("hooks") or []:
        texts.extend((hook.get("command") or "", hook.get("matcher") or ""))
    for assignment in document.get("matcher_sets") or []:
        texts.extend(
            matcher.get("matcher") or "" for matcher in assignment.get("matchers") or []
        )
    return sum(len(text.encode("utf-8")) for text in texts)


def verify_item(item: Mapping[str, Any]) -> VerifiedItem:
    """Check one uploaded item against its content hash and the limits.

    Args:
        item: A validated ``ExtensionContentItem`` as a plain mapping.

    Returns:
        The document to store and its counts. Each file and hook in the
        document carries ``verified``: True when its text was received
        unredacted and matched its hash.

    Raises:
        ContentRejectedError: with one of the ``REJECT_*`` codes.
    """
    kind = item["extension_kind"]
    content_hash = item["content_hash"]
    files = list(item.get("files") or [])
    hooks = list(item.get("hooks") or [])
    withheld = item.get("withheld")
    result = VerifiedItem(document={"v": 1, "files": [], "hooks": []})
    if withheld:
        if files or hooks:
            raise ContentRejectedError(REJECT_MALFORMED)
        result.withheld_reason = withheld
        result.document["withheld"] = withheld
        return result
    if kind == "hook":
        if files or not hooks or len(hooks) > MAX_ITEM_HOOKS:
            raise ContentRejectedError(REJECT_MALFORMED)
        if hook_hash([hook["command_hash"] for hook in hooks]) != content_hash:
            raise ContentRejectedError(REJECT_HASH_MISMATCH)
    else:
        if hooks or not files or len(files) > MAX_ITEM_ENTRIES:
            raise ContentRejectedError(REJECT_MALFORMED)
        paths = [entry["path"] for entry in files]
        if len(set(paths)) != len(paths) or not all(_safe_path(p) for p in paths):
            raise ContentRejectedError(REJECT_MALFORMED)
        if kind in _TREE_KINDS:
            entries = [
                (
                    entry["path"],
                    None if entry["status"] == "symlink" else entry["sha256"],
                )
                for entry in files
            ]
            if tree_hash(entries) != content_hash:
                raise ContentRejectedError(REJECT_HASH_MISMATCH)
        elif len(files) != 1 or f"sha256:{files[0]['sha256']}" != content_hash:
            raise ContentRejectedError(REJECT_HASH_MISMATCH)

    redacted_texts = 0
    for entry in files:
        record = _record(entry, entry.get("text"), entry.get("sha256"), result)
        record.update(path=entry["path"], sha256=entry.get("sha256"))
        record["size"] = entry.get("size")
        redacted_texts += int(record["redactions_total"] > 0 and "text" in record)
        record.pop("redactions_total")
        result.document["files"].append(record)
    for hook in hooks:
        digest = hook["command_hash"].removeprefix("sha256:")
        record = _record(hook, hook.get("command"), digest, result)
        if "text" in record:
            record["command"] = record.pop("text")
        matcher_redactions = dict(hook.get("matcher_redactions") or {})
        record.update(
            matcher=hook.get("matcher"),
            command_hash=hook["command_hash"],
            matcher_redactions=matcher_redactions,
        )
        result.redaction_count += sum(matcher_redactions.values())
        redacted_texts += int(record["redactions_total"] > 0 and "command" in record)
        record.pop("redactions_total")
        result.document["hooks"].append(record)
    if hooks:
        result.document["matcher_sets"] = [matcher_set(hooks)]
    result.stored_bytes = stored_text_bytes(result.document)
    result.entry_count = len(files) + len(hooks)
    result.withheld_count = result.entry_count - result.stored_count
    result.redacted = result.redaction_count > 0
    if result.stored_bytes > MAX_ITEM_BYTES:
        raise ContentRejectedError(REJECT_TOO_LARGE)
    if result.stored_count == 0:
        result.verification = "none"
    elif redacted_texts == 0:
        result.verification = "verified"
    elif result.verified_texts == 0:
        result.verification = "unverified"
    else:
        result.verification = "partial"
    return result


def _record(
    entry: Mapping[str, Any],
    text: Optional[str],
    digest_hex: Optional[str],
    result: VerifiedItem,
) -> dict[str, Any]:
    """Build the stored record of one file or hook and add to the counts."""
    status = entry["status"]
    redactions = dict(entry.get("redactions") or {})
    total = sum(redactions.values())
    record: dict[str, Any] = {
        "status": status,
        "redactions": redactions,
        "redactions_total": total,
        "verified": False,
    }
    if (status == "stored") != (text is not None):
        raise ContentRejectedError(REJECT_MALFORMED)
    if status != "stored":
        if total:
            raise ContentRejectedError(REJECT_MALFORMED)
        return record
    assert text is not None
    record["verified"] = _check_text(text, digest_hex, total)
    record["text"] = text
    result.stored_bytes += len(text.encode("utf-8"))
    result.stored_count += 1
    result.redaction_count += total
    result.verified_texts += int(record["verified"])
    return record
