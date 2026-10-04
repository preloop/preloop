"""Sensitive-data detectors that return typed spans.

Every detector is a pure function ``(text, config) -> matches``. Built-in
types cover personal, payment and health identifiers with checksums where a
public one exists (Luhn, IBAN mod-97, DE tax id, FR NIR, NL BSN). Account
configuration adds custom regexes and keyword lists. Overlapping matches are
resolved longest-first so a card number is never also reported as a phone.

Nothing here does network I/O or loads a model. A third-party recogniser is
registered with :func:`register_detector` and selected by name like a
built-in type; that registry is the in-process side of the external
redaction-service contract (#1100).
"""

from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

import regex as timeout_regex

#: Longest regex an account may register. Long patterns are hard to review
#: and are the usual vehicle for pathological backtracking.
MAX_CUSTOM_PATTERN_LENGTH = 512
#: Longest keyword term and most terms per list.
MAX_KEYWORD_TERM_LENGTH = 128
MAX_KEYWORD_TERMS = 500
#: Names of custom patterns, keyword lists and registered detectors.
TYPE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_REGEX_FLAG_MAP = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
}


class UnsafePatternError(ValueError):
    """A custom regex was rejected before compilation."""


class DetectorTimeoutError(TimeoutError):
    """An account pattern exceeded its match budget on one text."""


#: Match budget for one account pattern over one text. Enforced by the
#: ``regex`` engine itself, so a pathological pattern is interrupted even
#: though CPython holds the GIL while matching; a thread-pool timeout cannot
#: do that.
CUSTOM_PATTERN_TIMEOUT_SECONDS = 0.25
#: Budget for all account patterns in one ``detect`` call, so many slow
#: patterns cannot add up to minutes.
CUSTOM_PATTERNS_TOTAL_BUDGET_SECONDS = 1.0
#: Most custom patterns and keyword lists an account may configure.
MAX_CUSTOM_PATTERNS = 50
MAX_KEYWORD_LISTS = 50


@dataclass(frozen=True)
class Match:
    """One detected span. ``end`` is exclusive, like a slice."""

    type: str
    start: int
    end: int
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("Match span must be non-empty and non-negative")

    @property
    def length(self) -> int:
        """Span length in characters."""
        return self.end - self.start


@dataclass(frozen=True)
class SensitiveTypeInfo:
    """Catalog entry for one detector type (feeds the console page)."""

    id: str
    label: str
    description: str
    example: str
    locales: tuple[str, ...] = ()
    checksum: bool = False
    builtin: bool = True

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe form for the types endpoint."""
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "example": self.example,
            "locales": list(self.locales),
            "checksum": self.checksum,
            "builtin": self.builtin,
        }


@dataclass(frozen=True)
class CustomPattern:
    """Account-defined regex type."""

    name: str
    regex: str
    flags: tuple[str, ...] = ()

    def compiled(self) -> Any:
        """Compile through the safety gate with the timeout-capable engine."""
        return compile_safe_regex(self.regex, self.flags)


@dataclass(frozen=True)
class KeywordList:
    """Account-defined keyword type matched on whole words."""

    name: str
    terms: tuple[str, ...]
    case_sensitive: bool = False

    def compiled(self) -> re.Pattern[str]:
        """Whole-word alternation, longest term first."""
        return compile_keyword_pattern(self.terms, self.case_sensitive)


@dataclass(frozen=True)
class DetectorConfig:
    """What to scan for.

    ``types`` lists built-in ids, custom pattern names, keyword list names
    and registered detector names. ``None`` selects every built-in type
    plus every custom entry. ``locales`` narrows ``national_id``; empty
    means all supported locales.
    """

    types: Optional[tuple[str, ...]] = None
    locales: tuple[str, ...] = ()
    custom_patterns: tuple[CustomPattern, ...] = ()
    keywords: tuple[KeywordList, ...] = ()
    medical_record_number_pattern: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls, raw: Optional[Mapping[str, Any]], *, types: Optional[Sequence[str]] = None
    ) -> "DetectorConfig":
        """Build from the policy ``sensitive_data.detectors`` block.

        ``types`` overrides the block's own ``types`` (a rule narrows the
        account-wide detector set).
        """
        raw = raw or {}
        selected = types if types is not None else raw.get("types")
        patterns = tuple(
            CustomPattern(
                name=str(item["name"]),
                regex=str(item["regex"]),
                flags=tuple(item.get("flags") or ()),
            )
            for item in (raw.get("custom_patterns") or [])
            if isinstance(item, Mapping) and item.get("name") and item.get("regex")
        )
        keyword_lists = tuple(
            KeywordList(
                name=str(item["name"]),
                terms=tuple(str(term) for term in (item.get("terms") or []) if term),
                case_sensitive=bool(item.get("case_sensitive", False)),
            )
            for item in (raw.get("keywords") or [])
            if isinstance(item, Mapping) and item.get("name") and item.get("terms")
        )
        return cls(
            types=tuple(selected) if selected is not None else None,
            locales=tuple(str(item).lower() for item in (raw.get("locales") or [])),
            custom_patterns=patterns,
            keywords=keyword_lists,
            medical_record_number_pattern=raw.get("medical_record_number_pattern"),
        )

    def with_types(self, types: Optional[Sequence[str]]) -> "DetectorConfig":
        """Copy with a different type selection."""
        return DetectorConfig(
            types=tuple(types) if types is not None else None,
            locales=self.locales,
            custom_patterns=self.custom_patterns,
            keywords=self.keywords,
            medical_record_number_pattern=self.medical_record_number_pattern,
            extra=self.extra,
        )

    def custom_names(self) -> List[str]:
        """Names of custom patterns and keyword lists, in order."""
        return [item.name for item in self.custom_patterns] + [
            item.name for item in self.keywords
        ]


DetectorFn = Callable[[str, DetectorConfig], Iterable[Match]]


# ---------------------------------------------------------------------------
# Safe regex compilation
# ---------------------------------------------------------------------------


def _has_nested_quantifier(pattern: str) -> bool:
    """True when a quantified group itself contains a quantifier.

    Shapes such as ``(a+)+``, ``(\\w*\\s?)*`` and ``(x{2,}){3,}`` backtrack
    exponentially on non-matching input. The scan ignores escaped
    characters and character classes, tracks group nesting and remembers
    whether a quantifier appeared inside each open group.
    """
    quantified_inside: List[bool] = []
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            # Skip the class. A leading ``]`` or ``^]`` is literal.
            i += 1
            if i < length and pattern[i] == "^":
                i += 1
            if i < length and pattern[i] == "]":
                i += 1
            while i < length and pattern[i] != "]":
                if pattern[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if char == "(":
            quantified_inside.append(False)
            i = _skip_group_modifier(pattern, i + 1)
            continue
        if char == ")":
            inner = quantified_inside.pop() if quantified_inside else False
            i += 1
            quantifier, i = _read_quantifier(pattern, i)
            if quantifier:
                if inner:
                    return True
                if quantified_inside:
                    quantified_inside[-1] = True
            elif inner and quantified_inside:
                quantified_inside[-1] = True
            continue
        quantifier, next_i = _read_quantifier(pattern, i)
        if quantifier:
            if quantified_inside:
                quantified_inside[-1] = True
            i = next_i
            continue
        i += 1
    return False


_BRACE_QUANTIFIER_RE = re.compile(r"\{\d*(?:,\d*)?\}")


def _skip_group_modifier(pattern: str, index: int) -> int:
    """Return the index after a ``(?...`` group modifier starting at ``index``.

    ``(?:``, ``(?=``, ``(?!``, ``(?<=``, ``(?<!``, ``(?P<name>``, ``(?P=name)``,
    ``(?#comment)`` and inline flags ``(?i)`` / ``(?i:`` are syntax, not a
    ``?`` quantifier applied to something.
    """
    length = len(pattern)
    if index >= length or pattern[index] != "?":
        return index
    index += 1
    if index >= length:
        return index
    char = pattern[index]
    if char in ":=!":
        return index + 1
    if char == "<":
        if index + 1 < length and pattern[index + 1] in "=!":
            return index + 2
        end = pattern.find(">", index)
        return length if end == -1 else end + 1
    if char == "P":
        if index + 1 < length and pattern[index + 1] == "<":
            end = pattern.find(">", index)
            return length if end == -1 else end + 1
        end = pattern.find(")", index)
        return length if end == -1 else end + 1
    if char == "#":
        end = pattern.find(")", index)
        return length if end == -1 else end + 1
    # Inline flags: letters and '-' up to ')' or ':'.
    while index < length and (pattern[index].isalpha() or pattern[index] == "-"):
        index += 1
    if index < length and pattern[index] in ":)":
        index += 1
    return index


def _read_quantifier(pattern: str, index: int) -> tuple[bool, int]:
    """Return (is_quantifier, index after it) for a quantifier at ``index``.

    ``?`` directly after ``(`` is a group modifier, not a quantifier; the
    caller only asks at positions following an atom. ``{`` that is not a
    valid repetition is a literal.
    """
    if index >= len(pattern):
        return False, index
    char = pattern[index]
    if char in "*+":
        index += 1
    elif char == "?":
        index += 1
    elif char == "{":
        brace = _BRACE_QUANTIFIER_RE.match(pattern, index)
        if brace is None or brace.group(0) == "{}":
            return False, index
        index = brace.end()
    else:
        return False, index
    # Lazy / possessive suffixes belong to the same quantifier.
    while index < len(pattern) and pattern[index] in "?+":
        index += 1
    return True, index


_BACKREFERENCE_RE = re.compile(r"\\[1-9]|\(\?P=|\\g<")


def compile_safe_regex(pattern: str, flags: Sequence[str] = ()) -> Any:
    """Compile an account-supplied regex for use with a match timeout.

    The static checks (length cap, no nested quantifier, no backreference)
    are a coarse filter that rejects the common catastrophic shapes with a
    clear message. They are not a proof: alternation overlap such as
    ``(a|aa)+$`` passes them. The guarantee is the engine: patterns compile
    with the ``regex`` module and every scan passes
    ``timeout=CUSTOM_PATTERN_TIMEOUT_SECONDS``, which interrupts matching
    from inside the C loop. A thread-pool timeout cannot, because CPython
    holds the GIL while matching.

    Raises:
        UnsafePatternError: Empty, too long, invalid flag, nested quantifier,
            backreference, or a pattern the engine rejects.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        raise UnsafePatternError("Custom pattern regex must not be empty")
    if len(pattern) > MAX_CUSTOM_PATTERN_LENGTH:
        raise UnsafePatternError(
            f"Custom pattern regex exceeds {MAX_CUSTOM_PATTERN_LENGTH} characters"
        )
    re_flags = 0
    for flag in flags:
        if flag not in _REGEX_FLAG_MAP:
            raise UnsafePatternError(
                f"Unknown regex flag {flag!r}; supported: {sorted(_REGEX_FLAG_MAP)}"
            )
        re_flags |= _REGEX_FLAG_MAP[flag]
    if _has_nested_quantifier(pattern):
        raise UnsafePatternError(
            "Custom pattern has a nested quantifier (for example '(a+)+'), "
            "which can backtrack catastrophically. Rewrite it without a "
            "quantifier inside a quantified group."
        )
    if _BACKREFERENCE_RE.search(pattern):
        raise UnsafePatternError(
            "Custom pattern uses a backreference, which is not supported."
        )
    try:
        # VERSION0 keeps ``re`` semantics; only the timeout is new.
        return timeout_regex.compile(pattern, re_flags | timeout_regex.VERSION0)
    except (timeout_regex.error, ValueError, OverflowError) as exc:
        raise UnsafePatternError(f"Invalid regex: {exc}") from exc


def finditer_with_timeout(
    compiled: Any, text: str, timeout: float = CUSTOM_PATTERN_TIMEOUT_SECONDS
) -> List[Any]:
    """All matches of an account pattern, or :class:`DetectorTimeoutError`."""
    try:
        return list(compiled.finditer(text, timeout=timeout))
    except TimeoutError as exc:
        raise DetectorTimeoutError(
            f"custom pattern exceeded {timeout:.2f}s on {len(text)} characters"
        ) from exc


def compile_keyword_pattern(
    terms: Sequence[str], case_sensitive: bool = False
) -> re.Pattern[str]:
    """Whole-word alternation over ``terms``.

    Word boundaries are expressed as ``(?<!\\w)``/``(?!\\w)`` so a term that
    starts or ends with punctuation (``c++``) still matches only as a whole
    token.
    """
    cleaned = [term.strip() for term in terms if term and term.strip()]
    if not cleaned:
        raise ValueError("Keyword list must contain at least one term")
    if len(cleaned) > MAX_KEYWORD_TERMS:
        raise ValueError(f"Keyword list exceeds {MAX_KEYWORD_TERMS} terms")
    for term in cleaned:
        if len(term) > MAX_KEYWORD_TERM_LENGTH:
            raise ValueError(
                f"Keyword term exceeds {MAX_KEYWORD_TERM_LENGTH} characters"
            )
    ordered = sorted(set(cleaned), key=len, reverse=True)
    body = "|".join(re.escape(term) for term in ordered)
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", flags)


# ---------------------------------------------------------------------------
# Built-in detectors
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# US national shape, kept byte-for-byte from the original detector so the
# existing model I/O rules keep matching what they matched before.
_PHONE_US_RE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}(?!\w)"
)
# E.164 with optional grouping: +49 30 1234567, +44 (0)20 7946 0958, +14155552671.
_PHONE_E164_RE = re.compile(
    r"(?<![\w+])\+[1-9]\d{0,2}(?:[\s.\-]?\(?\d{1,4}\)?){2,6}(?!\w)"
)
# EU national trunk-prefixed numbers: 030 1234567, 06 12 34 56 78, 020 7946 0958.
_PHONE_EU_RE = re.compile(r"(?<![\w+])0\d(?:[\s.\-/]?\d){7,12}(?!\w)")

_CARD_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")

_IBAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}(?![A-Z0-9])")

_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6_RE = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")

_MONTHS = (
    "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    "aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
    "januar|februar|märz|mai|juni|juli|oktober|dezember|"
    "janvier|février|mars|avril|juin|juillet|août|septembre|octobre|novembre|décembre"
)
_DATE_RE = re.compile(
    r"(?<!\d)(?:"
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"  # 1980-05-01
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"  # 01/05/1980, 1.5.80
    rf"|\d{{1,2}}\.?\s+(?:{_MONTHS})\.?,?\s+\d{{4}}"  # 1 May 1980
    rf"|(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"  # May 1, 1980
    r")(?!\d)",
    re.IGNORECASE,
)
_DOB_KEYWORD_RE = re.compile(
    r"(?<!\w)(?:"
    r"d\.?o\.?b\.?|date\s+of\s+birth|birth\s*date|birthday|born(?:\s+on)?|"
    r"geburtsdatum|geb\.|geboren(?:\s+am)?|"
    r"date\s+de\s+naissance|n[ée]e?\s+le|"
    r"geboortedatum|fecha\s+de\s+nacimiento|data\s+de\s+nascimento"
    r")(?!\w)",
    re.IGNORECASE,
)
_DOB_WINDOW_AFTER = 40
_DOB_WINDOW_BEFORE = 16

# National identifiers. Shapes first, checksums in code where one exists.
_SSN_RE = re.compile(
    r"(?<![\w-])(?!000|666|9\d\d)\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}(?![\w-])"
)
_DE_TAX_ID_RE = re.compile(r"(?<!\d)\d{2}[ ]?\d{3}[ ]?\d{3}[ ]?\d{3}(?!\d)")
_UK_NINO_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-CEGHJ-PR-TW-Za-ceghj-pr-tw-z][A-CEGHJ-NPR-TW-Za-ceghj-npr-tw-z]"
    r"[ ]?\d{2}[ ]?\d{2}[ ]?\d{2}[ ]?[A-Da-d](?![A-Za-z0-9])"
)
_UK_NINO_FORBIDDEN_PREFIXES = frozenset({"BG", "GB", "NK", "KN", "TN", "NT", "ZZ"})
_FR_NIR_RE = re.compile(
    r"(?<!\d)[12][ ]?\d{2}[ ]?(?:0[1-9]|1[0-2]|[2-9]\d)[ ]?(?:\d{2}|2[ABab])"
    r"[ ]?\d{3}[ ]?\d{3}[ ]?\d{2}(?!\d)"
)
_NL_BSN_RE = re.compile(r"(?<!\d)\d{9}(?!\d)")
_NL_BSN_KEYWORD_RE = re.compile(
    r"(?<!\w)(?:bsn|burgerservicenummer|sofi(?:nummer)?)(?!\w)", re.IGNORECASE
)
_NATIONAL_ID_LOCALES = ("us", "de", "uk", "fr", "nl")

_MRN_KEYWORD = (
    r"(?<!\w)(?:mrn|medical\s+record(?:\s+(?:number|no\.?|#))?|"
    r"patient\s+(?:id|number|no\.?)|record\s+(?:number|no\.?)|"
    r"patienten(?:nummer|-id)|fallnummer)(?!\w)\s*[:#=]?\s*"
)
_MRN_DEFAULT_ID = r"[A-Za-z]{0,3}-?\d[A-Za-z0-9\-]{3,19}"
_MRN_RE = re.compile(
    rf"{_MRN_KEYWORD}({_MRN_DEFAULT_ID})(?![A-Za-z0-9])", re.IGNORECASE
)

_NAME_TOKEN = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿA-ZÀ-ÖØ-Þ'\-]+"
_HONORIFIC_NAME_RE = re.compile(
    rf"(?<!\w)(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Herr|Frau|Mme|Mlle|M)\.?\s+"
    rf"({_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,2}})"
)
_LABEL_NAME_RE = re.compile(
    rf"(?<!\w)(?:patient(?:\s+name)?|name|full\s+name|customer(?:\s+name)?|"
    rf"employee(?:\s+name)?|contact(?:\s+name)?|applicant|insured)\s*[:=]\s*"
    rf"({_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,3}})",
    re.IGNORECASE,
)


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum over a digit string of card length."""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _iban_ok(candidate: str) -> bool:
    """IBAN mod-97 check (ISO 13616) on a space-free uppercase candidate."""
    if not (15 <= len(candidate) <= 34):
        return False
    rearranged = candidate[4:] + candidate[:4]
    try:
        numeric = int("".join(str(int(ch, 36)) for ch in rearranged))
    except ValueError:
        return False
    return numeric % 97 == 1


def _de_tax_id_ok(digits: str) -> bool:
    """German Steuer-IdNr: structure rule plus ISO 7064 MOD 11,10 check digit."""
    if len(digits) != 11 or digits[0] == "0":
        return False
    head = digits[:10]
    counts: Dict[str, int] = {}
    for ch in head:
        counts[ch] = counts.get(ch, 0) + 1
    repeated = [count for count in counts.values() if count > 1]
    if len(repeated) != 1 or repeated[0] not in (2, 3):
        return False
    product = 10
    for ch in head:
        total = (int(ch) + product) % 10
        if total == 0:
            total = 10
        product = (2 * total) % 11
    check = (11 - product) % 10
    return check == int(digits[10])


def _fr_nir_ok(compact: str) -> bool:
    """French NIR: 13 digits plus a 2-digit key, Corsica letters mapped."""
    if len(compact) != 15:
        return False
    body, key = compact[:13], compact[13:]
    if not key.isdigit():
        return False
    department = body[5:7].upper()
    if department == "2A":
        body = body[:5] + "19" + body[7:]
    elif department == "2B":
        body = body[:5] + "18" + body[7:]
    if not body.isdigit():
        return False
    return (97 - int(body) % 97) == int(key)


def _nl_bsn_ok(digits: str) -> bool:
    """Dutch BSN 11-proef."""
    if len(digits) != 9 or not digits.isdigit():
        return False
    weights = (9, 8, 7, 6, 5, 4, 3, 2, -1)
    total = sum(int(ch) * weight for ch, weight in zip(digits, weights, strict=True))
    return total % 11 == 0


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _detect_email(text: str, _config: DetectorConfig) -> Iterable[Match]:
    for found in _EMAIL_RE.finditer(text):
        yield Match("email", found.start(), found.end(), 0.95)


def _detect_phone(text: str, _config: DetectorConfig) -> Iterable[Match]:
    for found in _PHONE_US_RE.finditer(text):
        yield Match("phone", found.start(), found.end(), 0.7)
    for found in _PHONE_E164_RE.finditer(text):
        if 8 <= len(_digits(found.group(0))) <= 15:
            yield Match("phone", found.start(), found.end(), 0.85)
    for found in _PHONE_EU_RE.finditer(text):
        if 9 <= len(_digits(found.group(0))) <= 13:
            yield Match("phone", found.start(), found.end(), 0.7)


def _detect_credit_card(text: str, _config: DetectorConfig) -> Iterable[Match]:
    for found in _CARD_RE.finditer(text):
        digits = _digits(found.group(0))
        if not _luhn_ok(digits):
            continue
        # A 15-digit run starting with 1 or 2 is the French NIR shape, not an
        # issuer range (Amex is 34/37). Lower confidence so a checksum-valid
        # national id wins the overlap tie.
        confidence = 0.9 if len(digits) == 15 and digits[0] in "12" else 1.0
        yield Match("credit_card", found.start(), found.end(), confidence)


def _detect_iban(text: str, _config: DetectorConfig) -> Iterable[Match]:
    for found in _IBAN_RE.finditer(text):
        if _iban_ok(found.group(0).replace(" ", "")):
            yield Match("iban", found.start(), found.end(), 1.0)


def _detect_ip_address(text: str, _config: DetectorConfig) -> Iterable[Match]:
    for found in _IPV4_RE.finditer(text):
        try:
            ipaddress.IPv4Address(found.group(0))
        except ValueError:
            continue
        yield Match("ip_address", found.start(), found.end(), 0.9)
    for found in _IPV6_RE.finditer(text):
        candidate = found.group(0)
        if candidate.count(":") < 2 or candidate.strip(":") == "":
            continue
        try:
            ipaddress.IPv6Address(candidate)
        except ValueError:
            continue
        yield Match("ip_address", found.start(), found.end(), 0.9)


_DATE_MAX_LENGTH = 32


def _detect_date_of_birth(text: str, _config: DetectorConfig) -> Iterable[Match]:
    """The nearest date after (or just before) a birth keyword.

    Each keyword binds to at most one date on either side, and the date
    regex only runs inside those windows, so cost follows the number of
    keywords rather than the amount of digits in the text.
    """
    seen: set[tuple[int, int]] = set()
    for keyword in _DOB_KEYWORD_RE.finditer(text):
        k_start, k_end = keyword.start(), keyword.end()
        after = _DATE_RE.search(
            text, k_end, k_end + _DOB_WINDOW_AFTER + _DATE_MAX_LENGTH
        )
        if after is not None and after.start() <= k_end + _DOB_WINDOW_AFTER:
            span = (after.start(), after.end())
            if span not in seen:
                seen.add(span)
                yield Match("date_of_birth", span[0], span[1], 0.85)
            continue
        before_start = max(0, k_start - _DOB_WINDOW_BEFORE - _DATE_MAX_LENGTH)
        before = None
        for found in _DATE_RE.finditer(text, before_start, k_start):
            before = found
        if before is not None and before.end() + _DOB_WINDOW_BEFORE >= k_start:
            span = (before.start(), before.end())
            if span not in seen:
                seen.add(span)
                yield Match("date_of_birth", span[0], span[1], 0.85)


def _detect_national_id(text: str, config: DetectorConfig) -> Iterable[Match]:
    locales = set(config.locales) or set(_NATIONAL_ID_LOCALES)
    if "us" in locales:
        for found in _SSN_RE.finditer(text):
            yield Match("national_id", found.start(), found.end(), 0.8)
    if "de" in locales:
        for found in _DE_TAX_ID_RE.finditer(text):
            if _de_tax_id_ok(_digits(found.group(0))):
                yield Match("national_id", found.start(), found.end(), 1.0)
    if "uk" in locales or "gb" in locales:
        for found in _UK_NINO_RE.finditer(text):
            if found.group(0)[:2].upper() in _UK_NINO_FORBIDDEN_PREFIXES:
                continue
            yield Match("national_id", found.start(), found.end(), 0.9)
    if "fr" in locales:
        for found in _FR_NIR_RE.finditer(text):
            if _fr_nir_ok(found.group(0).replace(" ", "")):
                yield Match("national_id", found.start(), found.end(), 1.0)
    if "nl" in locales:
        # About one in ten random 9-digit numbers passes the 11-proef, so an
        # unanchored BSN match would be noise. The keyword is required.
        keyword_positions = [k.end() for k in _NL_BSN_KEYWORD_RE.finditer(text)]
        if keyword_positions:
            for found in _NL_BSN_RE.finditer(text):
                if not _nl_bsn_ok(found.group(0)):
                    continue
                if any(0 <= found.start() - pos <= 24 for pos in keyword_positions):
                    yield Match("national_id", found.start(), found.end(), 0.9)


def _detect_medical_record_number(text: str, config: DetectorConfig) -> Iterable[Match]:
    custom = config.medical_record_number_pattern
    if not custom:
        for found in _MRN_RE.finditer(text):
            yield Match("medical_record_number", found.start(1), found.end(1), 0.85)
        return
    compiled = compile_mrn_pattern(custom)
    for found in finditer_with_timeout(compiled, text):
        yield Match("medical_record_number", found.start(1), found.end(1), 0.85)


def compile_mrn_pattern(identifier_pattern: str) -> Any:
    """Compile the keyword-anchored MRN pattern around an account identifier shape.

    The account part goes through the gate; the keyword prefix is a fixed,
    reviewed pattern (it has an optional group with a quantifier inside,
    which the coarse gate would flag). The composed form is compiled here
    as well, so a shape the engine cannot embed after the keyword prefix
    fails at validation with a clear message instead of inside ``detect``.
    Inline flags such as ``(?i)`` are accepted: the ``regex`` engine scopes
    an embedded flag to the rest of the group.

    Raises:
        UnsafePatternError: The identifier pattern fails the gate or cannot
            be embedded after the keyword prefix.
    """
    inner = compile_safe_regex(identifier_pattern).pattern
    try:
        return timeout_regex.compile(
            rf"{_MRN_KEYWORD}({inner})(?![A-Za-z0-9])",
            timeout_regex.IGNORECASE | timeout_regex.VERSION0,
        )
    except (timeout_regex.error, ValueError, OverflowError) as exc:
        raise UnsafePatternError(
            "medical_record_number_pattern cannot be embedded after the MRN "
            f"keyword prefix: {exc}. Give only the identifier shape (for "
            "example [A-Z]{2}-\\d{6}); the keyword and case-insensitive "
            "matching are added around it."
        ) from exc


def _detect_person_name(text: str, _config: DetectorConfig) -> Iterable[Match]:
    """Heuristic: honorific or label anchored. Low recall by design."""
    for found in _HONORIFIC_NAME_RE.finditer(text):
        yield Match("person_name", found.start(1), found.end(1), 0.5)
    for found in _LABEL_NAME_RE.finditer(text):
        yield Match("person_name", found.start(1), found.end(1), 0.5)


BUILTIN_TYPES: tuple[SensitiveTypeInfo, ...] = (
    SensitiveTypeInfo(
        "email", "Email address", "RFC-style mailbox address.", "alice@example.com"
    ),
    SensitiveTypeInfo(
        "phone",
        "Phone number",
        "E.164 plus US and European national formats.",
        "+44 20 7946 0958",
    ),
    SensitiveTypeInfo(
        "credit_card",
        "Payment card number",
        "13 to 19 digits that pass the Luhn check.",
        "4111 1111 1111 1111",
        checksum=True,
    ),
    SensitiveTypeInfo(
        "iban",
        "IBAN",
        "International bank account number with a valid mod-97 check.",
        "DE89 3704 0044 0532 0130 00",
        checksum=True,
    ),
    SensitiveTypeInfo(
        "ip_address",
        "IP address",
        "IPv4 or IPv6 address that parses.",
        "203.0.113.7",
    ),
    SensitiveTypeInfo(
        "date_of_birth",
        "Date of birth",
        "A date next to a birth keyword (DOB, born, Geburtsdatum).",
        "DOB: 1980-05-01",
    ),
    SensitiveTypeInfo(
        "national_id",
        "National identifier",
        "US SSN shape, DE tax id, UK NINO, FR NIR and NL BSN with checksums.",
        "AB 12 34 56 C",
        locales=_NATIONAL_ID_LOCALES,
        checksum=True,
    ),
    SensitiveTypeInfo(
        "medical_record_number",
        "Medical record number",
        "Identifier following MRN or patient-id keywords; pattern is configurable.",
        "MRN: 00123456",
    ),
    SensitiveTypeInfo(
        "person_name",
        "Person name",
        "Honorific or label anchored name (Dr. X, patient: X). Low recall.",
        "patient: Jane Doe",
    ),
)
BUILTIN_TYPE_IDS: tuple[str, ...] = tuple(info.id for info in BUILTIN_TYPES)

_BUILTIN_DETECTORS: Dict[str, DetectorFn] = {
    "email": _detect_email,
    "phone": _detect_phone,
    "credit_card": _detect_credit_card,
    "iban": _detect_iban,
    "ip_address": _detect_ip_address,
    "date_of_birth": _detect_date_of_birth,
    "national_id": _detect_national_id,
    "medical_record_number": _detect_medical_record_number,
    "person_name": _detect_person_name,
}

_REGISTERED: Dict[str, tuple[DetectorFn, SensitiveTypeInfo]] = {}


def register_detector(
    name: str,
    fn: DetectorFn,
    *,
    label: Optional[str] = None,
    description: str = "",
    example: str = "",
) -> None:
    """Register or replace a pluggable detector selectable as type ``name``.

    Mirrors ``register_moderation_backend``: tests register fakes, and an
    external recogniser (#1100) plugs in here without any ML dependency in
    the default install. Built-in type ids cannot be shadowed.
    """
    if not TYPE_NAME_RE.match(name or ""):
        raise ValueError(f"Detector name {name!r} must match {TYPE_NAME_RE.pattern}")
    if name in _BUILTIN_DETECTORS:
        raise ValueError(f"Cannot replace built-in detector {name!r}")
    _REGISTERED[name] = (
        fn,
        SensitiveTypeInfo(
            id=name,
            label=label or name,
            description=description,
            example=example,
            builtin=False,
        ),
    )


def reset_detectors() -> None:
    """Drop every registered detector. Built-ins stay."""
    _REGISTERED.clear()


def registered_type_ids() -> List[str]:
    """Names of registered (non built-in) detectors."""
    return list(_REGISTERED)


def list_types(config: Optional[DetectorConfig] = None) -> List[SensitiveTypeInfo]:
    """Catalog of built-in, registered and (when given) configured custom types."""
    items = list(BUILTIN_TYPES) + [info for _, info in _REGISTERED.values()]
    if config is not None:
        for pattern in config.custom_patterns:
            items.append(
                SensitiveTypeInfo(
                    pattern.name,
                    pattern.name,
                    "Account custom pattern.",
                    "",
                    builtin=False,
                )
            )
        for keyword_list in config.keywords:
            items.append(
                SensitiveTypeInfo(
                    keyword_list.name,
                    keyword_list.name,
                    "Account keyword list.",
                    keyword_list.terms[0] if keyword_list.terms else "",
                    builtin=False,
                )
            )
    return items


def known_type_ids(config: Optional[DetectorConfig] = None) -> List[str]:
    """Every selectable type id: built-ins, registered, and config custom names."""
    return [info.id for info in list_types(config)]


def resolve_overlaps(matches: Iterable[Match]) -> List[Match]:
    """Keep non-overlapping matches, longest first, then by position.

    Ties on length prefer the higher confidence, then the earlier start, so
    the outcome does not depend on detector iteration order.
    """
    ordered = sorted(matches, key=lambda m: (-m.length, -m.confidence, m.start, m.type))
    if not ordered:
        return []
    # Occupancy bitmap: linear in total span length, not quadratic in the
    # number of matches.
    taken = bytearray(max(m.end for m in ordered))
    kept: List[Match] = []
    for candidate in ordered:
        if any(taken[candidate.start : candidate.end]):
            continue
        taken[candidate.start : candidate.end] = b"\x01" * candidate.length
        kept.append(candidate)
    kept.sort(key=lambda m: (m.start, m.end))
    return kept


def _selected_types(config: DetectorConfig) -> List[str]:
    if config.types is None:
        return list(BUILTIN_TYPE_IDS) + list(_REGISTERED) + config.custom_names()
    return list(dict.fromkeys(config.types))


def detect(text: str, config: Optional[DetectorConfig] = None) -> List[Match]:
    """Scan ``text`` and return non-overlapping typed spans.

    Args:
        text: Any string. ``None`` or empty returns no matches.
        config: Type selection and account custom entries. ``None`` scans
            every built-in type.

    Returns:
        Matches sorted by position. Unknown type names in ``config.types``
        are ignored here; the policy schema rejects them earlier.

    Raises:
        DetectorTimeoutError: An account pattern exceeded its match budget.
            Built-in patterns are fixed and never raise.
    """
    if not text:
        return []
    config = config or DetectorConfig()
    custom_by_name = {item.name: item for item in config.custom_patterns}
    keywords_by_name = {item.name: item for item in config.keywords}
    found: List[Match] = []
    # One budget for every account pattern in this call, on top of the
    # per-pattern timeout: hundreds of slow patterns cannot add up. The
    # clock starts at the first account pattern, so built-in and registered
    # detectors are not charged against it.
    deadline: Optional[float] = None
    for type_id in _selected_types(config):
        detector = _BUILTIN_DETECTORS.get(type_id)
        if detector is not None:
            found.extend(detector(text, config))
            continue
        registered = _REGISTERED.get(type_id)
        if registered is not None:
            found.extend(
                Match(type_id, m.start, m.end, m.confidence)
                for m in registered[0](text, config)
            )
            continue
        custom = custom_by_name.get(type_id)
        if custom is not None:
            if deadline is None:
                deadline = time.monotonic() + CUSTOM_PATTERNS_TOTAL_BUDGET_SECONDS
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DetectorTimeoutError(
                    "account patterns exceeded the total budget of "
                    f"{CUSTOM_PATTERNS_TOTAL_BUDGET_SECONDS:.2f}s on one text"
                )
            compiled = custom.compiled()
            found.extend(
                Match(type_id, m.start(), m.end(), 0.8)
                for m in finditer_with_timeout(
                    compiled, text, min(CUSTOM_PATTERN_TIMEOUT_SECONDS, remaining)
                )
                if m.end() > m.start()
            )
            continue
        keyword_list = keywords_by_name.get(type_id)
        if keyword_list is not None:
            compiled = keyword_list.compiled()
            found.extend(
                Match(type_id, m.start(), m.end(), 0.8) for m in compiled.finditer(text)
            )
    return resolve_overlaps(found)


def types_found(matches: Sequence[Match]) -> List[str]:
    """Distinct type names in first-seen order."""
    return list(dict.fromkeys(m.type for m in matches))
