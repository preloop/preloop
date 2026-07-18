"""Tests for preloop.utils.repo_urls module."""

from preloop.utils.repo_urls import (
    inject_oauth_token,
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


class TestInjectOauthToken:
    def test_default_oauth2_user(self) -> None:
        url = "https://github.com/org/repo.git"
        assert (
            inject_oauth_token(url, "secret-token")
            == "https://oauth2:secret-token@github.com/org/repo.git"
        )

    def test_gitlab_ci_token_user(self) -> None:
        url = "https://gitlab.com/org/repo.git"
        assert (
            inject_oauth_token(url, "secret-token", user="gitlab-ci-token")
            == "https://gitlab-ci-token:secret-token@gitlab.com/org/repo.git"
        )

    def test_token_as_username(self) -> None:
        url = "https://github.com/org/repo.git"
        assert (
            inject_oauth_token(url, "secret-token", token_as_username=True)
            == "https://secret-token@github.com/org/repo.git"
        )

    def test_skips_existing_credentials(self) -> None:
        url = "https://oauth2:existing@github.com/org/repo.git"
        assert inject_oauth_token(url, "secret-token") == url


class TestRepoUrlLogLocation:
    def test_strips_userinfo(self) -> None:
        url = "https://oauth2:secret@github.com/org/repo.git"
        assert repo_url_log_location(url) == "github.com/org/repo.git"
