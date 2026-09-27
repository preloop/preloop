"""Control-plane Git workspace for the backport flow (issue #961).

The backport flow runs no agent. This module performs the only repository
work it needs: fetch the merge commit of the original pull request, cherry-pick
it onto a new branch cut from each target branch, and push that branch. A
conflict is never resolved here: the cherry-pick is aborted, nothing is pushed,
and the conflicting paths are returned so a person can take over.

Hardening follows :class:`preloop.services.trusted_publisher.CleanGitRepository`:
no system or global Git config, no hooks, no credential helper, no redirects,
HTTPS only, and Git's stderr (which can echo credentials) never leaves this
module. The credential travels as an ``http.<url>.extraHeader`` scoped to the
repository URL, never in the URL itself.
"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

GIT_TIMEOUT_SECONDS = 300
MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_CONFLICTING_FILES = 200
# Merge commits need their parents to diff against; two levels is enough.
MERGE_COMMIT_FETCH_DEPTH = 2
# Fallback when the host refuses to serve a commit by id: walk back this far
# on the branch the pull request merged into.
SOURCE_BRANCH_FETCH_DEPTH = 100

CherryPickStatus = Literal["applied", "conflict", "empty"]


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill a Git child and every helper it started.

    The child was started with ``start_new_session=True``, so its pid is also
    its process group id. Falls back to killing the child alone when the group
    is already gone or group signals are unavailable.
    """
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


class BackportGitError(RuntimeError):
    """A Git step failed. The message is safe to show and never has stderr."""


@dataclass(frozen=True)
class CherryPickOutcome:
    """What a cherry-pick onto one target branch produced.

    Attributes:
        status: ``applied`` when a new commit exists on the branch,
            ``conflict`` when Git stopped on unmerged paths, ``empty`` when the
            change is already present on the target.
        head_sha: The new commit for ``applied``, otherwise None.
        conflicting_files: Unmerged paths for ``conflict``.
    """

    status: CherryPickStatus
    head_sha: Optional[str] = None
    conflicting_files: tuple[str, ...] = ()


def _git_binary() -> str:
    """Prefer the system Git over whatever is first on PATH."""
    if os.path.exists("/usr/bin/git"):
        return "/usr/bin/git"
    found = shutil.which("git")
    if not found:
        raise BackportGitError("Git is not installed on this worker")
    return found


def validate_repository_url(url: str, *, allow_file_protocol: bool = False) -> str:
    """Accept a credential-free HTTPS repository URL.

    Args:
        url: Repository clone URL taken from the trigger payload.
        allow_file_protocol: Tests only. Permits a local ``file://`` remote.

    Returns:
        The URL unchanged.

    Raises:
        BackportGitError: The URL is not HTTPS, embeds credentials, or carries
            a query or fragment.
    """
    parsed = urlsplit(url or "")
    if allow_file_protocol and parsed.scheme == "file":
        return url
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise BackportGitError("The repository URL must be a credential-free HTTPS URL")
    return url


class BackportWorkspace:
    """A disposable non-bare repository used for one backport run."""

    def __init__(
        self,
        directory: Path,
        *,
        repository_url: str,
        token: Optional[str],
        auth_username: str,
        committer_name: str,
        committer_email: str,
        allow_file_protocol: bool = False,
    ) -> None:
        """Bind the workspace to one remote and one committer identity.

        Args:
            directory: Empty directory owned by the caller.
            repository_url: Remote the branches are fetched from and pushed to.
            token: Credential for the remote, or None for anonymous access.
            auth_username: HTTP basic user paired with ``token``
                (``x-access-token`` on GitHub, ``oauth2`` on GitLab).
            committer_name: Committer recorded on cherry-picked commits. The
                original author is kept.
            committer_email: Committer email.
            allow_file_protocol: Tests only. Permits a local ``file://`` remote.
        """
        self.directory = directory
        self.repository_url = validate_repository_url(
            repository_url, allow_file_protocol=allow_file_protocol
        )
        self._allow_file_protocol = allow_file_protocol
        self._git = _git_binary()
        self._environment = {
            "PATH": os.defpath,
            "HOME": str(directory),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file" if allow_file_protocol else "https",
            "GIT_COMMITTER_NAME": committer_name,
            "GIT_COMMITTER_EMAIL": committer_email,
            "LC_ALL": "C",
            "GIT_CONFIG_COUNT": "0",
        }
        if token:
            header = base64.b64encode(f"{auth_username}:{token}".encode()).decode()
            self._environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"http.{self.repository_url}.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {header}",
                }
            )
        self._lock = threading.Lock()
        self._closed = False
        self._process: Optional[subprocess.Popen[bytes]] = None

    def close(self) -> None:
        """Stop the workspace: kill a running Git child and refuse new ones.

        Safe to call from another thread. The async runner calls it when it is
        cancelled (for example by the flow's timeout budget), because
        cancelling ``asyncio.to_thread`` does not stop the worker thread and
        its Git child would otherwise keep running, and possibly push, after
        the execution was already recorded as failed. Git runs in its own
        process group, so its transport and pack helpers are killed with it.
        """
        with self._lock:
            self._closed = True
            process = self._process
        if process is not None and process.poll() is None:
            _kill_process_group(process)

    def _run(self, *args: str, check: bool = True) -> tuple[int, str]:
        """Run one Git command authored by this module.

        Args:
            *args: Git arguments.
            check: Raise on a non-zero exit instead of returning it.

        Returns:
            ``(returncode, stdout)`` with stdout decoded and stripped.

        Raises:
            BackportGitError: Git could not run, timed out, produced too much
                output, or failed while ``check`` was set.
        """
        command = [
            self._git,
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            "-c",
            f"protocol.file.allow={'always' if self._allow_file_protocol else 'never'}",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-c",
            "rerere.enabled=false",
            "-c",
            "commit.gpgSign=false",
            "-C",
            str(self.directory),
            *args,
        ]
        with self._lock:
            if self._closed:
                raise BackportGitError("Backport workspace was closed")
            try:
                process = subprocess.Popen(
                    command,
                    env=self._environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    # Git's stderr can include the remote URL and auth failures.
                    stderr=subprocess.DEVNULL,
                    # Own process group, so close() and the timeout also stop
                    # remote-https, fetch-pack and pack-objects helpers.
                    start_new_session=True,
                )
            except OSError as exc:
                raise BackportGitError(f"Git {args[0]} could not run") from exc
            self._process = process
        try:
            stdout, _ = process.communicate(timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(process)
            process.communicate()
            raise BackportGitError(f"Git {args[0]} timed out") from exc
        finally:
            with self._lock:
                self._process = None
        if self._closed:
            raise BackportGitError("Backport workspace was closed")
        if len(stdout) > MAX_GIT_OUTPUT_BYTES:
            raise BackportGitError(f"Git {args[0]} produced too much output")
        if check and process.returncode:
            raise BackportGitError(f"Git {args[0]} failed")
        return process.returncode, stdout.decode("utf-8", "replace").strip()

    def init(self) -> None:
        """Create an empty repository with no template (so no sample hooks)."""
        self._run("init", "-q", "--template=", str(self.directory))

    def fetch_commit(self, sha: str, *, fallback_branch: Optional[str]) -> None:
        """Fetch ``sha`` and its parents.

        GitHub and GitLab serve a reachable commit by id. When a host refuses,
        the branch the pull request merged into is fetched instead, and the
        commit must be within :data:`SOURCE_BRANCH_FETCH_DEPTH` of its tip.

        Args:
            sha: Full commit id of the merge commit.
            fallback_branch: Branch to fetch when fetching by id fails.

        Raises:
            BackportGitError: The commit could not be fetched.
        """
        code, _ = self._run(
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            f"--depth={MERGE_COMMIT_FETCH_DEPTH}",
            self.repository_url,
            sha,
            check=False,
        )
        if code and fallback_branch:
            self._run(
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                f"--depth={SOURCE_BRANCH_FETCH_DEPTH}",
                self.repository_url,
                f"+refs/heads/{fallback_branch}:refs/preloop/source",
                check=False,
            )
        present, _ = self._run("cat-file", "-e", f"{sha}^{{commit}}", check=False)
        if present:
            raise BackportGitError(
                "The merge commit of the original pull request could not be fetched"
            )

    def parent_count(self, sha: str) -> int:
        """Parents recorded in the commit object (not shallow grafts)."""
        _, raw = self._run("cat-file", "-p", sha)
        header = raw.split("\n\n", 1)[0]
        return sum(1 for line in header.splitlines() if line.startswith("parent "))

    def remote_branch_sha(self, branch: str) -> Optional[str]:
        """The remote tip of ``branch``, or None when it does not exist.

        Raises:
            BackportGitError: The remote could not be read (auth, network).
        """
        _, out = self._run(
            "ls-remote", "--refs", self.repository_url, f"refs/heads/{branch}"
        )
        for line in out.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == f"refs/heads/{branch}":
                return fields[0]
        return None

    def fetch_branch(self, branch: str) -> str:
        """Fetch the tip of ``branch`` and return the local ref holding it."""
        local_ref = f"refs/preloop/targets/{branch}"
        self._run(
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--depth=1",
            self.repository_url,
            f"+refs/heads/{branch}:{local_ref}",
        )
        return local_ref

    def cherry_pick(
        self, *, base_ref: str, branch: str, sha: str, mainline: Optional[int]
    ) -> CherryPickOutcome:
        """Cherry-pick ``sha`` onto a new local ``branch`` cut from ``base_ref``.

        The working tree is always left clean: a conflict or an empty result
        is aborted before returning, so the next target starts fresh.

        Args:
            base_ref: Local ref of the target branch tip.
            branch: Local branch to create (reset if it exists locally).
            sha: Commit to cherry-pick.
            mainline: ``1`` for a merge commit (diff against the first parent,
                the branch it merged into), None for a single-parent commit.

        Returns:
            The outcome for this target.

        Raises:
            BackportGitError: Git failed for a reason other than a conflict or
                an empty result.
        """
        self._run("checkout", "-q", "-f", "-B", branch, base_ref)
        _, before = self._run("rev-parse", "HEAD")
        args = ["cherry-pick", "-x", "--no-rerere-autoupdate"]
        if mainline:
            args += ["-m", str(mainline)]
        code, _ = self._run(*args, sha, check=False)
        _, after = self._run("rev-parse", "HEAD")
        if code == 0 and after != before:
            return CherryPickOutcome(status="applied", head_sha=after)

        _, unmerged = self._run("diff", "--name-only", "--diff-filter=U", "-z")
        conflicting = tuple(sorted({name for name in unmerged.split("\0") if name}))[
            :MAX_CONFLICTING_FILES
        ]
        in_progress, _ = self._run(
            "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", check=False
        )
        _, status = self._run("status", "--porcelain", "--untracked-files=no")
        self._run("cherry-pick", "--abort", check=False)
        self._run("reset", "-q", "--hard", before, check=False)
        if conflicting:
            return CherryPickOutcome(status="conflict", conflicting_files=conflicting)
        if in_progress == 0 and not status:
            # Git stopped because the result would be an empty commit: the
            # change is already on the target branch.
            return CherryPickOutcome(status="empty")
        raise BackportGitError("Git cherry-pick failed")

    def push_new_branch(self, branch: str) -> None:
        """Push ``HEAD`` to a branch that must not exist yet on the remote.

        ``--force-with-lease=<ref>:`` with an empty expectation refuses the
        push if anybody created the branch since it was checked, so a branch a
        person already worked on is never overwritten.
        """
        ref = f"refs/heads/{branch}"
        self._run(
            "push",
            "--porcelain",
            f"--force-with-lease={ref}:",
            self.repository_url,
            f"HEAD:{ref}",
        )
