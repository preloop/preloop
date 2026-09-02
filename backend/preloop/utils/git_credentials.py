"""Credential-free git authentication for agent containers.

Historically Preloop authenticated git operations by embedding the tracker
token directly in the clone URL (``https://<token>@github.com/org/repo.git``).
That makes the secret part of the repository's ``origin`` remote, so anything
that prints remotes (``git remote -v``, ``git config --list``, most clone error
messages) leaks the token into flow execution logs, which are readable through
the console and the API.

This module builds the pieces needed to authenticate without ever putting the
secret in the remote URL:

- the repository is cloned from a credential-free URL,
- the token is delivered to the container in an environment variable,
- an init snippet writes a mode-0600 ``git credential store`` file from that
  variable and unsets it.

``git remote -v`` inside the workspace then prints a clean URL.
"""

from __future__ import annotations

import logging
import os
import shlex
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional
from urllib.parse import quote, urlparse, urlunparse

logger = logging.getLogger(__name__)

# Environment variable carrying the credential store file contents. Kept in the
# environment (not the shell script) so the secret is not part of the container
# command line, and so it can later be sourced from a Kubernetes Secret without
# touching the script.
GIT_CREDENTIALS_ENV_VAR = "PRELOOP_GIT_CREDENTIALS"

# Environment variable prefix for per-repository API tokens used by the
# post-execution PR/MR creation calls.
GIT_TOKEN_ENV_PREFIX = "PRELOOP_GIT_TOKEN_"

# Location of the generated credential store file inside the container.
GIT_CREDENTIALS_FILE = "/tmp/.preloop-git-credentials"

# GitHub accepts any username when the fine-grained PAT is sent as the
# password; ``x-access-token`` is the convention GitHub itself uses.
GITHUB_CREDENTIAL_USER = "x-access-token"
GITLAB_CREDENTIAL_USER = "gitlab-ci-token"
DEFAULT_CREDENTIAL_USER = "oauth2"


def credential_username(host_kind: Optional[str], tracker_type: Optional[str]) -> str:
    """Return the username to pair with a tracker token."""

    kind = (host_kind or tracker_type or "").lower()
    if kind == "github":
        return GITHUB_CREDENTIAL_USER
    if kind == "gitlab":
        return GITLAB_CREDENTIAL_USER
    return DEFAULT_CREDENTIAL_USER


def strip_url_credentials(repo_url: str) -> str:
    """Return ``repo_url`` with any embedded userinfo removed.

    Used both to sanitize URLs that arrive with credentials already attached
    and to guarantee the URL handed to ``git clone`` is safe to print.
    """

    try:
        parsed = urlparse(repo_url)
    except ValueError:
        return repo_url

    if parsed.scheme not in {"http", "https"}:
        return repo_url
    if not parsed.username and not parsed.password:
        return repo_url

    hostport = parsed.hostname or ""
    if parsed.port is not None:
        hostport = f"{hostport}:{parsed.port}"

    return urlunparse(
        (
            parsed.scheme,
            hostport,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


@dataclass(frozen=True)
class GitCredential:
    """A token scoped to one repository URL."""

    repo_url: str
    username: str
    token: str

    def store_line(self) -> Optional[str]:
        """Render one line of a ``git credential store`` file.

        Returns None when the URL is not an HTTP(S) URL, since the store helper
        only applies to HTTP(S) transports (SSH remotes carry no secret here).
        """

        parsed = urlparse(strip_url_credentials(self.repo_url))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None

        hostport = parsed.hostname
        if parsed.port is not None:
            hostport = f"{hostport}:{parsed.port}"

        # Percent-encode userinfo: tokens and usernames may contain characters
        # that would otherwise break the URL parse git performs on this file.
        user = quote(self.username, safe="")
        secret = quote(self.token, safe="")
        return f"{parsed.scheme}://{user}:{secret}@{hostport}{parsed.path}"


def build_credentials_file_content(credentials: Iterable[GitCredential]) -> str:
    """Render the credential store file for the given credentials.

    Entries are path-scoped so that, when ``credential.useHttpPath`` is on, a
    flow cloning two repositories from the same host with two different tracker
    tokens authenticates each one with its own token. Duplicate lines are
    collapsed while preserving order.
    """

    lines: List[str] = []
    seen = set()
    for credential in credentials:
        line = credential.store_line()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return "\n".join(lines)


def build_credential_env(credentials: Iterable[GitCredential]) -> Dict[str, str]:
    """Return the environment variables carrying git credentials, if any."""

    content = build_credentials_file_content(credentials)
    if not content:
        return {}
    return {GIT_CREDENTIALS_ENV_VAR: content}


def needs_http_path_scoping(credentials: Iterable[GitCredential]) -> bool:
    """Return True when a host is served by more than one distinct credential.

    ``credential.useHttpPath=true`` makes git match the stored entry against
    the full repository path. That is required to keep two tokens for the same
    host apart, but it also makes lookups brittle: any URL whose path differs
    from the one we stored (a submodule, a redirect, a ``.git`` suffix
    mismatch) finds no credential and, with ``GIT_TERMINAL_PROMPT=0``, fails.

    So it is enabled only for the ambiguous case it exists to solve, and left
    off (host-level matching, which still works against path-scoped entries)
    for the overwhelmingly common single-token flow.
    """

    per_host: Dict[str, set] = {}
    for credential in credentials:
        if credential.store_line() is None:
            continue
        parsed = urlparse(strip_url_credentials(credential.repo_url))
        host = (parsed.hostname or "").lower()
        per_host.setdefault(host, set()).add((credential.username, credential.token))
    return any(len(secrets) > 1 for secrets in per_host.values())


def build_credential_setup_shell(*, use_http_path: bool = False) -> str:
    """Return shell that installs the credential helper from the environment.

    The snippet is a no-op when no credentials were provided, so it is safe to
    emit unconditionally. ``GIT_TERMINAL_PROMPT=0`` is exported so that a
    credential miss fails immediately with a readable error instead of blocking
    the container on an interactive password prompt.
    """

    http_path_line = (
        "    git config --global credential.useHttpPath true\n" if use_http_path else ""
    )
    return f"""
export GIT_TERMINAL_PROMPT=0
if [ -n "${{{GIT_CREDENTIALS_ENV_VAR}:-}}" ]; then
    (umask 077 && printf '%s\\n' "${GIT_CREDENTIALS_ENV_VAR}" > {GIT_CREDENTIALS_FILE})
    chmod 600 {GIT_CREDENTIALS_FILE}
    git config --global credential.helper 'store --file={GIT_CREDENTIALS_FILE}'
{http_path_line}    unset {GIT_CREDENTIALS_ENV_VAR}
    echo "Configured git credential helper (credentials are not stored in remotes)"
fi
""".strip()


def build_push_auth_setup_shell(*, token_ref: str, username: str) -> str:
    """Reinstall the credential helper immediately before ``git push``.

    Clone of a public repository succeeds with no helper, so
    ``PRELOOP_GIT_CREDENTIALS`` may never be set even when a tracker token is
    available for the post-execution REST calls. The clone snippet also
    unsets that variable after writing the store file, and agents often
    overwrite ``~/.gitconfig``. Either way, ``git push`` with
    ``GIT_TERMINAL_PROMPT=0`` fails with "could not read Username for
    'https://github.com'".

    ``token_ref`` must be a shell expansion such as ``${PRELOOP_GIT_TOKEN_1}``,
    never the raw secret. The token is written only into the mode-0600 store
    file, matching the clone helper.
    """

    # token_ref is empty when this repository has no tracker token; the
    # PRELOOP_GIT_CREDENTIALS / existing-file branches still apply.
    token_quoted = f'"{token_ref}"' if token_ref else '""'
    user_quoted = shlex.quote(username)
    return f"""
export GIT_TERMINAL_PROMPT=0
if [ -n "${{{GIT_CREDENTIALS_ENV_VAR}:-}}" ]; then
    (umask 077 && printf '%s\\n' "${GIT_CREDENTIALS_ENV_VAR}" > {GIT_CREDENTIALS_FILE})
    chmod 600 {GIT_CREDENTIALS_FILE}
    git config --global credential.helper 'store --file={GIT_CREDENTIALS_FILE}'
    echo "Reinstalled git credential helper from {GIT_CREDENTIALS_ENV_VAR}"
elif [ -f {GIT_CREDENTIALS_FILE} ]; then
    git config --global credential.helper 'store --file={GIT_CREDENTIALS_FILE}'
    echo "Reinstalled git credential helper from existing store file"
elif [ -n {token_quoted} ]; then
    git config --global credential.helper 'store --file={GIT_CREDENTIALS_FILE}'
    (umask 077 && : > {GIT_CREDENTIALS_FILE})
    chmod 600 {GIT_CREDENTIALS_FILE}
    ORIGIN_URL=$(git remote get-url origin 2>/dev/null || true)
    HOST=$(printf '%s\\n' "$ORIGIN_URL" | sed -E 's#^git@([^:]+):.*#\\1#; t; s#^https?://##; s#^[^/@]*@##; s#[/:].*##')
    if [ -z "$HOST" ]; then
        HOST=github.com
    fi
    printf 'protocol=https\\nhost=%s\\nusername=%s\\npassword=%s\\n\\n' "$HOST" {user_quoted} {token_quoted} | git credential approve
    echo "Installed git credential helper from tracker token for push"
else
    echo "WARNING: no git credentials available for push"
fi
""".strip()


def git_token_env_var(repo_index: int) -> str:
    """Return the env var name holding the API token for one repository."""

    return f"{GIT_TOKEN_ENV_PREFIX}{repo_index + 1}"


@contextmanager
def temporary_credential_file(
    credential: Optional[GitCredential],
) -> Iterator[Optional[Dict[str, str]]]:
    """Yield an environment that authenticates git via a temporary file.

    For git operations Preloop runs directly on the host (rather than inside an
    agent container). The credential file is created with mode 0600, exists
    only for the duration of the block, and is removed in a ``finally`` so a
    failed clone does not leave a token on disk.

    Yields ``None`` when there is no credential, which callers pass straight to
    ``subprocess`` as ``env`` to mean "inherit the current environment".
    """

    if credential is None:
        yield None
        return

    content = build_credentials_file_content([credential])
    if not content:
        yield None
        return

    handle, path = tempfile.mkstemp(prefix="preloop-git-cred-")
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(content + "\n")
        os.chmod(path, 0o600)

        env = os.environ.copy()
        # -c flags apply to the git invocation without mutating any config file.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = f"store --file={path}"
        env["GIT_TERMINAL_PROMPT"] = "0"
        yield env
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            logger.warning("Could not remove temporary git credential file")
