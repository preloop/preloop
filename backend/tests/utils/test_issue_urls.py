"""Tests for the shared tracker issue URL parser."""

import pytest

from preloop.utils.issue_urls import issue_key_from_url, parse_issue_url


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.example.com/grp/sub/proj/-/issues/12",
        "https://gitlab.example.com/grp/sub/proj/-/work_items/12",
        "https://gitlab.example.com/grp/sub/proj/-/work_items/12/",
        "https://gitlab.example.com/grp/sub/proj/-/work_items/12?show=x#note_9",
        "http://git.example.com:8443/grp/sub/proj/-/issues/12#note_1",
    ],
)
def test_both_gitlab_forms_give_one_key(url: str) -> None:
    assert issue_key_from_url(url) == "grp/sub/proj#12"


def test_path_word_and_parts_are_reported() -> None:
    parsed = parse_issue_url("https://GitLab.Example.com/a/b/-/work_items/3")
    assert parsed is not None
    assert (parsed.project, parsed.number, parsed.path_word, parsed.host) == (
        "a/b",
        "3",
        "work_items",
        "gitlab.example.com",
    )


def test_github_jira_and_legacy_gitlab_forms() -> None:
    assert issue_key_from_url("https://github.com/org/repo/issues/123") == (
        "org/repo#123"
    )
    assert issue_key_from_url("https://gitlab.example.com/grp/proj/issues/4") == (
        "grp/proj#4"
    )
    assert issue_key_from_url("https://jira.example.com/browse/PROJ-9") == "PROJ-9"


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.example.com/grp/proj/-/merge_requests/3",
        "https://gitlab.example.com/groups/grp/-/epics/3",
        "https://gitlab.example.com/grp/proj/-/work_items/abc",
        "https://gitlab.example.com/grp/proj/-/work_items/0",
        "ftp://gitlab.example.com/grp/proj/-/issues/3",
        "grp/proj#3",
    ],
)
def test_non_issue_urls_are_rejected(url: str) -> None:
    assert parse_issue_url(url) is None
