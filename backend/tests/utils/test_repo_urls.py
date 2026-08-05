"""Tests for preloop.utils.repo_urls module."""

import pytest

from preloop.utils.repo_urls import (
    repo_url_log_location,
    tracker_host_kind,
)


class TestTrackerHostKind:
    def test_github_hostnames(self) -> None:
        assert tracker_host_kind("https://github.com/org/repo") == "github"
        assert tracker_host_kind("https://api.github.com/org/repo") == "github"

    def test_gitlab_hostnames(self) -> None:
        assert tracker_host_kind("https://gitlab.com/org/repo") == "gitlab"
        assert tracker_host_kind("https://code.gitlab.example.com/org/repo") == "gitlab"

    def test_rejects_substring_false_positives(self) -> None:
        assert tracker_host_kind("https://notgithub.com/repo") is None
        assert tracker_host_kind("https://mygitlab.com/repo") is None


class TestInjectOauthTokenRemoved:
    """``inject_oauth_token`` was removed as part of the fix for issue #173.

    It embedded the tracker token in the clone URL, which made the secret part
    of the repository's ``origin`` remote and leaked it through ``git remote
    -v`` into flow execution logs. Credentials are now delivered by
    ``preloop.utils.git_credentials`` via a git credential helper.

    This test pins the removal so the helper cannot quietly come back.
    """

    def test_helper_is_gone(self) -> None:
        import preloop.utils.repo_urls as repo_urls

        assert not hasattr(repo_urls, "inject_oauth_token")

        with pytest.raises(ImportError):
            from preloop.utils.repo_urls import inject_oauth_token  # noqa: F401


class TestRepoUrlLogLocation:
    def test_strips_userinfo(self) -> None:
        url = "https://oauth2:secret@github.com/org/repo.git"
        assert repo_url_log_location(url) == "github.com/org/repo.git"
