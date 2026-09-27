"""Backport runner against a real local Git remote (issue #961).

The remote is a bare repository reached over ``file://`` (enabled for tests
only). The code host is an in-memory fake, so every assertion about pull
requests is about what the runner asked for, and every assertion about
branches is about what actually landed on the remote.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pytest

from preloop.services.backport import (
    BACKPORT_RESULT_KEY,
    STATUS_ALREADY_APPLIED,
    STATUS_CONFLICT,
    STATUS_EXISTS,
    STATUS_FAILED,
    STATUS_OPENED,
    STATUS_UPDATED,
    SUMMARY_COMMENT_MARKER,
    BackportPlan,
    MergedChange,
    agent_result_for,
    run_backport,
)
from preloop.services.backport_hosts import BackportHostError, HostChange

AUTHOR = {
    "GIT_AUTHOR_NAME": "Original Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Original Author",
    "GIT_COMMITTER_EMAIL": "author@example.com",
}


def git(cwd: Path, *args: str) -> str:
    """Run Git for fixture setup with no user or system config."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ALLOW_PROTOCOL": "file",
        **AUTHOR,
    }
    result = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-C", str(cwd), *args],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass
class Repo:
    """A bare remote plus the working clone that built its history."""

    remote: Path
    work: Path

    @property
    def url(self) -> str:
        return self.remote.as_uri()

    def remote_sha(self, branch: str) -> Optional[str]:
        out = git(
            self.remote,
            "for-each-ref",
            "--format=%(objectname)",
            f"refs/heads/{branch}",
        )
        return out or None

    def remote_file(self, branch: str, path: str) -> str:
        return git(self.remote, "show", f"refs/heads/{branch}:{path}")


def _commit(work: Path, path: str, content: str, message: str) -> None:
    (work / path).write_text(content)
    git(work, "add", path)
    git(work, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """History: release/1.0 merges a pull request with a merge commit.

    ``release/1.1`` and ``main`` start from the same base, so by default the
    change applies cleanly to both.
    """
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    _commit(work, "app.txt", "line one\nline two\nline three\n", "Base")
    _commit(work, "other.txt", "untouched\n", "Other file")
    git(work, "branch", "release/1.0")
    git(work, "branch", "release/1.1")
    git(work, "remote", "add", "origin", remote.as_uri())
    git(work, "push", "-q", "origin", "main", "release/1.0", "release/1.1")
    return Repo(remote=remote, work=work)


def merge_pull_request(repo: Repo, *, squash: bool = False) -> str:
    """Merge a fix into release/1.0 and return the merge (or squash) commit."""
    work = repo.work
    git(work, "checkout", "-q", "release/1.0")
    git(work, "checkout", "-q", "-b", "fix-bug")
    _commit(work, "app.txt", "line one\nline two fixed\nline three\n", "Fix bug")
    git(work, "checkout", "-q", "release/1.0")
    if squash:
        git(work, "merge", "-q", "--squash", "fix-bug")
        git(work, "commit", "-q", "-m", "Fix bug (#812)")
    else:
        git(work, "merge", "-q", "--no-ff", "-m", "Merge pull request #812", "fix-bug")
    git(work, "push", "-q", "origin", "release/1.0")
    return git(work, "rev-parse", "HEAD")


def diverge(repo: Repo, branch: str) -> None:
    """Change the same line on ``branch`` so the backport conflicts there."""
    work = repo.work
    git(work, "checkout", "-q", branch)
    _commit(work, "app.txt", "line one\nline two reworded\nline three\n", "Rework")
    git(work, "push", "-q", "origin", branch)


def change_for(repo: Repo, sha: str) -> MergedChange:
    return MergedChange(
        host="github",
        number=812,
        url="https://github.example/acme/widgets/pull/812",
        title="Fix the bug",
        description="Fixes a crash on startup.",
        base_branch="release/1.0",
        merge_commit_sha=sha,
        repository_url=repo.url,
    )


@dataclass
class FakeHost:
    """In-memory code host. It can open and update, and has no merge at all."""

    kind: str = "github"
    git_auth_username: str = "x-access-token"
    changes: Dict[Tuple[str, str], HostChange] = field(default_factory=dict)
    opened: List[dict] = field(default_factory=list)
    updated: List[dict] = field(default_factory=list)
    reviews: List[Tuple[int, List[str]]] = field(default_factory=list)
    comments: List[Tuple[int, str]] = field(default_factory=list)
    fail_reviews: bool = False
    fail_comment: bool = False
    next_number: int = 900

    async def find_change(self, branch: str, target: str) -> Optional[HostChange]:
        return self.changes.get((branch, target))

    async def open_change(
        self, branch: str, target: str, title: str, body: str
    ) -> HostChange:
        self.next_number += 1
        change = HostChange(
            number=self.next_number,
            url=f"https://github.example/acme/widgets/pull/{self.next_number}",
        )
        self.changes[(branch, target)] = change
        self.opened.append(
            {"branch": branch, "target": target, "title": title, "body": body}
        )
        return change

    async def update_change(self, number: int, title: str, body: str) -> HostChange:
        self.updated.append({"number": number, "title": title, "body": body})
        return HostChange(number=number, url="")

    async def request_reviewers(self, number: int, reviewers: List[str]) -> None:
        if self.fail_reviews:
            raise BackportHostError("Requesting reviewers failed with HTTP 422")
        self.reviews.append((number, list(reviewers)))

    async def comment_on_original(self, number: int, body: str) -> None:
        if self.fail_comment:
            raise BackportHostError("Commenting failed with HTTP 403")
        self.comments.append((number, body))


PLAN = BackportPlan(
    source_branch="release/1.0",
    target_branches=("release/1.1", "main"),
    reviewers=("maintainer-a", "maintainer-b"),
)


async def backport(repo: Repo, host: FakeHost, sha: str, plan: BackportPlan = PLAN):
    return await run_backport(
        plan,
        change_for(repo, sha),
        host,
        token=None,
        committer_name="Preloop",
        committer_email="bot@example.com",
        allow_file_protocol=True,
    )


@pytest.mark.asyncio
async def test_clean_backport_opens_one_pull_request_per_target_in_order(
    repo: Repo,
) -> None:
    sha = merge_pull_request(repo)
    host = FakeHost()

    report = await backport(repo, host, sha)

    assert [t.status for t in report.targets] == [STATUS_OPENED, STATUS_OPENED]
    assert [o["target"] for o in host.opened] == ["release/1.1", "main"]
    assert [o["branch"] for o in host.opened] == [
        "backport/pr-812-to-release-1.1",
        "backport/pr-812-to-main",
    ]
    for opened in host.opened:
        assert opened["title"] == f"Fix the bug (backport to {opened['target']})"
        assert opened["body"].startswith("Fixes a crash on startup.")
        assert (
            "Backport of https://github.example/acme/widgets/pull/812" in opened["body"]
        )
        assert sha in opened["body"]
        pushed = repo.remote_file(opened["branch"], "app.txt")
        assert "line two fixed" in pushed
        # The branch is cut from the target tip: one new commit on top.
        parent = git(repo.remote, "rev-parse", f"refs/heads/{opened['branch']}^")
        assert parent == repo.remote_sha(opened["target"])
    # The original author is kept; only the committer is Preloop.
    log = git(
        repo.remote,
        "log",
        "-1",
        "--format=%an|%ce|%B",
        "refs/heads/backport/pr-812-to-main",
    )
    assert log.startswith("Original Author|bot@example.com|")
    assert f"(cherry picked from commit {sha})" in log
    assert host.reviews == [
        (901, ["maintainer-a", "maintainer-b"]),
        (902, ["maintainer-a", "maintainer-b"]),
    ]
    assert report.succeeded
    assert agent_result_for(report)["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_conflict_pushes_nothing_and_the_next_target_still_opens(
    repo: Repo,
) -> None:
    sha = merge_pull_request(repo)
    diverge(repo, "release/1.1")
    host = FakeHost()

    report = await backport(repo, host, sha)

    first, second = report.targets
    assert first.status == STATUS_CONFLICT
    assert first.conflicting_files == ["app.txt"]
    assert first.pull_request_url is None
    assert repo.remote_sha("backport/pr-812-to-release-1.1") is None
    assert second.status == STATUS_OPENED
    assert [o["target"] for o in host.opened] == ["main"]
    assert not report.succeeded

    result = agent_result_for(report)
    assert result["status"] == "FAILED"
    assert "app.txt" in result["error_message"]
    stored = result["result"][BACKPORT_RESULT_KEY]
    assert [t["status"] for t in stored["targets"]] == ["conflict", "opened"]
    assert stored["targets"][0]["conflicting_files"] == ["app.txt"]

    ((number, body),) = host.comments
    assert number == 812
    assert body.startswith(SUMMARY_COMMENT_MARKER)
    assert "`app.txt`" in body and "Nothing was pushed" in body


@pytest.mark.asyncio
async def test_redelivery_updates_the_existing_pull_request_without_duplicates(
    repo: Repo,
) -> None:
    sha = merge_pull_request(repo)
    host = FakeHost()
    await backport(repo, host, sha)
    tips = {
        branch: repo.remote_sha(branch)
        for branch in ("backport/pr-812-to-release-1.1", "backport/pr-812-to-main")
    }

    report = await backport(repo, host, sha)

    assert [t.status for t in report.targets] == [STATUS_UPDATED, STATUS_UPDATED]
    assert len(host.opened) == 2
    assert [u["number"] for u in host.updated] == [901, 902]
    assert [t.pull_request_number for t in report.targets] == [901, 902]
    # Nothing is pushed again and reviewers are not re-requested.
    assert {b: repo.remote_sha(b) for b in tips} == tips
    assert len(host.reviews) == 2


@pytest.mark.asyncio
async def test_retry_after_push_opens_the_pull_request_without_repushing(
    repo: Repo,
) -> None:
    sha = merge_pull_request(repo)
    host = FakeHost()
    await backport(repo, host, sha)
    tip = repo.remote_sha("backport/pr-812-to-main")
    # The earlier run pushed the branches but never got to the host.
    host = FakeHost()

    report = await backport(repo, host, sha)

    assert [t.status for t in report.targets] == [STATUS_OPENED, STATUS_OPENED]
    assert repo.remote_sha("backport/pr-812-to-main") == tip


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["closed", "merged"])
async def test_closed_or_merged_backport_is_left_alone(repo: Repo, state: str) -> None:
    sha = merge_pull_request(repo)
    host = FakeHost()
    host.changes[("backport/pr-812-to-release-1.1", "release/1.1")] = HostChange(
        number=700, url="https://github.example/acme/widgets/pull/700", state=state
    )

    report = await backport(repo, host, sha)

    assert report.targets[0].status == STATUS_EXISTS
    assert report.targets[0].pull_request_number == 700
    assert repo.remote_sha("backport/pr-812-to-release-1.1") is None
    assert report.targets[1].status == STATUS_OPENED
    assert report.succeeded


@pytest.mark.asyncio
async def test_change_already_on_target_is_reported_and_nothing_pushed(
    repo: Repo,
) -> None:
    sha = merge_pull_request(repo)
    work = repo.work
    git(work, "checkout", "-q", "main")
    _commit(work, "app.txt", "line one\nline two fixed\nline three\n", "Same fix")
    git(work, "push", "-q", "origin", "main")
    host = FakeHost()

    report = await backport(repo, host, sha)

    assert report.targets[1].status == STATUS_ALREADY_APPLIED
    assert repo.remote_sha("backport/pr-812-to-main") is None
    assert [o["target"] for o in host.opened] == ["release/1.1"]
    assert report.succeeded


@pytest.mark.asyncio
async def test_failed_review_request_is_recorded_and_the_pull_request_stays(
    repo: Repo,
) -> None:
    sha = merge_pull_request(repo)
    host = FakeHost(fail_reviews=True)

    report = await backport(repo, host, sha)

    assert [t.status for t in report.targets] == [STATUS_OPENED, STATUS_OPENED]
    for target in report.targets:
        assert target.pull_request_url
        assert target.review_request_error == (
            "Requesting reviewers failed with HTTP 422"
        )
    assert report.succeeded
    assert "review request failed" in host.comments[0][1]


@pytest.mark.asyncio
async def test_missing_target_fails_only_that_target(repo: Repo) -> None:
    sha = merge_pull_request(repo)
    host = FakeHost()
    plan = BackportPlan(
        source_branch="release/1.0", target_branches=("release/9.9", "main")
    )

    report = await backport(repo, host, sha, plan)

    assert report.targets[0].status == STATUS_FAILED
    assert "release/9.9 does not exist" in (report.targets[0].detail or "")
    assert report.targets[1].status == STATUS_OPENED
    assert agent_result_for(report)["failure_category"] == "tool_error"


@pytest.mark.asyncio
async def test_never_merges_into_any_target(repo: Repo) -> None:
    sha = merge_pull_request(repo)
    before = {b: repo.remote_sha(b) for b in ("release/1.1", "main", "release/1.0")}

    await backport(repo, FakeHost(), sha)

    after = {b: repo.remote_sha(b) for b in before}
    assert after == before


@pytest.mark.asyncio
async def test_squash_merged_single_parent_commit_is_picked(repo: Repo) -> None:
    sha = merge_pull_request(repo, squash=True)
    host = FakeHost()

    report = await backport(repo, host, sha)

    assert [t.status for t in report.targets] == [STATUS_OPENED, STATUS_OPENED]
    assert "line two fixed" in repo.remote_file("backport/pr-812-to-main", "app.txt")


@pytest.mark.asyncio
async def test_unknown_merge_commit_fails_every_target_with_a_clear_reason(
    repo: Repo,
) -> None:
    merge_pull_request(repo)
    host = FakeHost()

    report = await backport(repo, host, "0" * 40)

    assert {t.status for t in report.targets} == {STATUS_FAILED}
    assert "could not be fetched" in (report.targets[0].detail or "")
    assert host.opened == []
    assert len(host.comments) == 1


@pytest.mark.asyncio
async def test_failed_summary_comment_is_recorded(repo: Repo) -> None:
    sha = merge_pull_request(repo)
    host = FakeHost(fail_comment=True)

    report = await backport(repo, host, sha)

    assert report.comment_status == "failed"
    assert report.comment_error == "Commenting failed with HTTP 403"
    assert report.succeeded


@pytest.mark.asyncio
async def test_summary_comment_can_be_turned_off(repo: Repo) -> None:
    sha = merge_pull_request(repo)
    host = FakeHost()
    plan = BackportPlan(
        source_branch="release/1.0",
        target_branches=("main",),
        comment_on_original=False,
    )

    report = await backport(repo, host, sha, plan)

    assert host.comments == []
    assert report.comment_status == "skipped"


def test_push_never_overwrites_a_branch_that_appeared_meanwhile(
    repo: Repo, tmp_path: Path
) -> None:
    from preloop.services.backport_git import BackportGitError, BackportWorkspace

    sha = merge_pull_request(repo)
    # Someone pushes the backport branch by hand after the runner checked.
    git(repo.work, "push", "-q", "origin", "main:refs/heads/backport/pr-812-to-main")
    manual_tip = repo.remote_sha("backport/pr-812-to-main")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    workspace = BackportWorkspace(
        scratch,
        repository_url=repo.url,
        token=None,
        auth_username="x-access-token",
        committer_name="Preloop",
        committer_email="bot@example.com",
        allow_file_protocol=True,
    )
    workspace.init()
    workspace.fetch_commit(sha, fallback_branch="release/1.0")
    base = workspace.fetch_branch("main")
    outcome = workspace.cherry_pick(
        base_ref=base, branch="backport/pr-812-to-main", sha=sha, mainline=1
    )
    assert outcome.status == "applied"

    with pytest.raises(BackportGitError, match="push failed"):
        workspace.push_new_branch("backport/pr-812-to-main")
    assert repo.remote_sha("backport/pr-812-to-main") == manual_tip


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/widgets.git",
        "https://user:secret@github.com/acme/widgets.git",
        "https://github.com/acme/widgets.git?x=1",
        "file:///tmp/remote.git",
        "ext::sh -c touch% /tmp/pwned",
    ],
)
def test_only_credential_free_https_remotes_are_accepted(url: str) -> None:
    from preloop.services.backport_git import BackportGitError, validate_repository_url

    with pytest.raises(BackportGitError):
        validate_repository_url(url)


def test_token_is_sent_as_a_scoped_header_never_in_the_url(tmp_path: Path) -> None:
    from preloop.services.backport_git import BackportWorkspace

    workspace = BackportWorkspace(
        tmp_path,
        repository_url="https://github.com/acme/widgets.git",
        token="s3cr3t",
        auth_username="x-access-token",
        committer_name="Preloop",
        committer_email="bot@example.com",
    )
    env = workspace._environment
    assert workspace.repository_url == "https://github.com/acme/widgets.git"
    assert env["GIT_CONFIG_KEY_0"] == (
        "http.https://github.com/acme/widgets.git.extraHeader"
    )
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "s3cr3t" not in env["GIT_CONFIG_VALUE_0"]
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_git_errors_never_carry_git_output(tmp_path: Path) -> None:
    """Git's stderr can echo the remote and auth failures; it never leaks."""
    from preloop.services.backport_git import BackportGitError, BackportWorkspace

    missing = tmp_path / "no-such-remote.git"
    workspace = BackportWorkspace(
        tmp_path / "scratch",
        repository_url=missing.as_uri(),
        token="s3cr3t",
        auth_username="x-access-token",
        committer_name="Preloop",
        committer_email="bot@example.com",
        allow_file_protocol=True,
    )
    (tmp_path / "scratch").mkdir()
    workspace.init()
    with pytest.raises(BackportGitError) as caught:
        workspace.remote_branch_sha("main")
    message = str(caught.value)
    assert message == "Git ls-remote failed"
    assert "no-such-remote" not in message
    assert "s3cr3t" not in message


def hanging_git(tmp_path: Path) -> Tuple[Path, Path]:
    """A stand-in Git binary that starts a helper, records both pids, hangs.

    The pid file holds ``<git pid> <helper pid>`` once both are running, like
    a real ``git fetch`` waiting on its ``git remote-https`` helper.
    """
    pid_file = tmp_path / "git.pid"
    script = tmp_path / "hanging-git"
    script.write_text(
        "#!/bin/sh\n"
        "sleep 60 &\n"
        f"echo $$ $! > {pid_file}.tmp && mv {pid_file}.tmp {pid_file}\n"
        "wait\n"
    )
    script.chmod(0o755)
    return script, pid_file


def recorded_pids(pid_file: Path) -> List[int]:
    return [int(pid) for pid in pid_file.read_text().split()]


def wait_for(predicate: Callable[[], bool], seconds: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.02)


def process_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    # A killed child that was already reaped by communicate() is gone; one
    # still listed here must at least be a zombie, never a running process.
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return status == "" or status.startswith("Z")


def test_closing_the_workspace_kills_a_running_git_and_refuses_new_ones(
    tmp_path: Path,
) -> None:
    import threading

    from preloop.services.backport_git import BackportGitError, BackportWorkspace

    script, pid_file = hanging_git(tmp_path)
    (tmp_path / "scratch").mkdir()
    workspace = BackportWorkspace(
        tmp_path / "scratch",
        repository_url="https://github.example/acme/widgets.git",
        token=None,
        auth_username="x-access-token",
        committer_name="Preloop",
        committer_email="bot@example.com",
    )
    workspace._git = str(script)
    errors: List[BackportGitError] = []

    def run() -> None:
        try:
            workspace.init()
        except BackportGitError as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    wait_for(pid_file.exists)
    workspace.close()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert [str(e) for e in errors] == ["Backport workspace was closed"]
    for pid in recorded_pids(pid_file):
        wait_for(lambda pid=pid: process_is_gone(pid))
    with pytest.raises(BackportGitError, match="closed"):
        workspace.init()


@pytest.mark.asyncio
async def test_cancelled_run_stops_git_and_surfaces_the_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flow's timeout budget cancels the run mid-Git; cleanup never masks it."""
    import asyncio

    script, pid_file = hanging_git(tmp_path)
    monkeypatch.setattr(
        "preloop.services.backport_git._git_binary", lambda: str(script)
    )
    change = MergedChange(
        host="github",
        number=812,
        url="https://github.example/acme/widgets/pull/812",
        title="Fix the widget",
        description="",
        base_branch="release/1.0",
        merge_commit_sha="a" * 40,
        repository_url="https://github.example/acme/widgets.git",
    )
    host = FakeHost()

    async def started() -> None:
        while not pid_file.exists():
            await asyncio.sleep(0.02)

    task = asyncio.create_task(
        run_backport(
            PLAN,
            change,
            host,
            token="s3cr3t",
            committer_name="Preloop",
            committer_email="bot@example.com",
        )
    )
    await asyncio.wait_for(started(), timeout=10)
    task.cancel()
    outcome = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(outcome[0], asyncio.CancelledError)

    for pid in recorded_pids(pid_file):
        wait_for(lambda pid=pid: process_is_gone(pid))
    assert host.comments == []
