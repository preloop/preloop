"""Parse a repository review policy.

The Pull Request Reviewer reads ``.preloop/review-policy.md`` from the
repository under review. The file is markdown. The first fenced ``yaml``
block is a compatibility config. The rest of the file is blocking prose.

Versions are quoted strings. YAML would otherwise read ``5.10`` as the
number ``5.1``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import yaml  # type: ignore[import-untyped]

POLICY_PATH = ".preloop/review-policy.md"

# perlver ships with Perl::MinimumVersion. The reviewer prompt falls back
# to a one-liner when this script is absent, and to reading the diff when
# perl itself is absent.
DEFAULT_PERL_LINTER = "perlver --blame"

_DEFAULT_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "perl": (".pl", ".pm", ".t"),
}

_FENCE = re.compile(
    r"```(?:yaml|yml)\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

_RULE_KEYS = frozenset(
    {
        "language",
        "minimum_version",
        "paths",
        "extensions",
        "version_linter",
        "allowed",
        "forbidden",
    }
)

# A version linter command is a program plus arguments. Shell syntax is
# rejected so a pull request cannot smuggle a pipeline into the sandbox
# by editing the policy file.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_./:@+=,-]+$")

_VERSION_SEGMENT = re.compile(r"^(\d+)")


class ReviewPolicyError(ValueError):
    """The policy text is present but not valid."""


@dataclass(frozen=True)
class CompatibilityRule:
    """One language, path, and minimum-version constraint.

    Attributes:
        language: Runtime name, compared case-insensitively.
        minimum_version: Quoted version string, such as ``5.10``.
        paths: Glob patterns. ``**`` matches every path.
        extensions: File suffixes including the dot, lowercased.
        version_linter: Owner command, or None to use the language default.
        allowed: Syntax the minimum version already includes.
        forbidden: Syntax that is a violation even if a linter is silent.
    """

    language: str
    minimum_version: str
    paths: tuple[str, ...]
    extensions: tuple[str, ...]
    version_linter: Optional[str]
    allowed: tuple[str, ...]
    forbidden: tuple[str, ...]


@dataclass(frozen=True)
class ReviewPolicy:
    """A parsed review policy.

    Attributes:
        prose: Blocking rules outside the yaml fence.
        rules: Compatibility entries, in file order.
    """

    prose: str
    rules: tuple[CompatibilityRule, ...]


def parse_review_policy(text: str) -> ReviewPolicy:
    """Parse ``.preloop/review-policy.md`` contents.

    Args:
        text: The whole file. An empty string is a policy with no rules.

    Returns:
        The prose and the compatibility rules.

    Raises:
        ReviewPolicyError: The yaml fence is not a valid compatibility config.
    """

    if text is None:
        raise ReviewPolicyError("review policy text is missing")
    match = _FENCE.search(text)
    if match is None:
        return ReviewPolicy(prose=text.strip(), rules=())

    prose = (text[: match.start()] + text[match.end() :]).strip()
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ReviewPolicyError(f"review policy yaml is invalid: {exc}") from exc
    if loaded is None:
        return ReviewPolicy(prose=prose, rules=())
    if not isinstance(loaded, dict):
        raise ReviewPolicyError("review policy yaml must be a mapping")
    unknown = set(loaded) - {"compatibility"}
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ReviewPolicyError(f"unknown review policy keys: {names}")
    raw_rules = loaded.get("compatibility", [])
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise ReviewPolicyError("compatibility must be a list")
    rules = tuple(_parse_rule(item, index) for index, item in enumerate(raw_rules))
    return ReviewPolicy(prose=prose, rules=rules)


def rule_matches_path(rule: CompatibilityRule, path: str) -> bool:
    """Return True when ``path`` is covered by ``rule``.

    Args:
        rule: One compatibility entry.
        path: Repository-relative path from the diff.

    Returns:
        True when both the glob and the extension match.
    """

    normalized = _normalize_path(path)
    if not normalized or normalized.endswith("/"):
        return False
    if not any(_glob_match(normalized, pattern) for pattern in rule.paths):
        return False
    if not rule.extensions:
        return True
    suffix = _suffix(normalized)
    return suffix in rule.extensions


def matching_rules(
    policy: ReviewPolicy, paths: list[str]
) -> list[tuple[str, CompatibilityRule]]:
    """Pair each changed path with the rules that cover it.

    Args:
        policy: Parsed policy.
        paths: Changed paths, repository-relative.

    Returns:
        ``(path, rule)`` pairs in path order, then rule order.
    """

    found: list[tuple[str, CompatibilityRule]] = []
    for path in paths:
        for rule in policy.rules:
            if rule_matches_path(rule, path):
                found.append((_normalize_path(path), rule))
    return found


def is_safe_version_linter(command: str) -> bool:
    """Return True when ``command`` is a program plus plain arguments.

    Args:
        command: The ``version_linter`` string from the policy.

    Returns:
        True when every token is a safe argv element. Shell operators,
        quotes, and whitespace other than single spaces are rejected.
    """

    if not command or command != command.strip():
        return False
    tokens = command.split(" ")
    if any(token == "" for token in tokens):
        return False
    return all(_SAFE_TOKEN.fullmatch(token) for token in tokens)


def linter_argv(rule: CompatibilityRule, path: str) -> Optional[list[str]]:
    """Build the argv for one changed file.

    Args:
        rule: Compatibility entry that matched ``path``.
        path: Repository-relative path.

    Returns:
        Argument vector, or None when no safe command applies. Perl with
        no ``version_linter`` uses ``perlver --blame``. An unsafe command
        is not returned; the caller treats that as a policy problem.
    """

    command = rule.version_linter
    if command is None and rule.language.lower() == "perl":
        command = DEFAULT_PERL_LINTER
    if command is None:
        return None
    if not is_safe_version_linter(command):
        return None
    argv = command.split(" ")
    normalized = _normalize_path(path)
    if normalized not in argv:
        argv.append(normalized)
    return argv


def version_tuple(value: str) -> tuple[int, ...]:
    """Parse a dotted version into integers.

    Args:
        value: A version such as ``5.10`` or ``v5.10.1``.

    Returns:
        Numeric segments. ``5.10`` is ``(5, 10)``, not ``(5, 1)``.

    Raises:
        ReviewPolicyError: ``value`` has no leading number.
    """

    text = value.strip()
    if text.lower().startswith("v"):
        text = text[1:]
    parts: list[int] = []
    for segment in text.split("."):
        match = _VERSION_SEGMENT.match(segment)
        if match is None:
            break
        parts.append(int(match.group(1)))
    if not parts:
        raise ReviewPolicyError(f"cannot read version {value!r}")
    return tuple(parts)


def version_is_newer(reported: str, minimum: str) -> bool:
    """Return True when ``reported`` is a newer version than ``minimum``.

    Args:
        reported: Version a linter reported for a file.
        minimum: Declared minimum version.

    Returns:
        True when the file requires a newer runtime. Equal versions are
        not newer. Missing trailing segments compare as zero, so ``5.10``
        and ``5.10.0`` are equal.
    """

    left = version_tuple(reported)
    right = version_tuple(minimum)
    width = max(len(left), len(right))
    left_full = left + (0,) * (width - len(left))
    right_full = right + (0,) * (width - len(right))
    return left_full > right_full


def _parse_rule(item: Any, index: int) -> CompatibilityRule:
    if not isinstance(item, dict):
        raise ReviewPolicyError(f"compatibility[{index}] must be a mapping")
    unknown = set(item) - _RULE_KEYS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ReviewPolicyError(f"compatibility[{index}] has unknown keys: {names}")
    language = item.get("language")
    if not isinstance(language, str) or not language.strip():
        raise ReviewPolicyError(f"compatibility[{index}].language is required")
    minimum = _require_version(item.get("minimum_version"), index)
    paths = _string_tuple(item.get("paths"), f"compatibility[{index}].paths")
    if not paths:
        paths = ("**",)
    extensions = _extensions(item.get("extensions"), language, index)
    linter = item.get("version_linter")
    if linter is None:
        parsed_linter: Optional[str] = None
    elif isinstance(linter, str) and linter.strip():
        parsed_linter = linter.strip()
    elif isinstance(linter, str):
        parsed_linter = None
    else:
        raise ReviewPolicyError(
            f"compatibility[{index}].version_linter must be a string"
        )
    return CompatibilityRule(
        language=language.strip(),
        minimum_version=minimum,
        paths=tuple(_normalize_path(path) or "**" for path in paths),
        extensions=extensions,
        version_linter=parsed_linter,
        allowed=_string_tuple(item.get("allowed"), f"compatibility[{index}].allowed"),
        forbidden=_string_tuple(
            item.get("forbidden"), f"compatibility[{index}].forbidden"
        ),
    )


def _require_version(value: Any, index: int) -> str:
    field = f"compatibility[{index}].minimum_version"
    if isinstance(value, bool) or value is None:
        raise ReviewPolicyError(f"{field} must be a quoted string")
    if isinstance(value, (int, float)):
        raise ReviewPolicyError(
            f"{field} must be a quoted string. YAML reads an unquoted "
            "5.10 as the number 5.1."
        )
    if not isinstance(value, str) or not value.strip():
        raise ReviewPolicyError(f"{field} must be a quoted string")
    text = value.strip()
    version_tuple(text)
    return text


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReviewPolicyError(f"{field} must be a list of strings")
    return tuple(item.strip() for item in value if item.strip())


def _extensions(value: Any, language: str, index: int) -> tuple[str, ...]:
    raw = _string_tuple(value, f"compatibility[{index}].extensions")
    if not raw:
        return _DEFAULT_EXTENSIONS.get(language.strip().lower(), ())
    normalized: list[str] = []
    for item in raw:
        suffix = item.lower()
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        normalized.append(suffix)
    return tuple(normalized)


def _normalize_path(path: str) -> str:
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _suffix(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0:
        return ""
    return name[dot:].lower()


def _glob_match(path: str, pattern: str) -> bool:
    if pattern in {"**", "**/*"}:
        return True
    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\*\*", "\x00")
    escaped = escaped.replace(r"\*", "[^/]*")
    escaped = escaped.replace("\x00", ".*")
    return re.fullmatch(escaped, path) is not None
