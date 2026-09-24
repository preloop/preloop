"""Local integration tests for report publication (issue #648).

These run the *actual* post-execution shell the container emits, against a
real git repository whose origin is a real bare repository with a protected
default branch, and a fake provider that keeps pull request state between
runs. No docker, no database: publication is shell, git and curl.

What the acceptance criteria need is all here rather than in assertions about
strings: how many pull requests exist after two runs, how many files the
commit changed, what happens to a byte identical report, and what the run
records when the push is refused.
"""

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys

import pytest

from preloop.agents.container import ContainerAgentExecutor
from preloop.services.report_publication import (
    REPORT_PUBLICATION_MARKER,
    parse_report_publication_marker,
)

OWNER = "example"
REPO = "widgets"
REPORT_BRANCH = "preloop/report/portfolio"
DOCUMENT = "PORTFOLIO.md"

# Rejects every ref except the platform's own report branches, which is what a
# protected default branch looks like from the pushing side.
PROTECTED_DEFAULT_BRANCH_HOOK = """#!/bin/sh
while read -r _old _new ref; do
  case "$ref" in
    refs/heads/preloop/report/*) ;;
    *)
      echo "remote: refusing update to protected ref $ref" >&2
      exit 1
      ;;
  esac
done
exit 0
"""

REFUSE_EVERYTHING_HOOK = """#!/bin/sh
echo "remote: this repository refuses updates" >&2
exit 1
"""

FAKE_CURL = r'''#!{python}
"""Stand-in for the provider REST API, with state that survives a run."""
import json
import pathlib
import sys

STORE = pathlib.Path({store!r})
CALLS = pathlib.Path({calls!r})
MODE = {mode!r}

args = sys.argv[1:]
method = args[args.index("-X") + 1] if "-X" in args else "GET"
url = next((arg for arg in args if arg.startswith("http")), "")
wants_code = "-w" in args
with CALLS.open("a") as stream:
    stream.write(f"{{method}} {{url}}\n")

if MODE == "unreachable":
    print("000", end="")
    sys.exit(7)

output = args[args.index("-o") + 1] if "-o" in args else "/dev/null"
pulls = json.loads(STORE.read_text()) if STORE.exists() else []


def payload():
    reference = next((a for a in args if a.startswith("@")), None)
    if reference is None:
        return {{}}
    return json.loads(pathlib.Path(reference[1:]).read_text())


def write(body, code):
    if output != "/dev/null":
        pathlib.Path(output).write_text(json.dumps(body))
    if wants_code:
        print(code, end="")


if method == "POST":
    body = payload()
    head = body.get("head", "")
    open_for_head = [p for p in pulls if p["head"]["ref"] == head and p["state"] == "open"]
    if open_for_head:
        write({{"message": f"A pull request already exists for {{head}}."}}, "422")
    else:
        number = len(pulls) + 1
        pull = {{
            "number": number,
            "state": "open",
            "title": body.get("title", ""),
            "body": body.get("body", ""),
            "head": {{"ref": head}},
            "base": {{"ref": body.get("base", "")}},
            "html_url": f"https://github.com/{owner}/{repo}/pull/{{number}}",
        }}
        pulls.append(pull)
        STORE.write_text(json.dumps(pulls))
        write(pull, "201")
elif method == "GET":
    head = url.split("head=")[-1].split("&")[0] if "head=" in url else ""
    branch = head.split(":")[-1]
    write([p for p in pulls if p["head"]["ref"] == branch and p["state"] == "open"], "200")
else:  # PATCH / PUT: the failure-disclosure refresh
    if MODE == "reject_update":
        write({{"message": "update rejected"}}, "422")
    else:
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        body = payload()
        for pull in pulls:
            if pull["number"] == number:
                pull.update(body)
        STORE.write_text(json.dumps(pulls))
        write({{"number": number}}, "200")
'''


class PublishingRepo:
    """A checkout, its bare origin, a workspace and a fake provider."""

    def __init__(self, tmp_path: pathlib.Path, *, protected: bool = True):
        home = tmp_path / "home"
        home.mkdir()
        self.root = tmp_path
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
            "GIT_CONFIG_SYSTEM": str(home / "system-gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TERMINAL_PROMPT": "0",
            "PRELOOP_DISABLE_TELEMETRY": "true",
        }
        self.origin = tmp_path / "origin.git"
        self.repo = tmp_path / "repo"
        self.workspace = tmp_path / "workspace"
        self.worktree = tmp_path / "publication-worktree"
        (self.workspace / "evidence").mkdir(parents=True)
        self.calls = tmp_path / "provider-calls"
        self.git_calls = tmp_path / "git-calls"
        self.store = tmp_path / "pull-requests.json"

        seed = tmp_path / "seed"
        seed.mkdir()
        self._git("init", "-b", "main", cwd=seed)
        self._git("config", "user.email", "seed@example.com", cwd=seed)
        self._git("config", "user.name", "Seed", cwd=seed)
        (seed / "README.md").write_text("portfolio of projects\n")
        self._git("add", ".", cwd=seed)
        self._git("commit", "-m", "base commit", cwd=seed)
        self._git("clone", "--bare", str(seed), str(self.origin), cwd=tmp_path)
        self._git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.origin)
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text(
            PROTECTED_DEFAULT_BRANCH_HOOK if protected else REFUSE_EVERYTHING_HOOK
        )
        hook.chmod(0o755)
        self._git("clone", str(self.origin), str(self.repo), cwd=tmp_path)
        self._bin = tmp_path / "bin"
        self._bin.mkdir()
        self._install_git_wrapper()
        self.write_report("first report\n")

    # --- helpers -----------------------------------------------------
    def _git(self, *args: str, cwd: pathlib.Path) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def _install_git_wrapper(self) -> None:
        real_git = shutil.which("git")
        assert real_git is not None
        (self._bin / "git").write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> {shlex.quote(str(self.git_calls))}\n'
            f'exec {shlex.quote(real_git)} "$@"\n'
        )
        (self._bin / "git").chmod(0o755)

    def _install_curl(self, mode: str) -> None:
        script = FAKE_CURL.format(
            python=sys.executable,
            store=str(self.store),
            calls=str(self.calls),
            mode=mode,
            owner=OWNER,
            repo=REPO,
        )
        (self._bin / "curl").write_text(script)
        (self._bin / "curl").chmod(0o755)

    def write_report(self, text: str) -> None:
        (self.workspace / "evidence" / "portfolio-report.md").write_text(text)

    def remove_report(self) -> None:
        (self.workspace / "evidence" / "portfolio-report.md").unlink()

    def origin_show(self, ref: str, path: str) -> str:
        return self._git("show", f"{ref}:{path}", cwd=self.origin)

    def origin_commits(self, ref: str) -> int:
        return int(self._git("rev-list", "--count", ref, cwd=self.origin))

    def origin_has(self, ref: str) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=self.origin,
            env=self.env,
            capture_output=True,
        )
        return result.returncode == 0

    def files_changed_by_tip(self, ref: str) -> list[str]:
        return [
            line
            for line in self._git(
                "show", "--pretty=format:", "--name-only", ref, cwd=self.origin
            ).splitlines()
            if line
        ]

    def pull_requests(self) -> list[dict]:
        return json.loads(self.store.read_text()) if self.store.exists() else []

    def provider_calls(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [line for line in self.calls.read_text().splitlines() if line]

    def git_commands(self) -> list[str]:
        if not self.git_calls.exists():
            return []
        return [line for line in self.git_calls.read_text().splitlines() if line]

    # --- the run under test ------------------------------------------
    def context(self, **overrides) -> dict:
        report_publication = {
            "enabled": True,
            "source_path": "evidence/portfolio-report.md",
            "destination_path": DOCUMENT,
            "commit_message": "Update the portfolio review report",
        }
        report_publication.update(overrides.pop("report_publication", {}))
        config = {
            "enabled": True,
            "create_pull_request": True,
            "source_branch": "main",
            "git_user_name": "Preloop",
            "git_user_email": "hello@preloop.ai",
            "repositories": [
                {
                    "repository_url": f"https://github.com/{OWNER}/{REPO}.git",
                    "clone_path": str(self.repo),
                    "tracker_id": "tracker-1",
                }
            ],
            "report_publication": report_publication,
        }
        config.update(overrides.pop("git_clone_config", {}))
        context = {
            "execution_id": "0d4d7f26-1f4f-4d33-9a52-0a1e4e6a1a10",
            "flow_name": "Portfolio Review",
            "trigger_event_data": {},
            "git_clone_config": config,
            "_git_source_branch": "main",
            "git_credentials_map": {
                "tracker-1": {"token": "fake-token", "tracker_type": "github"}
            },
        }
        context.update(overrides)
        return context

    def run(
        self, *, provider: str = "live", **overrides
    ) -> subprocess.CompletedProcess:
        self._install_curl(provider)
        # Observations are per run; pull request state is what persists.
        self.calls.unlink(missing_ok=True)
        self.git_calls.unlink(missing_ok=True)
        executor = ContainerAgentExecutor("codex", {}, "test-image")
        context = self.context(**overrides)
        commands = executor._prepare_git_post_execution_commands(context)
        assert commands
        # Relocate container-only absolute paths; the logic runs unchanged.
        commands = (
            commands.replace("/tmp/preloop-report-publication", str(self.worktree))
            .replace("/workspace", str(self.workspace))
            .replace("/tmp/.preloop-git-credentials", str(self.root / "git-creds"))
        )
        env = {**self.env, **executor._git_credential_env(context)}
        env["PATH"] = str(self._bin) + os.pathsep + env["PATH"]
        (self.root / "post-execution.sh").write_text(commands)
        return subprocess.run(
            ["bash", "-c", commands],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def outcome(self, result: subprocess.CompletedProcess) -> dict:
        markers = [
            line
            for line in result.stdout.splitlines()
            if line.strip().startswith(REPORT_PUBLICATION_MARKER + " ")
        ]
        assert len(markers) == 1, result.stdout + result.stderr
        parsed = parse_report_publication_marker(markers[0])
        assert parsed is not None, markers[0]
        return parsed


@pytest.fixture()
def repo(tmp_path: pathlib.Path) -> PublishingRepo:
    return PublishingRepo(tmp_path)


class TestReportLandsAsAPullRequest:
    def test_one_pull_request_with_exactly_one_changed_file(
        self, repo: PublishingRepo
    ) -> None:
        result = repo.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert repo.outcome(result) == {
            "outcome": "published",
            "reason": "",
            "branch": REPORT_BRANCH,
            "document": DOCUMENT,
            "log": "evidence/report-publication.log",
        }
        assert len(repo.pull_requests()) == 1
        pull = repo.pull_requests()[0]
        assert pull["head"]["ref"] == REPORT_BRANCH
        assert pull["base"]["ref"] == "main"
        assert repo.files_changed_by_tip(REPORT_BRANCH) == [DOCUMENT]
        assert repo.origin_show(REPORT_BRANCH, DOCUMENT) == "first report"
        assert "PRELOOP_PR_OPENED" in result.stdout

    def test_a_dirty_untrusted_checkout_contributes_nothing(
        self, repo: PublishingRepo
    ) -> None:
        """The agent read many untrusted projects into this checkout. Only the
        document may reach the commit, whatever else the tree holds."""
        (repo.repo / "README.md").write_text("tampered by a reviewed project\n")
        (repo.repo / "leftover.bin").write_text("scratch\n")
        (repo.repo / "node_modules").mkdir()
        (repo.repo / "node_modules" / "index.js").write_text("junk\n")

        result = repo.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert repo.outcome(result)["outcome"] == "published"
        assert repo.files_changed_by_tip(REPORT_BRANCH) == [DOCUMENT]
        assert repo.origin_show(REPORT_BRANCH, "README.md") == "portfolio of projects"

    def test_rerun_with_a_changed_report_updates_the_same_pull_request(
        self, repo: PublishingRepo
    ) -> None:
        first = repo.run()
        assert repo.outcome(first)["outcome"] == "published"

        repo.write_report("second report\n")
        second = repo.run()

        assert second.returncode == 0, second.stdout + second.stderr
        assert repo.outcome(second)["outcome"] == "published"
        # The acceptance assertion: the pull request count did not grow.
        assert len(repo.pull_requests()) == 1
        assert repo.pull_requests()[0]["number"] == 1
        assert repo.origin_show(REPORT_BRANCH, DOCUMENT) == "second report"
        assert repo.origin_commits(REPORT_BRANCH) == repo.origin_commits("main") + 2
        assert repo.files_changed_by_tip(REPORT_BRANCH) == [DOCUMENT]
        assert "PRELOOP_PR_OPENED" in second.stdout

    def test_rerun_with_an_identical_report_publishes_nothing(
        self, repo: PublishingRepo
    ) -> None:
        repo.run()
        tip = repo._git("rev-parse", REPORT_BRANCH, cwd=repo.origin)

        second = repo.run()

        assert second.returncode == 0, second.stdout + second.stderr
        assert repo.outcome(second) == {
            "outcome": "unchanged",
            "reason": "identical_document",
            "branch": REPORT_BRANCH,
            "document": DOCUMENT,
            "log": "evidence/report-publication.log",
        }
        assert repo._git("rev-parse", REPORT_BRANCH, cwd=repo.origin) == tip
        assert len(repo.pull_requests()) == 1
        # Nothing was opened and nothing was updated: no provider call at all.
        assert repo.provider_calls() == []
        assert not [line for line in repo.git_commands() if line.startswith("push")]

    def test_the_document_may_live_in_a_subdirectory(
        self, repo: PublishingRepo
    ) -> None:
        result = repo.run(
            report_publication={"destination_path": "docs/portfolio/report.md"}
        )

        assert result.returncode == 0, result.stdout + result.stderr
        outcome = repo.outcome(result)
        assert outcome["outcome"] == "published"
        assert outcome["branch"] == "preloop/report/docs-portfolio-report"
        assert repo.files_changed_by_tip(outcome["branch"]) == [
            "docs/portfolio/report.md"
        ]


class TestProtectedDefaultBranch:
    def test_no_direct_commit_is_attempted_on_the_default_branch(
        self, repo: PublishingRepo
    ) -> None:
        main_before = repo._git("rev-parse", "main", cwd=repo.origin)

        result = repo.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert repo.outcome(result)["outcome"] == "published"
        # The protected branch is untouched, and was never a push target: the
        # hook that would have refused it never had to fire.
        assert repo._git("rev-parse", "main", cwd=repo.origin) == main_before
        pushes = [line for line in repo.git_commands() if line.startswith("push")]
        assert pushes == [f"push origin HEAD:refs/heads/{REPORT_BRANCH}"]
        assert "refusing update to protected ref" not in result.stdout + result.stderr
        assert repo.pull_requests()[0]["base"]["ref"] == "main"

    def test_the_first_run_starts_the_branch_from_the_protected_default(
        self, repo: PublishingRepo
    ) -> None:
        result = repo.run()

        assert repo.outcome(result)["outcome"] == "published"
        merge_base = repo._git("merge-base", "main", REPORT_BRANCH, cwd=repo.origin)
        assert merge_base == repo._git("rev-parse", "main", cwd=repo.origin)

    def test_a_branch_override_of_main_is_refused(self, repo: PublishingRepo) -> None:
        main_before = repo._git("rev-parse", "main", cwd=repo.origin)

        result = repo.run(report_publication={"branch": "main"})

        assert result.returncode == 0, result.stdout + result.stderr
        assert repo.outcome(result)["reason"] == "invalid_configuration"
        assert repo._git("rev-parse", "main", cwd=repo.origin) == main_before
        assert not repo.origin_has(REPORT_BRANCH)


class TestPublishFailureDegradesTheRun:
    def test_a_refused_push_is_recorded_and_the_artifact_survives(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo = PublishingRepo(tmp_path, protected=False)  # refuses every ref

        result = repo.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert repo.outcome(result) == {
            "outcome": "failed",
            "reason": "push_failed",
            "branch": REPORT_BRANCH,
            "document": DOCUMENT,
            "log": "evidence/report-publication.log",
        }
        assert not repo.origin_has(REPORT_BRANCH)
        assert repo.pull_requests() == []
        # The report is still the run's artifact, untouched by the failure.
        report = repo.workspace / "evidence" / "portfolio-report.md"
        assert report.read_text() == "first report\n"
        # And the reason why is readable next to it.
        log = repo.workspace / "evidence" / "report-publication.log"
        assert "refuses updates" in log.read_text()

    def test_a_missing_report_is_recorded_without_touching_the_repository(
        self, repo: PublishingRepo
    ) -> None:
        repo.remove_report()

        result = repo.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert repo.outcome(result)["reason"] == "report_missing"
        assert repo.outcome(result)["outcome"] == "failed"
        assert not repo.origin_has(REPORT_BRANCH)
        assert repo.provider_calls() == []

    def test_an_unreachable_provider_leaves_the_branch_and_records_the_reason(
        self, repo: PublishingRepo
    ) -> None:
        result = repo.run(provider="unreachable")

        assert result.returncode == 0, result.stdout + result.stderr
        assert repo.outcome(result)["outcome"] == "failed"
        assert repo.outcome(result)["reason"] == "pull_request_unavailable"
        # The document reached the repository; only the review surface is
        # missing, and the next run retries it on the same branch.
        assert repo.origin_show(REPORT_BRANCH, DOCUMENT) == "first report"
        assert repo.pull_requests() == []

    def test_a_failed_body_update_prints_one_marker_and_exits_zero(
        self, repo: PublishingRepo
    ) -> None:
        """A provenance failure must not kill the report wrapper.

        The commit is already on the branch. The wrapper still prints exactly
        one marker and exits zero so the run records the failed update.
        """
        assert repo.outcome(repo.run())["outcome"] == "published"
        repo.write_report("second report\n")

        result = repo.run(provider="reject_update")

        assert result.returncode == 0, result.stdout + result.stderr
        markers = [
            line
            for line in result.stdout.splitlines()
            if line.strip().startswith("PRELOOP_REPORT_")
        ]
        assert len(markers) == 1, result.stdout + result.stderr
        parsed = parse_report_publication_marker(markers[0])
        assert parsed is not None
        assert parsed["outcome"] == "failed"
        assert parsed["reason"] == "pull_request_unavailable"
        assert "PRELOOP_PR_OPENED" not in result.stdout
        assert repo.origin_show(REPORT_BRANCH, DOCUMENT) == "second report"

    def test_a_second_run_opens_the_pull_request_the_first_could_not(
        self, repo: PublishingRepo
    ) -> None:
        assert repo.outcome(repo.run(provider="unreachable"))["outcome"] == "failed"

        repo.write_report("second report\n")
        second = repo.run()

        assert repo.outcome(second)["outcome"] == "published"
        assert len(repo.pull_requests()) == 1
        assert repo.pull_requests()[0]["head"]["ref"] == REPORT_BRANCH


class TestWriteFlowConflict:
    def test_agent_commits_refuse_publication_and_keep_the_marker(
        self, repo: PublishingRepo
    ) -> None:
        repo._git("config", "user.email", "agent@example.com", cwd=repo.repo)
        repo._git("config", "user.name", "Agent", cwd=repo.repo)
        (repo.repo / "agent.txt").write_text("agent work\n")
        repo._git("add", ".", cwd=repo.repo)
        repo._git("commit", "-m", "agent change", cwd=repo.repo)

        result = repo.run()

        assert result.returncode == 0, result.stdout + result.stderr
        outcome = repo.outcome(result)
        assert outcome["outcome"] == "failed"
        assert outcome["reason"] == "write_flow_conflict"
        assert not repo.origin_has(REPORT_BRANCH)
        assert repo.pull_requests() == []
