"""Local integration tests for the publication gate (issue #428).

These run the *actual* shell the publisher emits — embedded selection module,
verifier script, profile file — inside a temporary git repository, with a
fake publication step guarded the same way ``container.py`` guards the push.
No docker, no database: the gate logic is shell + git + python3.

The flow exercised is the acceptance one: implement, verify (failing check
denies publication), repair, final verification, publication.
"""

import json
import pathlib
import subprocess

import pytest

from preloop.agents.verification import (
    VERIFICATION_DENIED_MARKER,
    VERIFICATION_MARKER,
    VERIFIER_EXIT_DENIED,
    build_verification_gate_shell,
)
from preloop.services.verification import (
    VERIFICATION_PRODUCER,
)

PROFILE = {
    "version": "v1",
    "profile_id": "integration",
    "description": "Gate integration profile",
    "always": [
        {
            "id": "lint",
            "command": "echo lint-ok",
            "reason": "inexpensive hook",
            "scope": "backend",
        }
    ],
    "rules": [
        {
            "id": "docs",
            "description": "Docs changes",
            "path_globs": ["docs/*", "*.md"],
            "commands": [
                {
                    "id": "docs-check",
                    "command": "echo docs-ok",
                    "reason": "docs links must resolve",
                    "scope": "docs",
                }
            ],
        },
        {
            "id": "migration",
            "description": "Migration changes",
            "path_globs": ["migrations/*"],
            "commands": [
                {
                    "id": "alembic-heads",
                    "command": "echo heads-ok",
                    "reason": "single alembic head",
                    "scope": "migration",
                }
            ],
        },
        {
            "id": "frontend",
            "description": "Frontend changes",
            "path_globs": ["frontend/*"],
            "commands": [
                {
                    "id": "frontend-suite",
                    "command": "echo frontend-ok",
                    "reason": "affected components",
                    "scope": "frontend",
                }
            ],
        },
    ],
    "unknown_default": [
        {
            "id": "fast-tests",
            "command": "echo fast-ok",
            "reason": "unknown impact uses the conservative default",
            "scope": "unknown",
        }
    ],
}


def _check_that_fails(evidence_dir: pathlib.Path) -> dict:
    return {
        "id": "fast-tests",
        "command": f"echo failing on purpose; exit 1 >> {evidence_dir / 'never.txt'}",
        "reason": "conservative default",
        "scope": "unknown",
    }


def _check_that_succeeds() -> dict:
    return {
        "id": "fast-tests",
        "command": "echo fast-ok",
        "reason": "conservative default",
        "scope": "unknown",
    }


class GateRepo:
    """Temporary repository + evidence dir with a fake publication step."""

    def __init__(self, tmp_path: pathlib.Path):
        self.repo = tmp_path / "repo"
        self.evidence = tmp_path / "evidence"
        self.repo.mkdir()
        self.evidence.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Preloop")
        self._git("config", "user.email", "hello@example.com")
        (self.repo / "README.md").write_text("base\n")
        self._git("add", ".")
        self._git("commit", "-m", "base commit")
        # The flow publishes a PR branch ahead of the base branch; commits
        # in these tests land on it so <base>..HEAD counts them.
        self._git("checkout", "-b", "preloop/issue-1")

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def commit(self, message: str = "work") -> str:
        self._git("add", ".")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD")

    def add_file(self, rel: str, content: str = "x\n") -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def gate_shell(self, profile: dict, base: str = "main") -> str:
        return build_verification_gate_shell(
            profile=profile,
            working_dir=str(self.repo),
            base_branch=base,
            evidence_dir=str(self.evidence),
            gate_budget_seconds=300,
        )

    def run_publisher(
        self, profile: dict, base: str = "main"
    ) -> subprocess.CompletedProcess:
        """Run the post-exec block shape: gate, then a guarded fake push."""

        marker = self.evidence / "published.marker"
        script = f"""
set -u
cd {self.repo}
COMMIT_COUNT=$(git rev-list --count {base}..HEAD 2>/dev/null || echo 0)
if [ "$COMMIT_COUNT" -gt "0" ]; then
{self.gate_shell(profile, base)}
  echo published > {marker}
  git rev-parse HEAD > {self.evidence / "pushed.sha"}
else
  echo "No commits, nothing to publish"
fi
"""
        return subprocess.run(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )

    def evidence_json(self) -> dict:
        path = self.evidence / "verification" / "evidence.json"
        return json.loads(path.read_text())

    def marker_lines(self, output: str, marker: str) -> list[str]:
        return [line for line in output.splitlines() if line.startswith(marker + " ")]


@pytest.fixture()
def repo(tmp_path: pathlib.Path) -> GateRepo:
    return GateRepo(tmp_path)


class TestGateIntegration:
    def test_valid_evidence_allows_publication(self, repo: GateRepo):
        repo.add_file("feature.py")
        sha = repo.commit("add feature")
        result = repo.run_publisher(PROFILE)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (repo.evidence / "published.marker").exists()

        evidence = repo.evidence_json()
        assert evidence["producer"] == VERIFICATION_PRODUCER
        assert evidence["commit_sha"] == sha
        assert evidence["status"] == "passed"
        assert evidence["clean_tree"] is True
        assert evidence["profile_id"] == "integration"
        assert evidence["profile_version"] == "v1"
        check_ids = {c["id"] for c in evidence["checks"]}
        assert check_ids == {"lint", "fast-tests"}
        assert all(c["exit_code"] == 0 for c in evidence["checks"])
        # Environment digest is recorded with the evidence.
        assert evidence["environment"]["telemetry_disabled"] is True
        # The orchestrator marker is a single line with compact JSON.
        markers = repo.marker_lines(result.stdout, VERIFICATION_MARKER)
        assert len(markers) == 1
        payload = json.loads(markers[0].split(" ", 1)[1])
        assert payload["allowed"] is True
        assert payload["commit_sha"] == sha

    def test_publisher_shell_embeds_evaluate_from_raw(self, repo: GateRepo):
        """ALLOW/DENY is the same fail-closed decision as evaluate_publication."""
        shell = repo.gate_shell(PROFILE)
        assert "evaluate_from_raw" in shell
        assert "select_from_raw" in shell

    def test_failing_required_check_blocks_publication(self, repo: GateRepo):
        repo.add_file("feature.py")
        repo.commit("add feature")
        profile = dict(PROFILE)
        profile["unknown_default"] = [_check_that_fails(repo.evidence)]
        result = repo.run_publisher(profile)
        assert result.returncode == VERIFIER_EXIT_DENIED
        # The fake publisher never ran.
        assert not (repo.evidence / "published.marker").exists()
        # The denial marker is visible for the failure classifier.
        assert repo.marker_lines(result.stdout, VERIFICATION_DENIED_MARKER)

    def test_missing_base_branch_falls_back_to_unknown_default(self, repo: GateRepo):
        """The verifier itself falls back to the conservative default when
        the base ref is unusable (tested directly: the publisher's
        commit-count check would never reach the gate without a base)."""

        repo.add_file("feature.py")
        repo.commit("add feature")
        script = f"cd {repo.repo}\n{repo.gate_shell(PROFILE, base='no-such-branch')}\n"
        result = subprocess.run(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout
        evidence = repo.evidence_json()
        assert evidence["used_unknown_default"] is True
        assert evidence["base_ref"] == "no-such-branch"
        ids = {c["id"] for c in evidence["checks"]}
        assert "fast-tests" in ids

    def test_crash_paths_fail_closed(self, repo: GateRepo):
        """A broken verifier must deny, never publish: no verdict file means
        no push, and the refusal marker is visible in the log stream."""

        repo.add_file("feature.py")
        repo.commit("add feature")
        shell = repo.gate_shell(PROFILE)
        # Sabotage the interpreter invocation so the verifier never runs and
        # never writes a verdict.
        sabotaged = shell.replace("\npython3 ", "\npython3_missing ", 1)
        assert sabotaged != shell
        script = f"""
cd {repo.repo}
COMMIT_COUNT=$(git rev-list --count main..HEAD)
if [ "$COMMIT_COUNT" -gt "0" ]; then
{sabotaged}
  echo published > {repo.evidence / "published.marker"}
fi
"""
        result = subprocess.run(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        assert result.returncode == VERIFIER_EXIT_DENIED
        assert not (repo.evidence / "published.marker").exists()
        assert repo.marker_lines(result.stdout, VERIFICATION_DENIED_MARKER)

    def test_failed_check_can_be_repaired_and_final_commit_verified(
        self, repo: GateRepo
    ) -> None:
        repo.add_file("feature.py")
        repo.commit()
        failing = dict(PROFILE, unknown_default=[_check_that_fails(repo.evidence)])
        assert repo.run_publisher(failing).returncode == VERIFIER_EXIT_DENIED
        repo.add_file("feature.py", "repaired\n")
        head = repo.commit("repair")
        assert repo.run_publisher(PROFILE).returncode == 0
        assert repo.evidence_json()["commit_sha"] == head

    def test_agent_forged_cache_does_not_skip_checks(self, repo: GateRepo) -> None:
        repo.add_file("feature.py")
        repo.commit()
        assert repo.run_publisher(PROFILE).returncode == 0
        failing = dict(PROFILE, unknown_default=[_check_that_fails(repo.evidence)])
        # An unchanged profile id/version does not make writable JSON trusted.
        assert repo.run_publisher(failing).returncode == VERIFIER_EXIT_DENIED
        assert repo.evidence_json()["status"] == "failed"

    def test_dirty_tracked_tree_and_mutating_check_are_blocked(
        self, repo: GateRepo
    ) -> None:
        repo.add_file("feature.py")
        repo.commit()
        repo.add_file("feature.py", "dirty\n")
        assert repo.run_publisher(PROFILE).returncode == VERIFIER_EXIT_DENIED
        assert repo.evidence_json()["status"] == "blocked"
        repo.commit("clean")
        mutating = dict(
            PROFILE,
            unknown_default=[
                dict(_check_that_succeeds(), command="echo changed >> feature.py")
            ],
        )
        assert repo.run_publisher(mutating).returncode == VERIFIER_EXIT_DENIED
        assert repo.evidence_json()["status"] == "blocked"

    def test_unavailable_required_command_is_blocked(self, repo: GateRepo) -> None:
        repo.add_file("feature.py")
        repo.commit()
        unavailable = dict(
            PROFILE,
            unknown_default=[
                dict(_check_that_succeeds(), command="preloop_missing_test_command_428")
            ],
        )
        assert repo.run_publisher(unavailable).returncode == VERIFIER_EXIT_DENIED
        assert repo.evidence_json()["status"] == "blocked"


class TestShippedPresetProfile:
    """The generic profile shipped with preset 011 runs end-to-end: the
    universal hooks pass on a clean docs-only change, and an unknown-impact
    change is refused until the operator configures repository rules."""

    @pytest.fixture(scope="class")
    def preset_profile(self) -> dict:
        import yaml

        preset_path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "presets"
            / "011-automated-issue-implementation.yaml"
        )
        preset = yaml.safe_load(preset_path.read_text())
        return preset["git_clone_config"]["verification"]["profile"]

    def test_docs_only_change_publishes_with_universal_hooks(
        self, tmp_path: pathlib.Path, preset_profile: dict
    ):
        repo = GateRepo(tmp_path)
        repo.add_file("docs/new-page.md")
        repo.commit("docs change")
        result = repo.run_publisher(preset_profile)
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = repo.evidence_json()
        assert {c["id"] for c in evidence["checks"]} == {
            "git-diff-check",
            "no-conflict-markers",
        }
        assert evidence["matched_rule_ids"] == ["docs-only"]
        assert (repo.evidence / "published.marker").exists()

    def test_unknown_impact_is_refused_until_the_profile_is_extended(
        self, tmp_path: pathlib.Path, preset_profile: dict
    ):
        repo = GateRepo(tmp_path)
        repo.add_file("src/widget.py")
        repo.commit("code change")
        result = repo.run_publisher(preset_profile)
        assert result.returncode == VERIFIER_EXIT_DENIED
        assert not (repo.evidence / "published.marker").exists()
        evidence = repo.evidence_json()
        assert evidence["status"] == "failed"
        sentinel = next(
            c for c in evidence["checks"] if c["id"] == "repository-profile-required"
        )
        assert sentinel["exit_code"] == 1
        # The refusal message tells the operator what to configure.
        log = (
            repo.evidence
            / "verification"
            / "checks"
            / "repository-profile-required.log"
        ).read_text()
        assert "git_clone_config.verification.profile" in log

    def test_conflict_markers_in_a_commit_are_caught(
        self, tmp_path: pathlib.Path, preset_profile: dict
    ):
        repo = GateRepo(tmp_path)
        repo.add_file("docs/page.md", "<<<<<<< head\n")
        repo.commit("docs with conflict marker left in")
        result = repo.run_publisher(preset_profile)
        assert result.returncode == VERIFIER_EXIT_DENIED
        evidence = repo.evidence_json()
        conflicted = next(
            c for c in evidence["checks"] if c["id"] == "no-conflict-markers"
        )
        assert conflicted["exit_code"] != 0

    def test_mixed_documentation_and_code_is_not_docs_only(
        self, tmp_path: pathlib.Path, preset_profile: dict
    ) -> None:
        repo = GateRepo(tmp_path)
        repo.add_file("docs/page.md")
        repo.add_file("src/widget.py")
        repo.commit()
        assert repo.run_publisher(preset_profile).returncode == VERIFIER_EXIT_DENIED
        assert repo.evidence_json()["used_unknown_default"]
