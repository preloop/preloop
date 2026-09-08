"""Tests for the Pull Request Reviewer preset.

Covers the identity/tool contract and the Issue Coverage slice: the preset
must read the issue a PR references and say whether the PR addresses it in
full or in part, without inventing criteria and without letting that verdict
change the review action.
"""

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from preloop.services.issue_references import (
    extract_issue_references,
)
from preloop.services.prompt_resolvers.base import ResolverContext
from preloop.services.prompt_resolvers.trigger_event import TriggerEventResolver

PRESET_FILE = "002-pull-request-reviewer.yaml"
PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"

EXPECTED_TOOLS = [
    "get_issue",
    "get_pull_request",
    "update_pull_request",
    "add_comment",
    "update_comment",
]

FORBIDDEN_TOOLS = {
    "create_issue": "follow-ups are proposed in the review, a human files them",
    "update_issue": "the reviewer never writes to the tracker",
    "search_issues": "references come from the PR, not from a tracker search",
    "create_pull_request": "a reviewer does not open pull requests",
}


def _norm(text: str) -> str:
    """Collapse whitespace so asserts survive YAML line wrapping."""
    return " ".join(text.split())


@pytest.fixture(scope="module")
def preset() -> dict:
    path = PRESETS_DIR / PRESET_FILE
    assert path.exists(), f"Missing preset file: {path}"
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def template(preset: dict) -> str:
    return preset["prompt_template"]


@pytest.fixture(scope="module")
def prompt(template: str) -> str:
    return _norm(template)


class TestPresetIdentity:
    def test_name_and_slug(self, preset: dict) -> None:
        assert preset["name"] == "Pull Request Reviewer"
        assert preset["slug"] == "pull-request-reviewer"
        assert preset["is_preset"] is True

    def test_trigger_types_are_normalized_underscore_names(self, preset: dict) -> None:
        assert preset["trigger_event_types"] == ["pull_request_opened"]

    def test_description_mentions_issue_coverage(self, preset: dict) -> None:
        assert "in full or in part" in _norm(preset["description"])


class TestToolAllowlist:
    def test_exact_allowlist(self, preset: dict) -> None:
        names = [tool["name"] for tool in preset["allowed_mcp_tools"]]
        assert names == EXPECTED_TOOLS

    def test_issue_read_is_granted(self, preset: dict) -> None:
        """The smallest grant that makes issue coverage possible."""
        names = {tool["name"] for tool in preset["allowed_mcp_tools"]}
        assert "get_issue" in names

    @pytest.mark.parametrize("tool,reason", sorted(FORBIDDEN_TOOLS.items()))
    def test_forbidden_tool_absent(self, preset: dict, tool: str, reason: str) -> None:
        names = {tool["name"] for tool in preset["allowed_mcp_tools"]}
        assert tool not in names, f"{tool} must stay out of the allowlist: {reason}"


class TestIssueDiscoveryContract:
    def test_prompt_embeds_the_parsed_reference_field(self, template: str) -> None:
        assert (
            "{{trigger_event.payload.object_attributes.referenced_issues}}" in template
        )

    def test_discovery_step_exists(self, prompt: str) -> None:
        assert "Step 1.6: Identify the Referenced Issue(s)" in prompt

    def test_discovery_names_every_reference_kind(self, prompt: str) -> None:
        for kind in ("closes", "reference", "branch"):
            assert f"`{kind}`" in prompt

    def test_no_reference_means_no_section(self, prompt: str) -> None:
        assert "none detected" in prompt
        assert "no referenced issue" in prompt
        assert "omit the Issue Coverage section" in prompt

    def test_issue_read_budget_is_bounded(self, prompt: str) -> None:
        assert "at most 2 issues read" in prompt
        assert "at most 3 `get_issue` calls per run" in prompt

    def test_unreadable_issue_falls_back_instead_of_guessing(self, prompt: str) -> None:
        assert "UNREADABLE" in prompt
        assert "do NOT reconstruct the issue from the PR description" in prompt


class TestCoverageJudgementContract:
    def test_step_exists(self, prompt: str) -> None:
        assert "Step 2.8: Issue Coverage Judgement" in prompt

    @pytest.mark.parametrize("verdict", ["FULL", "PARTIAL", "NOT ADDRESSED", "UNCLEAR"])
    def test_all_four_verdicts_are_defined(self, prompt: str, verdict: str) -> None:
        assert f"`{verdict}`" in prompt

    def test_criteria_come_from_the_issue_only(self, prompt: str) -> None:
        assert "Never invent a criterion the issue does not state" in prompt
        assert "Quote the issue where you can" in prompt

    def test_gaps_and_follow_ups_are_required(self, prompt: str) -> None:
        assert "List the gaps" in prompt
        assert "Phrase every gap as a follow-up item" in prompt

    def test_coverage_does_not_change_the_review_action(self, prompt: str) -> None:
        assert "Coverage is not a code-review finding" in prompt
        assert "never blocks a merge" in prompt

    def test_result_json_reports_coverage(self, prompt: str) -> None:
        assert '"issue_coverage"' in prompt
        assert '"verdict": "full" | "partial" | "not_addressed" | "unclear"' in prompt


class TestStatefulSection:
    def test_section_has_its_own_marker(self, template: str) -> None:
        assert "<!-- preloop-review:issue-coverage -->" in template

    def test_summary_template_carries_the_section(self, template: str) -> None:
        assert "### 🎯 Issue Coverage" in template
        assert "Acceptance criteria as this review reads them" in template
        assert "Follow-ups (ready to file as issues):" in template

    def test_carry_forward_step_exists(self, prompt: str) -> None:
        assert "Step 3.4: Carry Issue Coverage Forward" in prompt

    def test_previous_criteria_are_reused_not_reworded(self, prompt: str) -> None:
        assert "Reuse those criteria verbatim" in prompt

    def test_checked_criteria_are_never_unchecked_silently(self, prompt: str) -> None:
        assert (
            "Never uncheck a criterion a previous review checked unless the code that satisfied it is gone"
            in prompt
        )

    def test_incremental_scope_keeps_out_of_scope_criteria(self, prompt: str) -> None:
        assert (
            "In INCREMENTAL scope, re-check only criteria whose file appears in the changed-files list"
            in prompt
        )


class FakeTracker:
    """Minimal stand-in for the tracker behind the `get_issue` MCP tool.

    Keyed the way `_find_issue_by_identifier` resolves identifiers: by issue
    URL and by `org/repo#123` key.
    """

    def __init__(self, issues: dict[str, dict]):
        self._issues = issues
        self.calls: list[str] = []

    def get_issue(self, identifier: str) -> dict:
        self.calls.append(identifier)
        try:
            return self._issues[identifier]
        except KeyError:
            raise LookupError(f"Issue not found: {identifier}")


ISSUE_123 = {
    "key": "org/repo#123",
    "title": "Widget picker loses the last selection",
    "url": "https://github.com/org/repo/issues/123",
    "description": (
        "## Acceptance criteria\n"
        "- The picker restores the last selection after a reload\n"
        "- The restore is covered by a test\n"
        "- The behaviour is documented in the widget guide\n"
    ),
}


VERDICTS = ("FULL", "PARTIAL", "NOT ADDRESSED", "UNCLEAR")

# One Issue Coverage section as a review would publish it for ISSUE_123 after
# a PR that restores the selection but ships neither test nor docs.
GOOD_SECTION = (
    "### 🎯 Issue Coverage\n\n"
    "<!-- preloop-review:issue-coverage -->\n\n"
    f"**[`{ISSUE_123['key']}`]({ISSUE_123['url']}): {ISSUE_123['title']}**"
    " (verdict: **PARTIAL**)\n\n"
    "Restores the selection but ships no test and no doc update.\n\n"
    "Acceptance criteria as this review reads them (quoted from the issue):\n"
    "- [x] The picker restores the last selection after a reload"
    " - `src/widget-picker.ts:88`\n"
    "- [ ] The restore is covered by a test\n"
    "- [ ] The behaviour is documented in the widget guide\n\n"
    "Gaps:\n"
    "- No test covers the restore path - `src/widget-picker.test.ts`\n"
    "- The widget guide still describes the old behaviour\n\n"
    "Follow-ups (ready to file as issues):\n"
    "- **Cover widget picker restore with a test**: the reload path is untested.\n"
    "- **Document the widget picker restore**: the guide predates it.\n"
)


def section_shape_problems(section: str) -> list[str]:
    """Check a published Issue Coverage section against the preset's shape."""
    problems: list[str] = []
    if "<!-- preloop-review:issue-coverage -->" not in section:
        problems.append("missing the issue-coverage marker")

    verdicts = re.findall(r"\(verdict: \*\*([A-Z ]+)\*\*\)", section)
    if len(verdicts) != 1:
        problems.append(f"expected exactly one verdict, found {len(verdicts)}")
    for verdict in verdicts:
        if verdict not in VERDICTS:
            problems.append(f"verdict must be one of {VERDICTS}, got {verdict!r}")

    criteria = re.findall(r"^- \[[ x]\] ", section, re.MULTILINE)
    if verdicts and verdicts[0] != "UNCLEAR" and not criteria:
        problems.append("no acceptance criteria checkboxes")

    has_gaps = "\nGaps:\n" in section
    has_follow_ups = "\nFollow-ups (ready to file as issues):\n" in section
    if verdicts and verdicts[0] in ("PARTIAL", "NOT ADDRESSED"):
        if not has_gaps:
            problems.append("a partial verdict must list gaps")
        if has_gaps and not has_follow_ups:
            problems.append("gaps listed with no follow-ups")
    if verdicts and verdicts[0] == "FULL" and (has_gaps or has_follow_ups):
        problems.append("a full verdict must not list gaps or follow-ups")
    return problems


def mutate_section(section: str, mutation: str) -> str:
    """Break one rule of the section shape, for the negative tests."""
    if mutation == "drop_marker":
        return section.replace("<!-- preloop-review:issue-coverage -->\n\n", "")
    if mutation == "invent_verdict":
        return section.replace("**PARTIAL**", "**MOSTLY DONE**")
    if mutation == "drop_criteria":
        return re.sub(r"^- \[[ x]\] .*\n", "", section, flags=re.MULTILINE)
    if mutation == "gaps_without_followups":
        return section[: section.index("Follow-ups (ready to file as issues):")]
    raise AssertionError(f"unknown mutation: {mutation}")


def _render_preset_prompt(template: str, trigger_event_data: dict) -> str:
    """Resolve every {{trigger_event...}} placeholder in the preset prompt."""
    resolver = TriggerEventResolver()
    context = ResolverContext(
        db=MagicMock(),
        trigger_event_data=trigger_event_data,
        flow_id="flow-1",
        execution_id="exec-1",
    )
    rendered = template
    for placeholder in set(re.findall(r"\{\{(trigger_event[^}]*)\}\}", template)):
        path = placeholder[len("trigger_event") :].lstrip(".")
        value = asyncio.run(resolver.resolve(path, context))
        if value is not None:
            rendered = rendered.replace("{{" + placeholder + "}}", value)
    return rendered


class TestEndToEndWithFakeTracker:
    """A PR that closes an issue, from webhook payload to section shape."""

    @pytest.fixture
    def trigger_event_data(self) -> dict:
        return {
            "source": "github",
            "type": "pull_request_opened",
            "payload": {
                "repository": {"full_name": "org/repo"},
                "pull_request": {
                    "number": 45,
                    "title": "Restore the widget picker selection",
                    "body": "Closes #123\n\nRestores the selection on reload.",
                    "html_url": "https://github.com/org/repo/pull/45",
                    "head": {"ref": "123-restore-selection"},
                    "base": {"ref": "main"},
                    "user": {"login": "dev"},
                },
            },
        }

    def test_reference_reaches_the_prompt(
        self, template: str, trigger_event_data: dict
    ) -> None:
        rendered = _render_preset_prompt(template, trigger_event_data)
        assert "org/repo#123 [closes, from body]" in rendered
        assert "https://github.com/org/repo/issues/123" in rendered
        assert "{{trigger_event." not in rendered

    def test_the_identifier_in_the_prompt_resolves_against_the_tracker(
        self, trigger_event_data: dict
    ) -> None:
        tracker = FakeTracker({ISSUE_123["url"]: ISSUE_123})
        pr = trigger_event_data["payload"]["pull_request"]
        refs = extract_issue_references(
            description=pr["body"],
            title=pr["title"],
            branch=pr["head"]["ref"],
            repo_path="org/repo",
            host="github.com",
            platform="github",
            self_number=pr["number"],
        )
        assert [ref.key for ref in refs] == ["org/repo#123"]
        issue = tracker.get_issue(refs[0].identifier())
        assert issue["title"] == ISSUE_123["title"]
        assert tracker.calls == ["https://github.com/org/repo/issues/123"]

    def test_missing_issue_is_reported_not_invented(
        self, prompt: str, trigger_event_data: dict
    ) -> None:
        tracker = FakeTracker({})
        with pytest.raises(LookupError):
            tracker.get_issue("https://github.com/org/repo/issues/123")
        # The prompt tells the reviewer exactly what to do with that failure.
        assert "verdict UNCLEAR with the reason" in prompt

    def test_section_shape_a_review_can_fill_in(
        self, template: str, trigger_event_data: dict
    ) -> None:
        """The published section shape, filled with the fake tracker's issue."""
        rendered = _render_preset_prompt(template, trigger_event_data)
        start = rendered.index("### 🎯 Issue Coverage")
        section = rendered[start : rendered.index("### ⚠️ Issues Found", start)]

        # Marker first: the section is updated in place like the rest.
        assert "<!-- preloop-review:issue-coverage -->" in section
        # Issue identity, one verdict, reasoning, criteria, gaps, follow-ups.
        assert "{issue_key}" in section and "{issue_url}" in section
        assert "FULL | PARTIAL | NOT ADDRESSED | UNCLEAR" in section
        assert "One line of reasoning" in section
        assert "- [x]" in section and "- [ ]" in section
        assert "Gaps:" in section
        assert "Follow-ups (ready to file as issues):" in section

    def test_a_filled_section_satisfies_the_shape(self) -> None:
        """What a review would publish for the fake tracker's issue."""
        assert section_shape_problems(GOOD_SECTION) == []

    @pytest.mark.parametrize(
        "mutation,problem",
        [
            ("drop_marker", "missing the issue-coverage marker"),
            ("invent_verdict", "verdict must be one of"),
            ("drop_criteria", "no acceptance criteria checkboxes"),
            ("gaps_without_followups", "gaps listed with no follow-ups"),
        ],
    )
    def test_broken_sections_are_caught(self, mutation: str, problem: str) -> None:
        broken = mutate_section(GOOD_SECTION, mutation)
        assert any(problem in item for item in section_shape_problems(broken))
