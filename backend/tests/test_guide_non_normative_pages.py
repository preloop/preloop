"""Findings and design notes under docs/guide/ must not read as shipped behaviour.

A heading such as "Subagent turns: what reaches the gateway, per harness" is
easy to quote as a product capability. Pages that record observations or a
proposed design carry ``status: non-normative`` and a visible banner before
any other body text. See GitHub issue 967.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = REPO_ROOT / "docs" / "guide"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

BANNER = (
    "> **Status: findings / design note. Not shipped behaviour.** "
    "This page records observations or a proposed design. "
    "Nothing here is a product capability unless a linked release note says so."
)

# Phrases a page uses when it is not documentation of shipped behaviour.
# Matched against prose, with fenced code removed, so an example payload
# cannot recruit a shipped guide.
_MARKERS: tuple[str, ...] = (
    "findings page",
    "design note",
    "decision note",
    "nothing here has been implemented",
    "nothing in this page changes behaviour",
    "nothing in this note changes",
)

_FENCE = re.compile(r"```.*?```", re.DOTALL)


def _relative(path: Path) -> str:
    return path.relative_to(GUIDE).as_posix()


def _split_front_matter(text: str) -> tuple[str | None, str]:
    """Return ``(front matter, body)``. Body includes the title."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return None, text
    front = text[4:end]
    body = text[end + len("\n---\n") :]
    return front, body


def _front_matter(text: str) -> str | None:
    front, _body = _split_front_matter(text)
    return front


def _body(text: str) -> str:
    _front, body = _split_front_matter(text)
    return body


def _prose(text: str) -> str:
    """Page prose, without fenced examples or the banner this test requires."""
    body = _FENCE.sub("", _body(text))
    lines = [line for line in body.splitlines() if line != BANNER]
    return "\n".join(lines).lower()


def _is_non_normative(text: str) -> bool:
    prose = _prose(text)
    return any(marker in prose for marker in _MARKERS)


def _first_body_line(text: str) -> str:
    """First non-empty line after the H1, which is where the banner sits."""
    lines = _body(text).splitlines()
    seen_h1 = False
    for line in lines:
        if not seen_h1:
            if line.startswith("# "):
                seen_h1 = True
            continue
        if line.strip():
            return line
    return ""


def _guide_pages() -> list[Path]:
    return sorted(GUIDE.rglob("*.md"))


def test_non_normative_pages_are_detected() -> None:
    """The criterion names every findings or design-note page, and only those."""
    detected = [
        _relative(path)
        for path in _guide_pages()
        if _is_non_normative(path.read_text(encoding="utf-8"))
    ]
    assert detected == [
        "flow-delegation-child-approvals.md",
        "flows/scanner-binaries-decision.md",
        "subagent-session-identity.md",
    ]


def test_each_non_normative_page_declares_status() -> None:
    for path in _guide_pages():
        text = path.read_text(encoding="utf-8")
        if not _is_non_normative(text):
            assert _front_matter(text) is None or "status: non-normative" not in (
                _front_matter(text) or ""
            )
            continue
        front = _front_matter(text)
        assert front is not None, _relative(path)
        assert "status: non-normative" in front.splitlines(), _relative(path)


def test_banner_is_the_first_line_of_body_text() -> None:
    for path in _guide_pages():
        text = path.read_text(encoding="utf-8")
        if not _is_non_normative(text):
            assert BANNER not in text, _relative(path)
            continue
        assert _first_body_line(text) == BANNER, _relative(path)
        body = _body(text)
        assert body.lstrip().startswith("# "), _relative(path)


def test_contributing_states_the_banner_rule() -> None:
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "status: non-normative" in text
    assert BANNER in text
    assert "docs/guide/" in text
