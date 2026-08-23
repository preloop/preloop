"""HEAD-tree hygiene walk: junk names, leftover CI, certs, binary kinds."""

from __future__ import annotations

import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from preloop.security.defaults import (
    BINARY_EXTENSIONS,
    CERT_EXTENSIONS,
    DISABLED_CI_SUFFIXES,
    HIGH_ENTROPY_MIN_LEN,
    HIGH_ENTROPY_THRESHOLD,
    HOSTNAME_RE,
    JUNK_NAME_PATTERNS,
    JUNK_ROOT_SPACE_NAME,
    KEY_EXTENSIONS,
    SENSITIVE_FILENAME_NAMES,
    SENSITIVE_FILENAME_SUBSTRINGS,
    SENSITIVE_FILENAME_SUFFIXES,
    URL_RE,
)
from preloop.security.git_guard import run_git

_STRING_RE = re.compile(rb"[\x20-\x7e]{8,}")
_OPENSSL_KEY_BITS = re.compile(r"Public-Key:\s*\((\d+)\s*bit\)", re.IGNORECASE)


def _tracked_paths(repo: Path) -> List[str]:
    proc = run_git(repo, ["ls-files"])
    return [p for p in proc.stdout.splitlines() if p]


def _is_sensitive_filename(path: str) -> bool:
    name = Path(path).name.lower()
    if name in SENSITIVE_FILENAME_NAMES:
        return True
    for suffix in SENSITIVE_FILENAME_SUFFIXES:
        if name.endswith(suffix):
            return True
    return any(fragment in name for fragment in SENSITIVE_FILENAME_SUBSTRINGS)


def _is_junk_name(path: str) -> bool:
    name = Path(path).name
    if any(pat.search(name) for pat in JUNK_NAME_PATTERNS):
        return True
    if "/" not in path and JUNK_ROOT_SPACE_NAME.search(name) and "." not in name:
        return True
    return False


def _is_disabled_ci(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.endswith(suffix) for suffix in DISABLED_CI_SUFFIXES)


def _shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    length = len(data)
    entropy = 0.0
    for count in counts:
        if count:
            p = count / length
            entropy -= p * math.log2(p)
    return entropy


def _openssl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["openssl", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _parse_cert(path: Path) -> Dict[str, Any]:
    """Parse a certificate with openssl. Never dumps private key material."""
    info: Dict[str, Any] = {
        "path": str(path),
        "kind": "certificate",
        "available": False,
    }
    try:
        proc = _openssl(
            "x509",
            "-in",
            str(path),
            "-noout",
            "-startdate",
            "-enddate",
            "-issuer",
            "-subject",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        info["error"] = f"openssl unavailable: {exc}"
        return info
    if proc.returncode != 0:
        info["error"] = "not_an_x509_certificate"
        return info
    text = proc.stdout
    issuer = ""
    subject = ""
    not_after = ""
    for line in text.splitlines():
        if line.startswith("issuer="):
            issuer = line.split("=", 1)[1]
        elif line.startswith("subject="):
            subject = line.split("=", 1)[1]
        elif line.startswith("notAfter="):
            not_after = line.split("=", 1)[1]
    bits_proc = _openssl("x509", "-in", str(path), "-noout", "-text")
    bits_match = _OPENSSL_KEY_BITS.search(bits_proc.stdout)
    expired = False
    if not_after:
        try:
            expiry = datetime.strptime(not_after.strip(), "%b %d %H:%M:%S %Y %Z")
            expiry = expiry.replace(tzinfo=timezone.utc)
            expired = expiry < datetime.now(timezone.utc)
        except ValueError:
            expired = False
    info.update(
        {
            "available": True,
            "self_signed": bool(issuer) and issuer == subject,
            "expired": expired,
            "not_after": not_after or None,
            "key_bits": int(bits_match.group(1)) if bits_match else None,
        }
    )
    return info


def _parse_key_size_only(path: Path) -> Dict[str, Any]:
    """Report key size for a private key file without dumping the key."""
    info: Dict[str, Any] = {
        "path": str(path),
        "kind": "key_material",
        "available": False,
    }
    try:
        # -noout plus a public-only query. Do not use -text (dumps the key).
        proc = _openssl("pkey", "-in", str(path), "-noout")
    except (OSError, subprocess.TimeoutExpired) as exc:
        info["error"] = f"openssl unavailable: {exc}"
        return info
    if proc.returncode != 0:
        info["error"] = "not_a_parseable_key"
        return info
    # Check2: modulus-free size via pkeyutl / rsa -noout (no text).
    size_proc = _openssl("rsa", "-in", str(path), "-noout", "-check")
    # rsa -check still does not print the key body; capture only the first
    # "RSA key ok" / bit hint from stderr/stdout labels.
    bits_proc = _openssl("pkey", "-in", str(path), "-noout", "-text_pub")
    bits_match = _OPENSSL_KEY_BITS.search(bits_proc.stdout)
    info.update(
        {
            "available": True,
            "parseable": size_proc.returncode == 0 or bits_proc.returncode == 0,
            "key_bits": int(bits_match.group(1)) if bits_match else None,
        }
    )
    return info


def _classify_binary(path: Path) -> List[str]:
    """Return hit *kinds* found in a binary file. Never returns the bytes."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    # Cap the read window so a huge binary cannot explode memory.
    data = data[: 2 * 1024 * 1024]
    kinds: List[str] = []
    seen = set()
    for match in _STRING_RE.finditer(data):
        chunk = match.group(0)
        text = chunk.decode("ascii", errors="ignore")
        if URL_RE.search(text) and "url" not in seen:
            kinds.append("url")
            seen.add("url")
        if HOSTNAME_RE.search(text) and "hostname" not in seen:
            kinds.append("hostname")
            seen.add("hostname")
        if (
            len(chunk) >= HIGH_ENTROPY_MIN_LEN
            and _shannon_entropy(chunk) >= HIGH_ENTROPY_THRESHOLD
            and "high_entropy" not in seen
        ):
            kinds.append("high_entropy")
            seen.add("high_entropy")
    return kinds


def repo_hygiene_walk(repo_path: str) -> Dict[str, Any]:
    """Walk the HEAD tree for junk names, leftover CI, certs, and binary kinds.

    Args:
        repo_path: Path to a git work tree.

    Returns:
        JSON-serializable dict of classifiable rows. Default status is
        ``finding``. Values and private-key bodies are never included.
    """
    repo = Path(repo_path)
    rows: List[Dict[str, Any]] = []
    head = run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()

    for path in _tracked_paths(repo):
        abs_path = repo / path
        if _is_junk_name(path):
            rows.append(
                {
                    "sha": head,
                    "path": path,
                    "subject": "HEAD",
                    "term": "junk_name",
                    "kind": "junk_name",
                    "status": "finding",
                }
            )
        if _is_sensitive_filename(path):
            rows.append(
                {
                    "sha": head,
                    "path": path,
                    "subject": "HEAD",
                    "term": Path(path).name,
                    "kind": "sensitive_filename",
                    "status": "finding",
                }
            )
        if _is_disabled_ci(path):
            rows.append(
                {
                    "sha": head,
                    "path": path,
                    "subject": "HEAD",
                    "term": "disabled_ci",
                    "kind": "disabled_ci",
                    "status": "finding",
                }
            )
        suffix = Path(path).suffix.lower()
        if suffix in CERT_EXTENSIONS and abs_path.is_file():
            cert = _parse_cert(abs_path)
            rows.append(
                {
                    "sha": head,
                    "path": path,
                    "subject": "HEAD",
                    "term": "certificate",
                    "kind": "certificate",
                    "status": "finding",
                    "cert": {
                        k: cert[k]
                        for k in (
                            "self_signed",
                            "expired",
                            "not_after",
                            "key_bits",
                            "available",
                            "error",
                        )
                        if k in cert
                    },
                }
            )
        elif suffix in KEY_EXTENSIONS and abs_path.is_file():
            key = _parse_key_size_only(abs_path)
            rows.append(
                {
                    "sha": head,
                    "path": path,
                    "subject": "HEAD",
                    "term": "key_material",
                    "kind": "key_material",
                    "status": "finding",
                    "key": {
                        k: key[k]
                        for k in ("key_bits", "available", "parseable", "error")
                        if k in key
                    },
                }
            )
        if suffix in BINARY_EXTENSIONS and abs_path.is_file():
            for hit_kind in _classify_binary(abs_path):
                rows.append(
                    {
                        "sha": head,
                        "path": path,
                        "subject": "HEAD",
                        "term": hit_kind,
                        "kind": f"binary_{hit_kind}",
                        "status": "finding",
                    }
                )

    return {
        "tool": "repo_hygiene_walk",
        "rows": rows,
        "notes": [
            "default_status_is_finding",
            "values_and_private_keys_are_never_emitted",
            "binary_hits_report_kind_not_bytes",
        ],
    }
