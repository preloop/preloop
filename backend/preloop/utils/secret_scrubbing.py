"""Redaction of secrets from agent output before it is persisted or displayed.

This is the defense-in-depth half of the fix for the credential leak in issue
#173. The primary fix stops Preloop from putting tokens in git remote URLs at
all, but agent output is arbitrary: an agent can echo an environment variable,
paste a curl command, or print a URL it constructed itself. Anything that
reaches flow execution logs is visible in the console and over the API, so it
is scrubbed on the way in.

Applied to every agent log line, so patterns must be anchored and cheap.
"""

from __future__ import annotations

import re
from typing import Any, List, Match, Optional, Pattern, Tuple

REDACTED = "[REDACTED]"

# Known token formats, most specific first. Each pattern keeps its
# human-recognizable prefix so a redacted log still says which kind of
# credential leaked, which is what an operator needs in order to rotate it.
_TOKEN_PATTERNS: List[Tuple[Pattern[str], str]] = [
    # GitHub fine-grained PAT, and the classic ghp_/gho_/ghu_/ghs_/ghr_ family.
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), f"github_pat_{REDACTED}"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), f"ghp_{REDACTED}"),
    # GitLab personal/project/deploy tokens and CI job tokens.
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"), f"glpat-{REDACTED}"),
    (
        re.compile(r"\bgl(?:ptt|rt|soat|ft|imt|agent)-[A-Za-z0-9_\-]{16,}"),
        f"gl-token-{REDACTED}",
    ),
    # OpenAI and Anthropic keys, which flow through the same containers.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), f"sk-ant-{REDACTED}"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), f"sk-{REDACTED}"),
    # Preloop's own API tokens.
    (re.compile(r"\bpreloop_[A-Za-z0-9_\-]{16,}"), f"preloop_{REDACTED}"),
]

# Credentials embedded in a URL's userinfo section:
#   https://user:token@host/path   ->  https://user:[REDACTED]@host/path
#   https://github_pat_xxx@host/p  ->  https://[REDACTED]@host/path
# The userinfo grammar excludes "/" so this cannot run past the authority into
# the path, and it requires a scheme so it does not fire on email addresses.
# The scheme is capped at 64 chars: an unbounded scheme quantifier makes the
# scan quadratic on long runs of scheme-legal characters (every start offset
# rescans to the end of the run looking for "://"), which an agent-controlled
# log line could exploit. No real URI scheme approaches 64 chars.
_URL_USERINFO_WITH_PASSWORD = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]{0,63}://)"
    r"(?P<user>[^\s:/@]{1,256}):"
    r"(?P<password>[^\s/@]{1,4096})"
    r"(?=@[^\s/@]+)"
)
_URL_USERINFO_TOKEN_ONLY = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]{0,63}://)"
    r"(?P<user>[^\s:/@]{1,4096})"
    r"(?=@[^\s/@]+)"
)

# Bearer/PRIVATE-TOKEN style headers, as produced by the PR/MR creation curls.
_AUTH_HEADER_PATTERNS: List[Pattern[str]] = [
    re.compile(
        r"(?P<prefix>[Aa]uthorization:\s*(?:[Bb]earer|[Tt]oken|[Bb]asic)\s+)"
        r"(?P<secret>[^\s\"']+)"
    ),
    re.compile(r"(?P<prefix>PRIVATE-TOKEN:\s*)(?P<secret>[^\s\"']+)"),
    re.compile(r"(?P<prefix>[Xx]-[Gg]itlab-[Tt]oken:\s*)(?P<secret>[^\s\"']+)"),
]

# Usernames that carry no secret on their own; when the userinfo is only one of
# these there is nothing to redact and blanking it would make the log
# needlessly unreadable.
_NON_SECRET_USERINFO = {
    "git",
    "oauth2",
    "gitlab-ci-token",
    "x-access-token",
    "x-oauth-basic",
    "token",
    "user",
    "username",
}


def _redact_url_credentials(text: str) -> str:
    """Blank the userinfo portion of any URL that carries a credential."""

    def _password_sub(match: Match[str]) -> str:
        return f"{match.group('scheme')}{match.group('user')}:{REDACTED}"

    text = _URL_USERINFO_WITH_PASSWORD.sub(_password_sub, text)

    def _token_only_sub(match: Match[str]) -> str:
        user = match.group("user")
        # Already handled above, or a bare username with no secret in it.
        if user == REDACTED or user.lower() in _NON_SECRET_USERINFO:
            return match.group(0)
        return f"{match.group('scheme')}{REDACTED}"

    return _URL_USERINFO_TOKEN_ONLY.sub(_token_only_sub, text)


def scrub_secrets(text: Optional[str]) -> Optional[str]:
    """Return ``text`` with known credential formats replaced by placeholders.

    Order matters: URL userinfo is blanked first so that a token inside a URL
    becomes ``https://[REDACTED]@host/...`` (hiding the fact that a credential
    was present at all in that position), and remaining bare tokens elsewhere
    in the line are then replaced by their prefix-preserving placeholder.

    ``None`` and empty input are passed through so callers can apply this
    without branching.
    """

    if not text:
        return text

    scrubbed = _redact_url_credentials(text)

    for header_pattern in _AUTH_HEADER_PATTERNS:
        scrubbed = header_pattern.sub(
            lambda m: f"{m.group('prefix')}{REDACTED}", scrubbed
        )

    for pattern, replacement in _TOKEN_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)

    return scrubbed


def scrub_secret_lines(lines: List[str]) -> List[str]:
    """Scrub a batch of log lines.

    None-safe by design: entries that are ``None`` become ``""`` so callers
    always get back a list of strings.
    """

    return [scrub_secrets(line) or "" for line in lines]


def scrub_structure(data: Any) -> Any:
    """Recursively scrub every string inside a dict/list structure.

    Used as the last gate before log rows are written to the database, so that
    a secret cannot be persisted by a producer that skipped scrubbing at the
    source. Non-string scalars are returned unchanged.
    """

    if isinstance(data, str):
        return scrub_secrets(data)
    if isinstance(data, dict):
        return {key: scrub_structure(value) for key, value in data.items()}
    if isinstance(data, list):
        return [scrub_structure(item) for item in data]
    if isinstance(data, tuple):
        return tuple(scrub_structure(item) for item in data)
    return data
