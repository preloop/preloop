"""Built-in detectors for model I/O content policies.

Detectors are deterministic and have no network I/O unless a test registers
a fake moderation backend. Prompt-injection scoring reuses
``security_screen.score_text``. PII detection is a thin wrapper over the
shared span-returning library in ``preloop.services.sensitive_data``.
Moderation defaults to a local keyword ruleset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from preloop.services.security_screen import score_text
from preloop.services.sensitive_data.detectors import (
    _CARD_RE,
    _EMAIL_RE,
    _PHONE_US_RE,
    DetectorConfig,
    detect,
    types_found,
)

#: Types the legacy ``detect_pii`` default scans; new built-in types are
#: opt-in so existing rules keep their behaviour.
LEGACY_PII_TYPES = ("email", "phone", "credit_card")

#: Compatibility aliases. ``session_search_index`` masks emails with
#: ``PII_EMAIL_RE``; the patterns now live in the shared library.
PII_EMAIL_RE = _EMAIL_RE
PII_PHONE_RE = _PHONE_US_RE
PII_CARD_RE = _CARD_RE

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
    count: int = 0


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


def detect_pii(
    text: str,
    types: Optional[Sequence[str]] = None,
    config: Optional[DetectorConfig] = None,
) -> PIIResult:
    """Scan ``text`` for configured PII entity types.

    Thin wrapper over :func:`preloop.services.sensitive_data.detect` kept
    for the model I/O evaluator and its tests.

    Args:
        text: Canonical request or response text.
        types: Type names to scan. When omitted, the account default
            (``sensitive_data.detectors.types`` carried by ``config``) is
            used, and without one the legacy email, phone, credit_card set.
        config: Account detector configuration (custom patterns, keyword
            lists, locales). ``types`` narrows it when both are given.

    Returns:
        ``PIIResult`` with ``found``, the matched type names and the match
        count.
    """
    base = config or DetectorConfig()
    if types:
        selected = list(types)
    elif base.types:
        selected = list(base.types)
    else:
        selected = list(LEGACY_PII_TYPES)
    matches = detect(text, base.with_types(selected))
    names = types_found(matches)
    return PIIResult(found=bool(names), types_found=names, count=len(matches))


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
