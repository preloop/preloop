"""Deterministic rule-based scorer for the security-screen proxy endpoint.

Implements the provider side of QM's external security-screen proxy contract
(https://github.com/yc-software/qm/blob/main/docs/deploy-directory.md):
screened text chunks are scored in [0, 1] against compiled regex rule sets,
and the caller decides Strict/allow by comparing the score to the returned
threshold. Scoring is pure and deterministic: no I/O, no model calls.

Rule categories mirror the dangerous-content classes of QM's predeclared
command policy plus prompt-injection markers:

- ``prompt_injection``: instruction-override phrasing, system-prompt
  extraction, jailbreak personas, chat-template markers.
- ``destructive_command``: recursive force deletes, filesystem/device
  overwrites, fork bombs, force pushes, destructive shutdown flags.
- ``destructive_sql``: DROP/TRUNCATE, DELETE or UPDATE without WHERE.
- ``secret_exfiltration``: credential-file reads, environment dumps piped to
  network tools, cloud key material, PEM private key blocks.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional, Pattern, Tuple

DEFAULT_SCREEN_THRESHOLD = 0.7

_THRESHOLD_ENV_VAR = "PRELOOP_SECURITY_SCREEN_THRESHOLD"

# Maximum accepted text size. QM chunks screened inputs into overlapping
# 1,600-character requests (16,000-character total cap), so this is a
# defensive bound for non-QM callers, not a contract limit.
MAX_TEXT_LENGTH = 64_000


@dataclass(frozen=True)
class ScreenRule:
    """One deterministic screening rule.

    Attributes:
        category: Outcome label reported when this rule fires (lowercase).
        score: Score in [0, 1] assigned when the pattern matches.
        pattern: Compiled regex evaluated against the screened text.
        name: Short identifier for logs and diagnostics.
    """

    category: str
    score: float
    pattern: Pattern[str]
    name: str


@dataclass(frozen=True)
class ScreenVerdict:
    """Result of scoring one screened text chunk.

    Attributes:
        score: Highest score among matching rules; 0.0 when nothing matched.
        primary_outcome: Category of the highest-scoring match, or ``None``
            when the text is benign.
        matched_rules: Names of every rule that matched, for diagnostics.
    """

    score: float
    primary_outcome: Optional[str]
    matched_rules: Tuple[str, ...]


def _rule(category: str, score: float, name: str, pattern: str) -> ScreenRule:
    """Compile one screening rule with case-insensitive matching."""
    return ScreenRule(
        category=category,
        score=score,
        pattern=re.compile(pattern, re.IGNORECASE),
        name=name,
    )


_RULES: Tuple[ScreenRule, ...] = (
    # --- prompt_injection -------------------------------------------------
    _rule(
        "prompt_injection",
        0.90,
        "instruction_override",
        r"(?:ignore|forget|override|disregard)\s+(?:all\s+|any\s+)?"
        r"(?:your\s+|the\s+)?(?:previous|prior|above|earlier|original|system)"
        r"\s*(?:instructions?|prompts?|rules|guidelines|directions)",
    ),
    _rule(
        "prompt_injection",
        0.90,
        "system_prompt_extraction",
        r"(?:reveal|print|show|output|repeat|display)\s+(?:me\s+)?"
        r"(?:your|the)\s+(?:system|hidden|initial|secret)\s+prompt",
    ),
    _rule(
        "prompt_injection",
        0.90,
        "jailbreak_persona",
        r"you\s+are\s+now\s+(?:dan\b|jailbroken|unrestricted|"
        r"in\s+developer\s+mode)",
    ),
    _rule(
        "prompt_injection",
        0.90,
        "chat_template_marker",
        r"<\|im_start\|>\s*system",
    ),
    _rule(
        "prompt_injection",
        0.90,
        "user_concealment",
        r"do\s+not\s+(?:tell|inform|alert|notify|warn)\s+the\s+user",
    ),
    # --- destructive_command ----------------------------------------------
    _rule(
        "destructive_command",
        0.90,
        "recursive_force_delete",
        # Option tokens start with 1-2 dashes followed by a non-dash,
        # non-space character so the repetition is unambiguous (no
        # exponential backtracking on runs of dashes).
        r"\brm\s+(?:-{1,2}[^\s-]\S*\s+)*-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)[a-z]*\b"
        r"|\brm\b[^\n]*--recursive\b[^\n]*--force\b"
        r"|\brm\b[^\n]*--force\b[^\n]*--recursive\b",
    ),
    _rule(
        "destructive_command",
        0.90,
        "filesystem_format",
        r"\bmkfs(?:\.\w+)?\b",
    ),
    _rule(
        "destructive_command",
        0.90,
        "raw_device_overwrite",
        r"\bdd\s+[^\n]*\bof=/dev/|>\s*/dev/sd[a-z]\b",
    ),
    _rule(
        "destructive_command",
        0.90,
        "fork_bomb",
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
    ),
    _rule(
        "destructive_command",
        0.90,
        "world_writable_root",
        r"\bchmod\s+(?:-[a-z]+\s+)*777\s+/(?:\s|$)",
    ),
    _rule(
        "destructive_command",
        0.90,
        "force_push",
        r"\bgit\s+push\s+[^\n]*(?:--force\b|--force-with-lease\b|\s-f\b)",
    ),
    _rule(
        "destructive_command",
        0.90,
        "host_shutdown",
        r"\bshutdown\s+-[hr]\b",
    ),
    # --- destructive_sql ---------------------------------------------------
    _rule(
        "destructive_sql",
        0.85,
        "drop_statement",
        r"\bdrop\s+(?:table|database|schema)\b",
    ),
    _rule(
        "destructive_sql",
        0.85,
        "truncate_statement",
        r"\btruncate\s+(?:table\s+)?[\w.`\"]+",
    ),
    _rule(
        "destructive_sql",
        0.85,
        "delete_without_where",
        r"\bdelete\s+from\s+[\w.`\"]+\s*(?![^;]*\bwhere\b)(?:;|$)",
    ),
    _rule(
        "destructive_sql",
        0.85,
        "update_without_where",
        r"\bupdate\s+[\w.`\"]+\s+set\b(?![^;]*\bwhere\b)",
    ),
    # --- secret_exfiltration -----------------------------------------------
    _rule(
        "secret_exfiltration",
        0.90,
        "ssh_key_read",
        r"(?:~|/home/\w+|/root)/\.ssh/id_[a-z0-9_]+",
    ),
    _rule(
        "secret_exfiltration",
        0.90,
        "env_file_read",
        r"\b(?:cat|less|head|tail|strings)\s+[^\n]*\.env\b",
    ),
    _rule(
        "secret_exfiltration",
        0.90,
        "system_credential_file",
        r"/etc/shadow\b",
    ),
    _rule(
        "secret_exfiltration",
        0.90,
        "env_dump_to_network",
        r"\b(?:printenv|env)\b[^\n|]*\|\s*(?:curl|wget|nc|ncat)\b",
    ),
    _rule(
        "secret_exfiltration",
        0.90,
        "secret_var_to_network",
        r"\b(?:curl|wget|nc|ncat)\b[^\n]*\$\{?\w*"
        r"(?:SECRET|TOKEN|API_?KEY|PASSWORD|CREDENTIAL)\w*",
    ),
    _rule(
        "secret_exfiltration",
        0.90,
        "aws_access_key_id",
        r"\bAKIA[0-9A-Z]{16}\b",
    ),
    _rule(
        "secret_exfiltration",
        0.90,
        "pem_private_key",
        r"-----BEGIN\s+(?:[A-Z]+\s+)?PRIVATE\s+KEY(?:\s+BLOCK)?-----",
    ),
)


def score_text(text: str) -> ScreenVerdict:
    """Score one screened text chunk against the built-in rule sets.

    Args:
        text: Untrusted content to score. Matching is case-insensitive.

    Returns:
        A verdict whose score is the maximum over all matching rules (0.0
        when nothing matched) and whose primary outcome is the category of
        the highest-scoring match.
    """
    best_score = 0.0
    best_category: Optional[str] = None
    matched: list[str] = []
    for rule in _RULES:
        if rule.pattern.search(text):
            matched.append(rule.name)
            if rule.score > best_score:
                best_score = rule.score
                best_category = rule.category
    return ScreenVerdict(
        score=best_score,
        primary_outcome=best_category,
        matched_rules=tuple(matched),
    )


def get_screen_threshold() -> float:
    """Resolve the screening threshold from the environment.

    Reads ``PRELOOP_SECURITY_SCREEN_THRESHOLD`` and clamps it to [0, 1].
    Missing or unparsable values fall back to ``DEFAULT_SCREEN_THRESHOLD``.

    Returns:
        The threshold QM compares chunk scores against.
    """
    raw = os.getenv(_THRESHOLD_ENV_VAR)
    if raw is None:
        return DEFAULT_SCREEN_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SCREEN_THRESHOLD
    return min(1.0, max(0.0, value))
