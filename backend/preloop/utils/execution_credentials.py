"""Exact execution credential masking using non-recoverable fingerprints."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, quote_plus, unquote, urlsplit

REDACTED = "[REDACTED]"
_SECRET_NAME = re.compile(
    r"token|password|secret|credential|api[_-]?key|private[_-]?key|authorization|capability|marker",
    re.I,
)


def credential_values(value: Any, *, sensitive: bool = False) -> set[str]:
    """Collect credentials from trusted launch data, including config files.

    Args:
        value: Server-built environment or execution context.
        sensitive: Whether the parent field identifies credential material.

    Returns:
        Nonempty credential values, never names or identifiers alone.
    """
    result: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            secret = bool(_SECRET_NAME.search(name)) and not name.lower().endswith(
                ("_id", "_url", "_path", "_file", "_name", "_enabled", "_seconds")
            )
            result.update(credential_values(item, sensitive=secret))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.update(credential_values(item, sensitive=sensitive))
    elif isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            result.update(credential_values(parsed))
        elif sensitive:
            result.add(value)
            if value.lower().startswith(("bearer ", "basic ")):
                result.add(value.split(" ", 1)[1])
        for line in value.splitlines():
            try:
                password = urlsplit(line).password
            except ValueError:
                password = None
            if password:
                result.add(unquote(password))
    return result


def _variants(value: str) -> set[str]:
    """Return raw, JSON, URL and standard/URL-safe base64 representations."""
    variants = {value, json.dumps(value, ensure_ascii=True)[1:-1]}
    for encoded in (quote(value), quote(value, safe=""), quote_plus(value)):
        variants.add(encoded)
        variants.add(re.sub(r"%[0-9A-F]{2}", lambda m: m[0].lower(), encoded))
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        encoded = encode(value.encode()).decode()
        variants.update((encoded, encoded.rstrip("=")))
    return variants


class CredentialMasker:
    """Match exact values without persisting recoverable credential material.

    A short public prefix finds candidates; a SHA-256 digest confirms each
    complete value. Longer overlapping matches win. Fingerprints remain useful
    for late output after credentials have expired or been revoked.
    """

    def __init__(self, fingerprints: Iterable[dict[str, Any]] = ()) -> None:
        self.fingerprints = list(fingerprints)
        self._candidates: dict[bytes, list[tuple[int, str]]] = {}
        for item in self.fingerprints:
            prefix = bytes.fromhex(item["prefix"])
            self._candidates.setdefault(prefix, []).append(
                (item["length"], item["sha256"])
            )
        for candidates in self._candidates.values():
            candidates.sort(reverse=True)
        self._pattern = (
            re.compile(
                b"(?=("
                + b"|".join(
                    re.escape(p)
                    for p in sorted(self._candidates, key=len, reverse=True)
                )
                + b"))"
            )
            if self._candidates
            else None
        )

    @classmethod
    def from_values(cls, values: Iterable[str]) -> CredentialMasker:
        """Fingerprint trusted issued values and their common encodings."""
        fingerprints: dict[str, dict[str, Any]] = {}
        for value in values:
            if not value:
                continue
            for variant in _variants(value):
                raw = variant.encode()
                digest = hashlib.sha256(raw).hexdigest()
                fingerprints[digest] = {
                    "prefix": raw[: min(4, max(1, len(raw) // 4))].hex(),
                    "length": len(raw),
                    "sha256": digest,
                }
        return cls(fingerprints.values())

    def mask_bytes(self, value: bytes) -> bytes:
        """Replace confirmed matches, including adjacent and embedded values."""
        if self._pattern is None:
            return value
        chunks: list[bytes] = []
        cursor = 0
        for match in self._pattern.finditer(value):
            start = match.start()
            if start < cursor:
                continue
            for size, digest in self._matching_candidates(value, start):
                if hashlib.sha256(value[start : start + size]).hexdigest() == digest:
                    chunks.extend((value[cursor:start], REDACTED.encode()))
                    cursor = start + size
                    break
        if not chunks:
            return value
        chunks.append(value[cursor:])
        return b"".join(chunks)

    def _matching_candidates(self, value: bytes, start: int) -> list[tuple[int, str]]:
        """Include every prefix at a position, longest complete value first."""
        return sorted(
            (
                candidate
                for prefix, candidates in self._candidates.items()
                if value.startswith(prefix, start)
                for candidate in candidates
            ),
            reverse=True,
        )

    def mask(self, value: Any) -> Any:
        """Recursively mask text, bytes and JSON keys without mutating input."""
        if isinstance(value, str):
            return self.mask_bytes(value.encode()).decode()
        if isinstance(value, bytes):
            return self.mask_bytes(value)
        if isinstance(value, dict):
            return {self.mask(key): self.mask(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.mask(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.mask(item) for item in value)
        return value

    def mask_archive(self, value: bytes) -> bytes:
        """Mask validated gzip tar members while maintaining content manifests."""
        if not self.fingerprints:
            return value
        output = io.BytesIO()
        changed = False
        manifest = None
        manifest_member = None
        digests: dict[str, tuple[str, str, int]] = {}
        with tarfile.open(fileobj=io.BytesIO(value), mode="r:gz") as source:
            with tarfile.open(fileobj=output, mode="w:gz") as target:
                for member in source:
                    old_name, old_link = member.name, member.linkname
                    member.name = self.mask(member.name)
                    member.linkname = self.mask(member.linkname)
                    headers = self.mask(member.pax_headers)
                    changed |= headers != member.pax_headers
                    member.pax_headers = headers
                    changed |= (old_name, old_link) != (member.name, member.linkname)
                    data = source.extractfile(member) if member.isfile() else None
                    if data is None:
                        target.addfile(member)
                        continue
                    raw = data.read()
                    safe = self.mask_bytes(raw)
                    if raw.startswith(b"SQLite format 3\x00") and safe != raw:
                        safe = self._mask_sqlite(raw)
                    changed |= safe != raw
                    if old_name == "manifest.json":
                        try:
                            manifest = json.loads(safe)
                        except (ValueError, UnicodeError):
                            manifest = None
                        if isinstance(manifest, dict):
                            manifest_member = member
                            continue
                    digests[member.name] = (
                        hashlib.sha256(raw).hexdigest(),
                        hashlib.sha256(safe).hexdigest(),
                        len(safe),
                    )
                    member.size = len(safe)
                    target.addfile(member, io.BytesIO(safe))
                if manifest_member is not None and isinstance(manifest, dict):
                    for field, path_field, size_field, prefix in (
                        ("files", "path", "size", "files/"),
                        ("members", "name", "size_bytes", ""),
                    ):
                        entries = manifest.get(field)
                        if not isinstance(entries, list):
                            continue
                        for entry in entries:
                            if not isinstance(entry, dict):
                                continue
                            name = prefix + str(entry.get(path_field, ""))
                            pair = digests.get(name)
                            if pair is None:
                                continue
                            before_hash, after_hash, size = pair
                            if before_hash != after_hash:
                                if entry.get("sha256") != before_hash:
                                    raise ValueError("artifact_manifest_mismatch")
                                entry["sha256"] = after_hash
                                entry[size_field] = size
                        if field == "members" and changed:
                            manifest["members_digest"] = hashlib.sha256(
                                json.dumps(
                                    entries,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    ensure_ascii=False,
                                ).encode()
                            ).hexdigest()
                    body = json.dumps(manifest, sort_keys=True).encode()
                    manifest_member.size = len(body)
                    target.addfile(manifest_member, io.BytesIO(body))
        return output.getvalue() if changed else value

    def _mask_sqlite(self, value: bytes) -> bytes:
        """Rewrite native session text using SQLite so page structure stays valid."""
        import sqlite3

        def quoted(name: str) -> str:
            return '"' + name.replace('"', '""') + '"'

        with sqlite3.connect(":memory:") as db:
            db.deserialize(value)
            db.execute("PRAGMA secure_delete=ON")
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for (table,) in tables:
                columns = [
                    row[1]
                    for row in db.execute("PRAGMA table_info(" + quoted(table) + ")")
                ]
                for row in db.execute(
                    "SELECT rowid, * FROM " + quoted(table)
                ).fetchall():
                    for column, before in zip(columns, row[1:], strict=True):
                        after = self.mask(before)
                        if after != before:
                            db.execute(
                                "UPDATE "
                                + quoted(table)
                                + " SET "
                                + quoted(column)
                                + "=? WHERE rowid=?",
                                (after, row[0]),
                            )
            db.commit()
            db.execute("VACUUM")
            result = db.serialize()
        if self.mask_bytes(result) != result:
            raise ValueError("artifact_credential_in_binary_metadata")
        return result


class CredentialStream:
    """Hold only unresolved credential candidates between arbitrary chunks."""

    def __init__(self, masker: CredentialMasker) -> None:
        self.masker = masker
        self.pending = b""

    def feed(self, chunk: bytes) -> bytes:
        """Return safe output while retaining any possibly incomplete match."""
        self.pending += chunk
        cut = len(self.pending)
        matches: list[tuple[int, int]] = []
        pattern = self.masker._pattern
        if pattern is not None:
            for match in pattern.finditer(self.pending):
                start = match.start()
                uncertain = False
                for length, digest in self.masker._matching_candidates(
                    self.pending, start
                ):
                    if start + length > len(self.pending):
                        uncertain = True
                    elif (
                        hashlib.sha256(self.pending[start : start + length]).hexdigest()
                        == digest
                    ):
                        matches.append((start, start + length))
                        break
                if uncertain:
                    cut = min(cut, start)
            for prefix in self.masker._candidates:
                for size in range(1, len(prefix)):
                    if self.pending.endswith(prefix[:size]):
                        cut = min(cut, len(self.pending) - size)
            for start, end in reversed(matches):
                if start < cut < end:
                    cut = start
        output = self.masker.mask_bytes(self.pending[:cut])
        self.pending = self.pending[cut:]
        return output

    def finish(self) -> bytes:
        """Flush the last complete or nonmatching candidate at EOF."""
        output = self.masker.mask_bytes(self.pending)
        self.pending = b""
        return output

    def cut(self) -> bytes:
        """Flush when the source is abandoned before EOF.

        Unlike ``finish``, a trailing candidate that is still shorter than the
        value it may belong to is replaced whole: the rest of that value will
        never be read, so its beginning must not be released as plain text.

        Returns:
            The remaining output, with complete values and an unfinished
            trailing candidate redacted.
        """
        pending, self.pending = self.pending, b""
        pattern = self.masker._pattern
        if pattern is None:
            return pending
        cursor = 0
        for match in pattern.finditer(pending):
            start = match.start()
            if start < cursor:
                continue
            for length, digest in self.masker._matching_candidates(pending, start):
                if start + length > len(pending):
                    return self.masker.mask_bytes(pending[:start]) + REDACTED.encode()
                if hashlib.sha256(pending[start : start + length]).hexdigest() == (
                    digest
                ):
                    cursor = start + length
                    break
        return self.masker.mask_bytes(pending)
