"""Change brief: parsing the PR body block and keeping it on the result."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from preloop.services.flow_pr_binding import (
    BRIEF_END,
    BRIEF_START,
    MAX_BRIEF_BULLETS,
    MAX_BRIEF_TEXT,
    brief_from_result,
    merge_result_preserving_pr_binding,
    parse_brief,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "change_brief"
PRESETS_DIR = Path(__file__).resolve().parents[2] / "presets"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _prompt(preset: str) -> str:
    return str(yaml.safe_load((PRESETS_DIR / preset).read_text())["prompt_template"])


def test_full_brief_parses_every_section() -> None:
    brief = parse_brief(_fixture("full.md"))
    assert brief is not None
    assert brief["what_changed"] == [
        "`backend/app/queue.py`: store the retry count on the job row",
        "`backend/tests/test_queue.py`: cover backoff and the retry cap",
    ]
    assert brief["why"] == [
        "The issue asks that a failing job stop retrying after five attempts"
    ]
    assert brief["assumptions"] == [
        "The queue already holds a row lock while a job runs"
    ]
    assert brief["not_verified"][0].endswith("would verify it")
    assert brief["rollback"] == "revert is clean"
    assert brief["decisions"] == [
        "Store retries on the job row; the queue already locks the row; "
        "separate retry table, in-memory counter"
    ]
    assert brief["missing"] == []
    assert brief["truncated"] is False


def test_brief_missing_sections_lists_them() -> None:
    brief = parse_brief(_fixture("partial.md"))
    assert brief is not None
    assert brief["what_changed"] == ["`README.md`: fix the install command"]
    assert brief["why"] == []
    assert brief["rollback"] == "revert is clean"
    assert brief["missing"] == ["assumptions", "not_verified", "decisions"]
    assert brief["decisions"] == []


@pytest.mark.parametrize(
    "body",
    [
        _fixture("none.md"),
        None,
        "",
        f"{BRIEF_START}\n### Why\n- no closing marker",
        f"{BRIEF_END}\n{BRIEF_START}",
    ],
)
def test_no_complete_brief_is_none(body: str | None) -> None:
    assert parse_brief(body) is None


def test_brief_is_bounded() -> None:
    bullets = "\n".join(f"- item {i}" for i in range(MAX_BRIEF_BULLETS + 5))
    body = (
        f"{BRIEF_START}\n### What changed\n{bullets}\n"
        f"### Why\n- {'x' * 5000}\n{BRIEF_END}"
    )
    brief = parse_brief(body)
    assert brief is not None
    assert len(brief["what_changed"]) == MAX_BRIEF_BULLETS
    assert brief["why"] == ["x" * MAX_BRIEF_TEXT]
    assert brief["truncated"] is True


def test_brief_beyond_the_body_limit_is_not_read() -> None:
    body = "x" * 60000 + f"\n{BRIEF_START}\n### Why\n- late\n{BRIEF_END}"
    assert parse_brief(body) is None


def test_brief_from_result_reads_the_published_body_aliases() -> None:
    full = _fixture("full.md")
    assert brief_from_result({"pr_body": full}) == parse_brief(full)
    assert brief_from_result({"pull_request": {"body": full}}) == parse_brief(full)
    assert brief_from_result({"pr_body": _fixture("none.md")}) is None
    assert brief_from_result({"summary": "no body"}) is None
    assert brief_from_result(None) is None


def test_brief_survives_an_agent_result_overwrite() -> None:
    stored = parse_brief(_fixture("full.md"))
    merged = merge_result_preserving_pr_binding(
        {"brief": stored, "decisions": []}, {"status": "success", "brief": "forged"}
    )
    assert merged is not None
    assert merged["brief"] == stored


@pytest.mark.parametrize("existing", [None, {"pr_url": "https://example.com/pr/1"}])
def test_agent_cannot_author_the_brief_key_directly(
    existing: dict[str, str] | None,
) -> None:
    merged = merge_result_preserving_pr_binding(
        existing, {"status": "success", "brief": {"why": ["forged"]}}
    )
    assert merged is not None
    assert "brief" not in merged
    assert merged["status"] == "success"


def test_brief_comes_from_the_body_the_publisher_selects() -> None:
    full = _fixture("full.md")
    oversized = "x" * 60001 + "\n" + full
    # The publisher skips an oversized pr_body and publishes pr_description.
    assert brief_from_result(
        {"pr_body": oversized, "pr_description": full}
    ) == parse_brief(full)
    assert brief_from_result({"pr_body": oversized}) is None
    # A body the publisher rejects for a forged provenance region is skipped.
    forged = full + "\n<!-- preloop:executions:start -->"
    assert brief_from_result({"pr_body": forged}) is None


def test_implementation_prompt_asks_for_the_brief_block() -> None:
    prompt = _prompt("011-automated-issue-implementation.yaml")
    flat = " ".join(prompt.split())
    assert BRIEF_START in prompt and BRIEF_END in prompt
    headings = [
        "### What changed",
        "### Why",
        "### Assumptions",
        "### Not verified",
        "### Rollback",
        "### Decisions",
    ]
    positions = [prompt.index(h) for h in headings]
    assert positions == sorted(positions)
    assert "before any `Closes` or `Refs` line" in flat
    assert "PRELOOP_DECISION markers you printed in this run" in flat
    assert "60,000 byte body limit" in flat
    assert "cite a path" in flat


def test_reviewer_summary_gains_not_verified_and_must_know() -> None:
    prompt = _prompt("002-pull-request-reviewer.yaml")
    flat = " ".join(prompt.split())
    block = prompt[
        prompt.index("<!-- PRELOOP_SUMMARY -->\n> [!NOTE]") : prompt.index(
            "<!-- /PRELOOP_SUMMARY -->"
        )
    ]
    assert block.index("**Overview**") < block.index("**Not verified**")
    assert block.index("**Not verified**") < block.index("**Must know**")
    assert block.index("**Must know**") < block.index("<sup>Written by")
    assert "up to three bullets" in block
    assert (
        "Risk Level, Overview, Not verified and Must know are substantively "
        "identical" in flat
    )
    assert "skip this step entirely" in flat
    assert "never add a second block" in flat
