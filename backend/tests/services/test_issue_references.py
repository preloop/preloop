"""Tests for the pull-request issue-reference parser.

The Pull Request Reviewer preset judges whether a PR addresses the issue it
references, so the reference has to be found reliably: closing keywords,
cross-repo forms, issue URLs, branch names, and (importantly) nothing at all
when the PR references nothing.
"""

import pytest

from preloop.services.issue_references import (
    KIND_BRANCH,
    KIND_CLOSES,
    KIND_REFERENCE,
    NO_REFERENCES,
    extract_issue_references,
    format_issue_references,
    references_from_trigger_payload,
)

GITHUB = dict(repo_path="org/repo", host="github.com", platform="github")
GITLAB = dict(repo_path="grp/sub/proj", host="gitlab.example.com", platform="gitlab")


def keys(refs):
    return [ref.key for ref in refs]


class TestGitHubForms:
    @pytest.mark.parametrize(
        "body",
        [
            "Closes #123",
            "closes #123",
            "This fixes #123 for good",
            "Fixed #123",
            "Resolves: #123",
            "resolved #123",
            "Implements #123",
        ],
    )
    def test_closing_keywords(self, body):
        refs = extract_issue_references(description=body, **GITHUB)
        assert keys(refs) == ["org/repo#123"]
        assert refs[0].kind == KIND_CLOSES
        assert refs[0].url == "https://github.com/org/repo/issues/123"

    def test_cross_repo_reference(self):
        refs = extract_issue_references(description="Fixes other-org/other#7", **GITHUB)
        assert keys(refs) == ["other-org/other#7"]
        assert refs[0].url == "https://github.com/other-org/other/issues/7"

    def test_full_issue_url_in_body(self):
        refs = extract_issue_references(
            description="Context: https://github.com/acme/tools/issues/42", **GITHUB
        )
        assert keys(refs) == ["acme/tools#42"]
        assert refs[0].kind == KIND_REFERENCE
        assert refs[0].url == "https://github.com/acme/tools/issues/42"

    def test_bare_mention_is_a_weaker_reference(self):
        refs = extract_issue_references(description="Follows on from #9", **GITHUB)
        assert keys(refs) == ["org/repo#9"]
        assert refs[0].kind == KIND_REFERENCE

    def test_closing_keyword_outranks_bare_mention_of_same_issue(self):
        refs = extract_issue_references(description="See #5. Closes #5", **GITHUB)
        assert keys(refs) == ["org/repo#5"]
        assert refs[0].kind == KIND_CLOSES

    def test_closes_entries_are_listed_before_weaker_ones(self):
        refs = extract_issue_references(
            description="Related to #2\n\nCloses #3", branch="4-slug", **GITHUB
        )
        assert keys(refs) == ["org/repo#3", "org/repo#2", "org/repo#4"]
        assert [ref.kind for ref in refs] == [KIND_CLOSES, KIND_REFERENCE, KIND_BRANCH]

    def test_self_reference_is_dropped(self):
        refs = extract_issue_references(
            description="Squashed from (#45)", self_number=45, **GITHUB
        )
        assert refs == []

    def test_commit_sha_and_anchors_are_not_issues(self):
        refs = extract_issue_references(
            description="see file.py#L12 and heading ##Notes", **GITHUB
        )
        assert refs == []


class TestGitLabForms:
    def test_closes_in_merge_request_description(self):
        refs = extract_issue_references(description="Closes #7", **GITLAB)
        assert keys(refs) == ["grp/sub/proj#7"]
        assert refs[0].url == "https://gitlab.example.com/grp/sub/proj/-/issues/7"

    def test_subgroup_issue_url(self):
        refs = extract_issue_references(
            description="https://gitlab.example.com/grp/sub/other/-/issues/31", **GITLAB
        )
        assert keys(refs) == ["grp/sub/other#31"]
        assert refs[0].url == "https://gitlab.example.com/grp/sub/other/-/issues/31"

    def test_cross_project_key(self):
        refs = extract_issue_references(description="Closes grp/other#8", **GITLAB)
        assert keys(refs) == ["grp/other#8"]


class TestJiraForms:
    def test_keyword_plus_jira_key(self):
        refs = extract_issue_references(description="Fixes PROJ-123", **GITHUB)
        assert keys(refs) == ["PROJ-123"]
        assert refs[0].kind == KIND_CLOSES
        assert refs[0].url is None
        assert refs[0].identifier() == "PROJ-123"

    def test_jira_key_in_branch(self):
        refs = extract_issue_references(branch="feature/PROJ-7-add-widget", **GITHUB)
        assert keys(refs) == ["PROJ-7"]
        assert refs[0].kind == KIND_BRANCH

    def test_lowercase_branch_slug_is_not_a_jira_key(self):
        # "abc-123" in a branch is a slug, not project ABC issue 123, and the
        # number is not in an issue-number position either. Guessing here
        # would send the reviewer to read an unrelated issue.
        assert extract_issue_references(branch="fix/abc-123-thing", **GITHUB) == []


class TestBranchPatterns:
    @pytest.mark.parametrize(
        "branch,expected",
        [
            ("123-add-widget", "org/repo#123"),
            ("123", "org/repo#123"),
            ("fix/issue-123", "org/repo#123"),
            ("feature/123-add-widget", "org/repo#123"),
            ("issues/123", "org/repo#123"),
            ("gh-123-thing", "org/repo#123"),
        ],
    )
    def test_number_recovered_from_branch(self, branch, expected):
        refs = extract_issue_references(branch=branch, **GITHUB)
        assert keys(refs) == [expected]
        assert refs[0].kind == KIND_BRANCH
        assert refs[0].source == "branch"

    @pytest.mark.parametrize(
        "branch", ["main", "release/2.0", "feat/add-widget", "dependabot/npm/lit-3.1.0"]
    )
    def test_branches_without_an_issue_number(self, branch):
        assert extract_issue_references(branch=branch, **GITHUB) == []


class TestNoReferences:
    def test_empty_inputs(self):
        assert extract_issue_references() == []

    def test_prose_without_a_reference(self):
        refs = extract_issue_references(
            description="Refactor the console shell. No issue for this one.",
            branch="chore/console-shell",
            **GITHUB,
        )
        assert refs == []

    def test_format_says_none_detected(self):
        assert format_issue_references([]) == NO_REFERENCES


class TestFormatting:
    def test_line_shape(self):
        refs = extract_issue_references(description="Closes #12", **GITHUB)
        assert (
            format_issue_references(refs)
            == "org/repo#12 [closes, from body] https://github.com/org/repo/issues/12"
        )

    def test_entries_are_semicolon_separated(self):
        refs = extract_issue_references(
            description="Closes #12 and closes #13", **GITHUB
        )
        assert format_issue_references(refs).count("; ") == 1

    def test_cap_keeps_the_strongest_references(self):
        body = " ".join(f"see #{n}" for n in range(20, 40)) + " Closes #99"
        refs = extract_issue_references(description=body, **GITHUB)
        assert len(refs) == 5
        assert refs[0].key == "org/repo#99"


class TestTriggerPayloads:
    def test_github_pull_request_payload(self):
        payload = {
            "pull_request": {"number": 45},
            "repository": {"full_name": "org/repo"},
        }
        attrs = {
            "title": "Add widget",
            "description": "Closes #123",
            "url": "https://github.com/org/repo/pull/45",
            "source_branch": "123-add-widget",
            "number": 45,
        }
        refs = references_from_trigger_payload(payload, attrs, "github")
        assert keys(refs) == ["org/repo#123"]
        assert refs[0].url == "https://github.com/org/repo/issues/123"

    def test_gitlab_merge_request_payload(self):
        payload = {
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "grp/sub/proj"},
        }
        attrs = {
            "title": "Add widget",
            "description": "Closes #7",
            "url": "https://gitlab.example.com/grp/sub/proj/-/merge_requests/12",
            "source_branch": "fix/issue-8",
            "iid": 12,
        }
        refs = references_from_trigger_payload(payload, attrs, "gitlab")
        assert keys(refs) == ["grp/sub/proj#7", "grp/sub/proj#8"]

    def test_repository_path_falls_back_to_payload_when_url_is_odd(self):
        payload = {"pull_request": {"number": 1}, "repository": {"full_name": "o/r"}}
        attrs = {"description": "Closes #4", "url": "not-a-url", "source_branch": "x"}
        refs = references_from_trigger_payload(payload, attrs, "github")
        assert keys(refs) == ["o/r#4"]
        # No host means no URL, but the key is still resolvable by get_issue.
        assert refs[0].url is None
        assert refs[0].identifier() == "o/r#4"
