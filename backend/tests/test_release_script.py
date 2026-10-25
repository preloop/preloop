"""Guards for the release helper's pre-push and pre-exit checks."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "release_script", REPO_ROOT / "scripts" / "release.py"
)
assert SPEC is not None and SPEC.loader is not None
release_script = importlib.util.module_from_spec(SPEC)
sys.modules["release_script"] = release_script
SPEC.loader.exec_module(release_script)

# The v0.16.0 create-release job passed the changelog through the environment
# and published from an inline body. Inlined so CI does not need that tag.
_V0_16_CREATE_RELEASE = """
  create-release:
    if: startsWith(github.ref, 'refs/tags/v')
    permissions:
      contents: write
      id-token: write
      attestations: write
    steps:
      - name: Finalize changelog
        env:
          CHANGELOG_CONTENT: ${{ steps.changelog.outputs.content }}
        run: printf '%s\\n' "$CHANGELOG_CONTENT"
      - uses: actions/attest-build-provenance@abc
        with:
          subject-path: 'release-assets/*'
      - run: python3 scripts/release_assets.py verify release-assets
      - run: cp "$BUNDLE_PATH" "release-assets/preloop-${GITHUB_REF_NAME}.intoto.jsonl"
      - uses: softprops/action-gh-release@abc
        with:
          files: release-assets/*
          body: |
            notes
          draft: false
"""


def _workflow_text() -> str:
    return (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )


def _create_release_job() -> str:
    return release_script.extract_job(_workflow_text(), "create-release")


def test_current_workflow_can_publish_signed_assets() -> None:
    """The checkout's create-release job passes the pre-push check."""
    assert release_script.workflow_release_problems(_create_release_job()) == []


def test_package_version_comes_from_the_release_helper() -> None:
    """The workflow must not keep a second PEP 440 implementation."""
    job = _create_release_job()
    assert "from release import python_package_version" in job
    assert "label_map" not in job


def test_extract_job_stops_at_the_job_boundary() -> None:
    """The checker must not treat another job's steps as create-release."""
    job = _create_release_job()
    assert job.startswith("  create-release:\n")
    assert "\n  build-backend:\n" not in job
    assert "\n  sbom:\n" not in job


def test_changelog_in_the_environment_is_rejected() -> None:
    """v0.16.0 skipped attestation because the changelog was an env var."""
    job = _create_release_job().replace(
        "body_path:",
        "CHANGELOG_CONTENT: ${{ steps.changelog.outputs.content }}\n          body:",
    )
    problems = release_script.workflow_release_problems(job)
    assert any("changelog text" in item for item in problems)
    assert any("body_path" in item for item in problems)


def test_v0_16_0_tag_workflow_is_rejected() -> None:
    """The v0.16.0 release job shape fails the local check without that tag."""
    problems = release_script.workflow_release_problems(_V0_16_CREATE_RELEASE)
    assert any("changelog text" in item for item in problems)


def test_immediate_publish_is_rejected() -> None:
    """A public release must not be created before the bundle is attached."""
    job = _create_release_job().replace("draft: true", "draft: false", 1)
    problems = release_script.workflow_release_problems(job)
    assert any("publishes immediately" in item for item in problems)
    assert any("stay a draft" in item for item in problems)


def test_upload_before_attestation_is_rejected() -> None:
    """A release that uploads first can ship bytes with no provenance file."""
    job = """
  create-release:
    permissions:
      id-token: write
      attestations: write
    steps:
      - uses: softprops/action-gh-release@abc
        with:
          files: release-assets/*
          body_path: release-notes.md
      - uses: actions/attest-build-provenance@abc
        with:
          subject-path: 'release-assets/*'
      - run: cp bundle release-assets/preloop-${GITHUB_REF_NAME}.intoto.jsonl
      - run: python3 scripts/release_assets.py verify release-assets
"""
    problems = release_script.workflow_release_problems(job)
    assert any("before Create Release" in item for item in problems)


def test_tag_run_is_not_confused_with_the_main_run() -> None:
    """The same commit's green main run must not satisfy the tag check."""
    sha = "a" * 40
    runs = [
        {
            "databaseId": 1,
            "headSha": sha,
            "headBranch": "main",
            "event": "push",
            "conclusion": "success",
        },
        {
            "databaseId": 2,
            "headSha": sha,
            "headBranch": "v0.16.0",
            "event": "push",
            "conclusion": "failure",
        },
    ]
    selected = release_script.select_tag_push_run(runs, sha, "v0.16.0")
    assert selected is not None
    assert selected["databaseId"] == 2
    assert release_script.select_tag_push_run(runs, sha, "v0.17.0") is None


def test_skipped_create_release_job_is_a_failure() -> None:
    """A green workflow that never ran Create Release did not publish."""
    problem = release_script.release_run_problem(
        {"conclusion": "success"},
        [{"name": "Create Release", "conclusion": "skipped"}],
    )
    assert problem == "Create Release conclusion=skipped"


def test_prerelease_asset_names_use_pep440_packages() -> None:
    """Wheels use PEP 440 names; the provenance file keeps the git tag."""
    names = release_script.expected_release_assets("0.8.0-beta.1")
    assert "preloop-v0.8.0-beta.1.intoto.jsonl" in names
    assert "preloop-0.8.0b1-py3-none-any.whl" in names
    assert "preloop-0.8.0b1.tar.gz" in names
    assert "preloop-0.8.0-beta.1.tgz" in names


def test_release_requires_every_image_sbom_and_audit_evidence() -> None:
    names = release_script.expected_release_assets("1.2.3")
    for image in ("backend", "frontend", "scanner", "universal"):
        assert f"preloop-image-{image}-1.2.3.json" in names
        for arch in ("amd64", "arm64"):
            assert f"preloop-sbom-image-{image}-linux-{arch}-1.2.3.spdx.json" in names
    assert {"release-audit.json", "release-audit-runs.json"} <= names


def test_published_release_must_include_provenance() -> None:
    """Missing the bundle is a failed release even when every binary is there."""
    version = "1.2.3"
    names = release_script.expected_release_assets(
        version, release_script.vex_asset_names()
    )
    payload = {
        "isDraft": False,
        "assets": [
            {"name": name} for name in names if not name.endswith(".intoto.jsonl")
        ],
    }
    problem = release_script.published_release_problem(version, payload)
    assert problem is not None
    assert "preloop-v1.2.3.intoto.jsonl" in problem

    complete = {"isDraft": False, "assets": [{"name": name} for name in names]}
    assert release_script.published_release_problem(version, complete) is None


def test_draft_release_is_not_done() -> None:
    """A draft is not a published signed release."""
    problem = release_script.published_release_problem("1.2.3", {"isDraft": True})
    assert problem == "v1.2.3 is still a draft"


def test_finalize_rejects_unmerged_commit(tmp_path, monkeypatch) -> None:
    """A release branch must not be tagged before it reaches main."""
    import subprocess

    import pytest

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "a" * 40, "")
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(release_script, "run_command", run)
    with pytest.raises(SystemExit, match="must be merged"):
        release_script.finalize_release(
            release_script.ReleasePlan("0.17.1", "2026-10-21"), push=True
        )
    assert not any(args[:2] == ["git", "tag"] for args in calls)
    assert not any(args[:2] == ["git", "push"] for args in calls)


def test_finalize_rejects_dirty_checkout(monkeypatch) -> None:
    """Local release metadata must not be mistaken for merged source."""
    import subprocess

    import pytest

    monkeypatch.setattr(
        release_script,
        "run_command",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, " M VERSION", ""),
    )
    with pytest.raises(SystemExit, match="Commit or stash"):
        release_script.finalize_release(
            release_script.ReleasePlan("0.17.1", "2026-10-21"), push=True
        )


def test_finalize_tags_selected_merged_commit(tmp_path, monkeypatch) -> None:
    """Main can advance without changing the selected release merge commit."""
    import subprocess

    calls = []
    release_sha = "b" * 40
    (tmp_path / "VERSION").write_text("0.17.1\n")

    def run(args, **kwargs):
        calls.append(args)
        stdout = ""
        if args == ["git", "rev-parse", "origin/main"]:
            stdout = "a" * 40
        elif args[:3] == ["git", "rev-parse", "--verify"]:
            if args[3].startswith("refs/tags/"):
                return subprocess.CompletedProcess(args, 1, "", "")
            stdout = release_sha
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(release_script, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(release_script, "run_command", run)
    monkeypatch.setattr(release_script, "get_head_sha", lambda: release_sha)
    monkeypatch.setattr(release_script, "ensure_release_workflow_ready", lambda: None)
    monkeypatch.setattr(release_script, "ensure_version_metadata", lambda plan: None)
    release_script.finalize_release(
        release_script.ReleasePlan("0.17.1", "2026-10-21"),
        push=False,
        target=release_sha,
    )
    assert ["git", "merge-base", "--is-ancestor", release_sha, "a" * 40] in calls
    assert ["git", "tag", "-a", "v0.17.1", "-m", "Release v0.17.1"] in calls
    assert not any(args[:2] == ["git", "push"] for args in calls)


def test_prepare_refuses_push_to_main(monkeypatch) -> None:
    """Legacy push flags must never bypass protected main."""
    import pytest

    monkeypatch.setattr(release_script, "get_current_branch", lambda: "main")
    with pytest.raises(SystemExit, match="pull request"):
        release_script.push_current_branch()


def test_finalize_never_moves_existing_tag(tmp_path, monkeypatch) -> None:
    """A conflicting immutable tag must stop before any push."""
    import subprocess

    import pytest

    calls = []
    (tmp_path / "VERSION").write_text("0.17.1\n")

    def run(args, **kwargs):
        calls.append(args)
        stdout = ""
        if args[:2] == ["git", "rev-parse"]:
            stdout = "a" * 40
            if args[-1].startswith("refs/tags/"):
                stdout = "b" * 40
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(release_script, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(release_script, "run_command", run)
    monkeypatch.setattr(release_script, "get_head_sha", lambda: "a" * 40)
    monkeypatch.setattr(release_script, "ensure_release_workflow_ready", lambda: None)
    monkeypatch.setattr(release_script, "ensure_version_metadata", lambda plan: None)
    with pytest.raises(SystemExit, match="different commit"):
        release_script.finalize_release(
            release_script.ReleasePlan("0.17.1", "2026-10-21"), push=True
        )
    assert not any(args[:2] == ["git", "push"] for args in calls)


def test_release_remote_prefers_github_over_gitlab_origin(monkeypatch) -> None:
    """A private mirror must never receive public release pushes."""
    import subprocess

    def run(args, **kwargs):
        output = "origin\ngithub\n"
        if args[-1] == "origin":
            output = "git@gitlab.example.com:team/preloop.git"
        elif args[-1] == "github":
            output = "git@github.com:preloop/preloop.git"
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(release_script, "run_command", run)
    assert release_script.select_release_remote(None) == "github"
    import pytest

    with pytest.raises(SystemExit, match="github.com/preloop/preloop"):
        release_script.select_release_remote("origin")


def test_release_metadata_checks_all_versions(monkeypatch) -> None:
    """A correct VERSION alone cannot conceal a stale packaged version."""
    import pytest

    version = (REPO_ROOT / "VERSION").read_text().strip()
    plan = release_script.ReleasePlan(version, "2026-10-21")
    release_script.ensure_version_metadata(plan)
    original = release_script.read_text

    def read(path):
        value = original(path)
        if path.name == "package.json":
            value = value.replace(f'"version": "{version}"', '"version": "0.0.0"')
        return value

    monkeypatch.setattr(release_script, "read_text", read)
    with pytest.raises(SystemExit, match="frontend/package.json"):
        release_script.ensure_version_metadata(plan)


def test_release_remote_rejects_any_mismatched_push_url(monkeypatch) -> None:
    """Every configured push destination must be the public repository."""
    import subprocess

    import pytest

    def run(args, **kwargs):
        output = "origin"
        if "get-url" in args:
            output = "git@github.com:preloop/preloop.git"
            if "--push" in args:
                output += "\ngit@gitlab.example.com:team/preloop.git"
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(release_script, "run_command", run)
    with pytest.raises(SystemExit, match="github.com/preloop/preloop"):
        release_script.select_release_remote(None)


def test_prepare_reuses_existing_branch(monkeypatch) -> None:
    """Retry after commit/push failure preserves the existing release branch."""
    import subprocess

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(release_script, "RELEASE_REMOTE", "github")
    monkeypatch.setattr(release_script, "run_command", run)
    release_script.prepare_release_branch(release_script.ReleasePlan("1.2.3", "today"))
    assert ["git", "fetch", "github"] in calls
    assert ["git", "switch", "release/v1.2.3"] in calls
    assert not any("-c" in args for args in calls)


def test_prepare_new_branch_uses_selected_remote(monkeypatch) -> None:
    """Fresh preparation starts at fetched public main, never a private mirror."""
    import subprocess

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, int("show-ref" in args), "", "")

    monkeypatch.setattr(release_script, "RELEASE_REMOTE", "github")
    monkeypatch.setattr(release_script, "run_command", run)
    release_script.prepare_release_branch(release_script.ReleasePlan("1.2.3", "today"))
    assert ["git", "switch", "-c", "release/v1.2.3", "github/main"] in calls


def test_prepare_dirty_checkout_explains_recovery(monkeypatch) -> None:
    """A formatting hook failure gives an actionable recovery path."""
    import subprocess

    import pytest

    monkeypatch.setattr(
        release_script,
        "run_command",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, " M VERSION", ""),
    )
    with pytest.raises(SystemExit, match="docs/releasing.md#recovery"):
        release_script.prepare_release_branch(
            release_script.ReleasePlan("1.2.3", "today")
        )


def test_monitor_dispatch_never_prepares_files(monkeypatch) -> None:
    """Monitoring an existing tag must not edit the checkout."""
    from types import SimpleNamespace

    monitored = []
    monkeypatch.setattr(
        release_script,
        "parse_args",
        lambda: SimpleNamespace(
            remote=None,
            version="1.2.3",
            release_date="today",
            monitor_release=True,
        ),
    )
    monkeypatch.setattr(release_script, "select_release_remote", lambda value: "origin")
    monkeypatch.setattr(release_script, "monitor_release_workflow", monitored.append)
    monkeypatch.setattr(release_script, "prepare_release_branch", lambda plan: 1 / 0)
    assert release_script.main() == 0
    assert monitored[0].version == "1.2.3"


def test_legacy_tag_flags_rejected_before_preparation(monkeypatch) -> None:
    from types import SimpleNamespace

    import pytest

    monkeypatch.setattr(
        release_script,
        "parse_args",
        lambda: SimpleNamespace(
            remote=None,
            version="1.2.3",
            release_date="today",
            monitor_release=False,
            finalize=False,
            tag=True,
            push_tag=True,
        ),
    )
    monkeypatch.setattr(release_script, "select_release_remote", lambda value: "origin")
    monkeypatch.setattr(release_script, "prepare_release_branch", lambda plan: 1 / 0)
    with pytest.raises(SystemExit, match="Tagging requires --finalize"):
        release_script.main()


def test_open_pr_reuses_existing_pull_request(monkeypatch, capsys) -> None:
    """Retry after a successful PR creation must not fail with already-exists."""
    import subprocess

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, '[{"url":"https://example.com/pr"}]', ""
        )

    monkeypatch.setattr(release_script, "run_command", run)
    monkeypatch.setattr(release_script, "get_current_branch", lambda: "release/v1.2.3")
    release_script.open_release_pr(release_script.ReleasePlan("1.2.3", "today"))
    assert "https://example.com/pr" in capsys.readouterr().out
    assert not any(args[:3] == ["gh", "pr", "create"] for args in calls)
