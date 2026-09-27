"""Acceptance checks for the published compatibility policy.

The policy is the operator contract for which surfaces are stable and how a
breaking change is announced. These tests pin the document, the links from
the README and the release checklist, and a changelog slot for deprecations.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = REPO_ROOT / "docs" / "compatibility.md"
README = REPO_ROOT / "README.md"
RELEASING = REPO_ROOT / "RELEASING.md"
CLIFF = REPO_ROOT / "cliff.toml"

CHECKLIST_LINE = (
    "any deprecation or removal in this release follows "
    "[docs/compatibility.md](docs/compatibility.md) "
    "and is in the changelog"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_policy_lists_public_surfaces_and_the_rule() -> None:
    """The page names every public surface, the rule, and the exception."""
    text = _read(POLICY).lower()
    for needle in (
        "/api/v1",
        "openapi.yaml",
        "exit code",
        "machine-readable",
        "webhook",
        "event type",
        "preloop.agent_control.v1",
        "helm chart values",
        "result.json",
        "evidence pack",
        "forward-only",
        "upgrade hook",
        "additive",
        "one minor",
        "two minor",
        "deprecat",
        "security",
        "changelog",
        "restore from backup",
    ):
        assert needle in text, needle


def test_policy_states_what_is_not_public() -> None:
    """Unlisted surfaces are called out as free to change in any release."""
    text = _read(POLICY).lower()
    assert "not public" in text
    for needle in (
        "internal python",
        "database table",
        "console html",
        "undocumented endpoint",
    ):
        assert needle in text, needle


def test_api_versioning_is_pending_and_links_the_issue() -> None:
    """Versioning stays pending until issue 977 records a decision."""
    text = _read(POLICY)
    assert "pending" in text.lower()
    assert "https://github.com/preloop/preloop/issues/977" in text


def test_readme_and_releasing_link_the_policy() -> None:
    """Operators can reach the policy from the README and the release checklist."""
    assert "docs/compatibility.md" in _read(README)
    releasing = _read(RELEASING).lower()
    assert "docs/compatibility.md" in releasing
    assert CHECKLIST_LINE in releasing


def test_changelog_template_has_a_deprecations_place() -> None:
    """git-cliff has a Deprecated group so a deprecation is not filed as Changed."""
    text = _read(CLIFF)
    assert 'group = "Deprecated"' in text
