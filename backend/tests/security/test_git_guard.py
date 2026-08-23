"""Guard tests: git argv that can leak blob contents is rejected."""

from __future__ import annotations

import pytest

from preloop.security.git_guard import ForbiddenGitError, validate_git_argv


class TestGitGuard:
    def test_rejects_log_patch(self):
        with pytest.raises(ForbiddenGitError):
            validate_git_argv(["git", "log", "-p"])

    def test_rejects_show(self):
        with pytest.raises(ForbiddenGitError):
            validate_git_argv(["git", "show", "HEAD"])

    def test_rejects_cat_file_dump(self):
        with pytest.raises(ForbiddenGitError):
            validate_git_argv(["git", "cat-file", "-p", "HEAD"])

    def test_rejects_log_line_range(self):
        """git log -L dumps line contents at every revision."""
        with pytest.raises(ForbiddenGitError):
            validate_git_argv(["git", "log", "-L1,100:src/config.h"])
        with pytest.raises(ForbiddenGitError):
            validate_git_argv(["git", "log", "-L", "1,100:src/config.h"])

    def test_rejects_log_output_redirect(self):
        """git log --output writes to an arbitrary path."""
        with pytest.raises(ForbiddenGitError):
            validate_git_argv(["git", "log", "--output=/tmp/out"])
        with pytest.raises(ForbiddenGitError):
            validate_git_argv(["git", "log", "--output", "/tmp/out"])

    def test_allows_metadata_log(self):
        validate_git_argv(["git", "log", "--all", "--format=%H %s", "-S", "MQTT_PASS"])
        validate_git_argv(["git", "log", "--all", "--diff-filter=D", "--summary"])
