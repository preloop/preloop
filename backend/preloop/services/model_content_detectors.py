"""Built-in detectors for model I/O content policies.

Detectors are deterministic and have no network I/O unless a test registers
a fake moderation backend. Prompt-injection scoring reuses
``security_screen.score_text``. PII is regex plus a Luhn check for
credit-card-like numbers. Moderation defaults to a local keyword ruleset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from preloop.services.security_screen import score_text

PII_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PII_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}(?!\w)"
)
PII_CARD_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")

_INJECTION_CATEGORY = "prompt_injection"

_MODERATION_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "hate": ("kill all", "racial slur"),
    "violence": ("how to make a bomb", "build a weapon"),
    "self_harm": ("suicide method", "how to kill myself"),
    "sexual": ("child sexual",),
}


@dataclass(frozen=True)
class PIIResult:
    """PII detector output mapped to policy attributes."""

    found: bool
    types_found: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class InjectionResult:
    """Prompt-injection detector output.

    Best-effort heuristic, not a guarantee. ``score`` is in [0, 1].
    """

    score: float
    matched_patterns: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModerationResult:
    """Moderation detector output."""

    flagged: bool
    categories: List[str] = field(default_factory=list)


ModerationBackend = Callable[[str], ModerationResult]

_MODERATION_BACKENDS: Dict[str, ModerationBackend] = {}


def register_moderation_backend(name: str, backend: ModerationBackend) -> None:
    """Register or replace a moderation backend (tests use ``fake``)."""
    _MODERATION_BACKENDS[name] = backend


def reset_moderation_backends() -> None:
    """Drop test-registered backends. The local backend stays."""
    _MODERATION_BACKENDS.clear()
    _MODERATION_BACKENDS["local"] = local_moderation_check


def _luhn_ok(digits: str) -> bool:
    """Return True when ``digits`` pass the Luhn checksum."""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    reverse = digits[::-1]
    for index, char in enumerate(reverse):
        number = int(char)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def detect_pii(text: str, types: Optional[Sequence[str]] = None) -> PIIResult:
    """Scan ``text`` for configured PII entity types.

    Args:
        text: Canonical request or response text.
        types: Subset of email, phone, credit_card. Default is all three.

    Returns:
        ``PIIResult`` with ``found`` and the matched type names.
    """
    selected = list(types) if types else ["email", "phone", "credit_card"]
    found: List[str] = []
    if "email" in selected and PII_EMAIL_RE.search(text):
        found.append("email")
    if "phone" in selected and PII_PHONE_RE.search(text):
        found.append("phone")
    if "credit_card" in selected:
        for match in PII_CARD_RE.finditer(text):
            digits = re.sub(r"\D", "", match.group(0))
            if _luhn_ok(digits):
                found.append("credit_card")
                break
    return PIIResult(found=bool(found), types_found=found)


_INJECTION_RULE_NAMES = frozenset(
    {
        "instruction_override",
        "system_prompt_extraction",
        "jailbreak_persona",
        "chat_template_marker",
        "user_concealment",
    }
)


def detect_injection(text: str) -> InjectionResult:
    """Score prompt-injection heuristics via ``security_screen``.

    Only ``prompt_injection`` matches contribute to ``injection.score``.
    This is best-effort, not a guarantee.
    """
    verdict = score_text(text)
    matched = [name for name in verdict.matched_rules if name in _INJECTION_RULE_NAMES]
    if not matched:
        return InjectionResult(score=0.0, matched_patterns=[])
    score = verdict.score if verdict.primary_outcome == _INJECTION_CATEGORY else 0.90
    return InjectionResult(score=score, matched_patterns=matched)


def local_moderation_check(text: str) -> ModerationResult:
    """Keyword ruleset used when no live moderation provider is configured."""
    lowered = text.lower()
    categories: List[str] = []
    for category, phrases in _MODERATION_KEYWORDS.items():
        if any(phrase in lowered for phrase in phrases):
            categories.append(category)
    return ModerationResult(flagged=bool(categories), categories=categories)


def detect_moderation(text: str, backend: str = "local") -> ModerationResult:
    """Run the named moderation backend.

    Args:
        text: Canonical request or response text.
        backend: Registered backend name. Defaults to ``local``.

    Returns:
        ``ModerationResult``.

    Raises:
        ValueError: If the backend is not registered.
    """
    checker = _MODERATION_BACKENDS.get(backend)
    if checker is None:
        raise ValueError(f"Unknown moderation backend: {backend}")
    return checker(text)


reset_moderation_backends()
