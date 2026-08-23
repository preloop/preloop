"""Generic default term lists and filename patterns for repo-audit tools.

Product-specific tokens (firmware header names, broker passwords, vendor
nouns) must never appear here. Callers may pass extra terms at scan time.
"""

from __future__ import annotations

import re
from typing import FrozenSet, Pattern, Tuple

# Content pickaxe / grep terms. These are identifiers and English words, not
# assignment excerpts and not product-specific define names.
DEFAULT_SECRET_TERMS: Tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "api-key",
    "apikey",
    "private_key",
    "private-key",
    "client_secret",
    "access_key",
    "access-token",
    "auth_token",
    "bearer",
    "credential",
    "credentials",
)

DEFAULT_GREP_TERMS: Tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "credential",
    "private key",
    "api key",
    "should not be public",
)

# Deleted-path and HEAD filename patterns (generic sensitive / leftover).
SENSITIVE_FILENAME_SUFFIXES: Tuple[str, ...] = (
    ".env",
    ".pem",
    ".p12",
    ".pfx",
    ".key",
    ".swp",
    ".swo",
    ".keystore",
    ".jks",
)

SENSITIVE_FILENAME_NAMES: FrozenSet[str] = frozenset(
    {
        ".env",
        "id_rsa",
        "id_dsa",
        "id_ed25519",
        "id_ecdsa",
        "credentials.json",
        "secrets.json",
        "serviceaccount.json",
        "ca.key",
        "tls.key",
        "server.key",
    }
)

SENSITIVE_FILENAME_SUBSTRINGS: Tuple[str, ...] = (
    ".env.",
    "_private.",
    "id_rsa",
    "id_ed25519",
)

DISABLED_CI_SUFFIXES: Tuple[str, ...] = (
    ".yml.off",
    ".yaml.off",
    ".yml.old",
    ".yaml.old",
    ".yml.bak",
    ".yaml.bak",
)

CERT_EXTENSIONS: FrozenSet[str] = frozenset({".pem", ".crt", ".cer", ".der"})
KEY_EXTENSIONS: FrozenSet[str] = frozenset({".key", ".p12", ".pfx"})

JUNK_NAME_PATTERNS: Tuple[Pattern[str], ...] = (
    re.compile(r"should not be public", re.IGNORECASE),
    re.compile(r"^\s"),  # leading whitespace in a filename
    re.compile(r"\s{2,}"),  # pager-dump / wrapped commit-message fragments
)

# Root-level names that look like accidental pager or commit-message slices
# (spaces, no extension, unusually long).
JUNK_ROOT_SPACE_NAME = re.compile(r"\s")

BINARY_EXTENSIONS: FrozenSet[str] = frozenset(
    {
        ".bin",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".o",
        ".a",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".xz",
        ".7z",
        ".woff",
        ".woff2",
        ".ttf",
    }
)

HOSTNAME_RE = re.compile(
    r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"(?:[a-zA-Z]{2,24})\b"
)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

HIGH_ENTROPY_MIN_LEN = 20
HIGH_ENTROPY_THRESHOLD = 4.5
