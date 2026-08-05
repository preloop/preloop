"""Tests for preloop.utils.git_credentials.

These cover the primary half of the fix for issue #173: tracker tokens are
delivered through a git credential helper, so the clone URL (and therefore the
repository's ``origin`` remote) never contains a secret.
"""

import os
import re
import stat
import subprocess

import pytest

from preloop.utils.git_credentials import (
    GIT_CREDENTIALS_ENV_VAR,
    GitCredential,
    build_credential_env,
    build_credential_setup_shell,
    build_credentials_file_content,
    credential_username,
    git_token_env_var,
    needs_http_path_scoping,
    strip_url_credentials,
    temporary_credential_file,
)

# The exact leak shape reported in issue #173.
LEAKED_PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"


class TestStripUrlCredentials:
    def test_removes_token_as_username(self) -> None:
        url = f"https://{LEAKED_PAT}@github.com/acme/private.git"
        assert strip_url_credentials(url) == "https://github.com/acme/private.git"

    def test_removes_user_and_password(self) -> None:
        url = "https://gitlab-ci-token:glpat-secret@gitlab.com/acme/repo.git"
        assert strip_url_credentials(url) == "https://gitlab.com/acme/repo.git"

    def test_preserves_port_and_path(self) -> None:
        url = "https://token@gitlab.example.com:8443/group/sub/repo.git"
        assert (
            strip_url_credentials(url)
            == "https://gitlab.example.com:8443/group/sub/repo.git"
        )

    def test_leaves_clean_url_untouched(self) -> None:
        url = "https://github.com/acme/private.git"
        assert strip_url_credentials(url) == url

    def test_leaves_ssh_url_untouched(self) -> None:
        """SSH remotes carry no secret in the URL and must not be rewritten."""
        url = "git@github.com:acme/private.git"
        assert strip_url_credentials(url) == url


class TestCredentialUsername:
    def test_github_uses_x_access_token(self) -> None:
        assert credential_username("github", None) == "x-access-token"

    def test_gitlab_uses_ci_token_user(self) -> None:
        assert credential_username("gitlab", None) == "gitlab-ci-token"

    def test_falls_back_to_tracker_type(self) -> None:
        """Self-hosted hosts are unrecognized by hostname but known by type."""
        assert credential_username(None, "gitlab") == "gitlab-ci-token"

    def test_unknown_uses_generic_user(self) -> None:
        assert credential_username(None, None) == "oauth2"


class TestCredentialStoreFile:
    def test_store_line_carries_token(self) -> None:
        credential = GitCredential(
            "https://github.com/acme/private.git", "x-access-token", LEAKED_PAT
        )
        assert (
            credential.store_line()
            == f"https://x-access-token:{LEAKED_PAT}@github.com/acme/private.git"
        )

    def test_store_line_percent_encodes_secret(self) -> None:
        """A token containing URL metacharacters must not corrupt the file."""
        credential = GitCredential(
            "https://github.com/acme/private.git", "x-access-token", "a/b:c@d"
        )
        line = credential.store_line()
        assert "a%2Fb%3Ac%40d" in line
        # Exactly one '@' separator, so git still parses host correctly.
        assert line.count("@") == 1

    def test_store_line_ignores_ssh_urls(self) -> None:
        credential = GitCredential("git@github.com:acme/x.git", "u", "t")
        assert credential.store_line() is None

    def test_existing_url_credentials_are_not_duplicated(self) -> None:
        """A URL that already carries a token is sanitized before storing."""
        credential = GitCredential(
            f"https://{LEAKED_PAT}@github.com/acme/private.git",
            "x-access-token",
            LEAKED_PAT,
        )
        line = credential.store_line()
        assert line == (
            f"https://x-access-token:{LEAKED_PAT}@github.com/acme/private.git"
        )
        assert line.count("@") == 1

    def test_duplicate_credentials_collapse(self) -> None:
        credential = GitCredential(
            "https://github.com/acme/private.git", "x-access-token", LEAKED_PAT
        )
        content = build_credentials_file_content([credential, credential])
        assert len(content.splitlines()) == 1

    def test_multiple_repos_each_get_a_line(self) -> None:
        content = build_credentials_file_content(
            [
                GitCredential("https://github.com/acme/a.git", "x-access-token", "t1"),
                GitCredential("https://gitlab.com/acme/b.git", "gitlab-ci-token", "t2"),
            ]
        )
        assert len(content.splitlines()) == 2

    def test_empty_credentials_produce_no_env(self) -> None:
        assert build_credential_env([]) == {}

    def test_env_carries_the_file_content(self) -> None:
        env = build_credential_env(
            [
                GitCredential(
                    "https://github.com/acme/private.git", "x-access-token", LEAKED_PAT
                )
            ]
        )
        assert LEAKED_PAT in env[GIT_CREDENTIALS_ENV_VAR]


class TestHttpPathScoping:
    def test_single_token_does_not_need_scoping(self) -> None:
        """The common case stays on host matching, which is more forgiving."""
        credentials = [
            GitCredential("https://github.com/acme/a.git", "x-access-token", "t1"),
            GitCredential("https://github.com/acme/b.git", "x-access-token", "t1"),
        ]
        assert needs_http_path_scoping(credentials) is False

    def test_two_tokens_on_one_host_need_scoping(self) -> None:
        """Without path scoping, one token would shadow the other."""
        credentials = [
            GitCredential("https://github.com/acme/a.git", "x-access-token", "t1"),
            GitCredential("https://github.com/other/b.git", "x-access-token", "t2"),
        ]
        assert needs_http_path_scoping(credentials) is True

    def test_two_tokens_on_different_hosts_do_not(self) -> None:
        credentials = [
            GitCredential("https://github.com/acme/a.git", "x-access-token", "t1"),
            GitCredential("https://gitlab.com/acme/b.git", "gitlab-ci-token", "t2"),
        ]
        assert needs_http_path_scoping(credentials) is False


class TestCredentialSetupShell:
    def test_shell_never_contains_a_secret(self) -> None:
        """The token reaches the container by env var, never via the script."""
        shell = build_credential_setup_shell()
        assert LEAKED_PAT not in shell
        assert GIT_CREDENTIALS_ENV_VAR in shell

    def test_shell_is_a_noop_without_credentials(self) -> None:
        """Guarded by -n so it is safe to emit for flows with no token."""
        shell = build_credential_setup_shell()
        assert f'[ -n "${{{GIT_CREDENTIALS_ENV_VAR}:-}}" ]' in shell

    def test_shell_unsets_the_variable(self) -> None:
        """So the agent process cannot read the token out of its environment."""
        assert f"unset {GIT_CREDENTIALS_ENV_VAR}" in build_credential_setup_shell()

    def test_shell_restricts_file_permissions(self) -> None:
        shell = build_credential_setup_shell()
        assert "umask 077" in shell
        assert "chmod 600" in shell

    def test_shell_disables_interactive_prompt(self) -> None:
        """Otherwise a credential miss hangs the container until it times out."""
        assert "GIT_TERMINAL_PROMPT=0" in build_credential_setup_shell()

    def test_http_path_flag_is_opt_in(self) -> None:
        assert "credential.useHttpPath" not in build_credential_setup_shell()
        assert "credential.useHttpPath true" in build_credential_setup_shell(
            use_http_path=True
        )


class TestGitTokenEnvVar:
    def test_names_are_one_based_and_distinct(self) -> None:
        assert git_token_env_var(0) == "PRELOOP_GIT_TOKEN_1"
        assert git_token_env_var(1) == "PRELOOP_GIT_TOKEN_2"


class TestTemporaryCredentialFile:
    def test_yields_none_without_credential(self) -> None:
        with temporary_credential_file(None) as env:
            assert env is None

    def test_creates_private_file_and_removes_it(self) -> None:
        credential = GitCredential(
            "https://github.com/acme/private.git", "x-access-token", LEAKED_PAT
        )
        with temporary_credential_file(credential) as env:
            assert env is not None
            match = re.search(r"store --file=(\S+)", env["GIT_CONFIG_VALUE_0"])
            assert match
            path = match.group(1)

            assert LEAKED_PAT in open(path).read()
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode == 0o600, (
                f"credential file is world/group readable: {oct(mode)}"
            )

        assert not os.path.exists(path), "credential file outlived the context"

    def test_file_is_removed_even_when_the_block_raises(self) -> None:
        """A failed clone must not leave a token on disk."""
        credential = GitCredential(
            "https://github.com/acme/private.git", "x-access-token", LEAKED_PAT
        )
        path = None
        try:
            with temporary_credential_file(credential) as env:
                match = re.search(r"store --file=(\S+)", env["GIT_CONFIG_VALUE_0"])
                path = match.group(1)
                raise RuntimeError("clone failed")
        except RuntimeError:
            pass

        assert path is not None
        assert not os.path.exists(path)

    def test_env_disables_interactive_prompt(self) -> None:
        credential = GitCredential(
            "https://github.com/acme/private.git", "x-access-token", LEAKED_PAT
        )
        with temporary_credential_file(credential) as env:
            assert env["GIT_TERMINAL_PROMPT"] == "0"


class TestRealGitClone:
    """End-to-end check with a real git binary.

    The unit tests above assert on strings; this one asserts on what git
    actually writes into ``.git/config``, which is what issue #173 was about.
    """

    def _git(self, *args, cwd, env):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

    @pytest.fixture
    def sandbox(self, tmp_path):
        """An isolated HOME so the real user's gitconfig is never touched."""
        home = tmp_path / "home"
        home.mkdir()
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        return env

    @pytest.fixture
    def origin_repo(self, tmp_path, sandbox):
        """A local repository to clone from."""
        repo = tmp_path / "origin"
        repo.mkdir()
        self._git("init", "-q", "-b", "main", cwd=repo, env=sandbox)
        self._git("config", "user.email", "t@example.com", cwd=repo, env=sandbox)
        self._git("config", "user.name", "Test", cwd=repo, env=sandbox)
        (repo / "README.md").write_text("hello\n")
        self._git("add", "README.md", cwd=repo, env=sandbox)
        self._git("commit", "-qm", "init", cwd=repo, env=sandbox)
        return repo

    def test_setup_shell_configures_git_and_leaves_no_secret_in_config(
        self, tmp_path, sandbox
    ):
        """Run the generated setup script through a real shell, then ask git."""
        credential = GitCredential(
            "https://github.com/acme/private.git", "x-access-token", LEAKED_PAT
        )
        env = dict(sandbox)
        env[GIT_CREDENTIALS_ENV_VAR] = build_credentials_file_content([credential])

        credentials_file = tmp_path / "creds"
        script = build_credential_setup_shell().replace(
            "/tmp/.preloop-git-credentials", str(credentials_file)
        )
        subprocess.run(
            ["sh", "-c", script], cwd=tmp_path, env=env, check=True, capture_output=True
        )

        helper = self._git(
            "config", "--global", "credential.helper", cwd=tmp_path, env=sandbox
        ).stdout
        assert "store --file=" in helper

        gitconfig = (tmp_path / "home" / ".gitconfig").read_text()
        assert LEAKED_PAT not in gitconfig, "token must live in the store, not config"

        assert LEAKED_PAT in credentials_file.read_text()
        assert stat.S_IMODE(os.stat(credentials_file).st_mode) == 0o600

    def test_cloned_repo_has_a_credential_free_remote(
        self, tmp_path, sandbox, origin_repo
    ):
        """The regression test for the report: ``git remote -v`` is safe."""
        clone_path = tmp_path / "clone"
        env = dict(sandbox)
        env[GIT_CREDENTIALS_ENV_VAR] = build_credentials_file_content(
            [
                GitCredential(
                    "https://github.com/acme/private.git", "x-access-token", LEAKED_PAT
                )
            ]
        )

        script = build_credential_setup_shell().replace(
            "/tmp/.preloop-git-credentials", str(tmp_path / "creds")
        )
        script += f" && git clone -q {origin_repo} {clone_path}"
        subprocess.run(
            ["sh", "-c", script], cwd=tmp_path, env=env, check=True, capture_output=True
        )

        remotes = self._git("remote", "-v", cwd=clone_path, env=sandbox).stdout
        assert LEAKED_PAT not in remotes
        assert "@" not in remotes.replace(str(origin_repo), "")

        config = (clone_path / ".git" / "config").read_text()
        assert LEAKED_PAT not in config

    def test_env_var_is_not_visible_to_the_agent_process(self, tmp_path, sandbox):
        """The setup script unsets it, so a later `env` dump cannot leak it."""
        env = dict(sandbox)
        env[GIT_CREDENTIALS_ENV_VAR] = build_credentials_file_content(
            [
                GitCredential(
                    "https://github.com/acme/private.git", "x-access-token", LEAKED_PAT
                )
            ]
        )
        script = build_credential_setup_shell().replace(
            "/tmp/.preloop-git-credentials", str(tmp_path / "creds")
        )
        script += " && env"

        result = subprocess.run(
            ["sh", "-c", script],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert LEAKED_PAT not in result.stdout

    def test_setup_shell_is_a_noop_without_the_env_var(self, tmp_path, sandbox):
        """Flows that clone nothing must not fail or write a global config."""
        script = build_credential_setup_shell().replace(
            "/tmp/.preloop-git-credentials", str(tmp_path / "creds")
        )
        subprocess.run(
            ["sh", "-c", script],
            cwd=tmp_path,
            env=sandbox,
            check=True,
            capture_output=True,
        )
        assert not (tmp_path / "creds").exists()
