"""Guards for the release workflow's Windows signing configuration check.

The release may publish unsigned Windows binaries only while the repository
variable ``SIGNPATH_SIGNING_REQUIRED`` is not ``true``. These tests pin the
workflow wiring: the check step reads the variable and the missing-credential
branch can fail the release, so signing cannot silently regress once it is
required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
IMAGE_JOBS = ("build-backend", "build-frontend")


def _signpath_check_step() -> Dict:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("id") == "signpath-check":
                return step
    raise AssertionError("release.yml has no signpath-check step")


def test_signing_required_variable_is_wired() -> None:
    """The check step reads SIGNPATH_SIGNING_REQUIRED from repo variables."""
    step = _signpath_check_step()
    assert step["env"]["REQUIRED"] == "${{ vars.SIGNPATH_SIGNING_REQUIRED }}"


def test_missing_credentials_fail_when_signing_is_required() -> None:
    """The required-but-unconfigured branch exits nonzero, failing release."""
    step = _signpath_check_step()
    script = step["run"]
    required_branch = script.split('[ "$required" = "true" ]', 1)
    assert len(required_branch) == 2, "no required branch in signpath-check"
    assert "exit 1" in required_branch[1].split("else", 1)[0]


def test_signing_required_value_is_case_normalized() -> None:
    """TRUE/True/1 count as required; the check lowercases before matching."""
    script = _signpath_check_step()["run"]
    assert "tr '[:upper:]' '[:lower:]'" in script
    assert '[ "$required" = "1" ]' in script


def test_optional_mode_still_publishes_unsigned() -> None:
    """Without the variable, the step keeps the enabled=false fallback."""
    step = _signpath_check_step()
    assert "enabled=false" in step["run"]


def _workflow() -> Dict[str, Any]:
    loaded = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _trigger(workflow: Dict[str, Any]) -> Dict[str, Any]:
    """Return the ``on:`` mapping.

    PyYAML 1.1 reads a bare ``on`` key as boolean ``True``.
    """
    trigger = workflow.get("on", workflow.get(True))
    assert isinstance(trigger, dict)
    return trigger


def test_images_publish_only_on_version_tags() -> None:
    """GHCR and Docker Hub move only for a ``v*`` tag, not main or a PR.

    ``workflow_dispatch`` is absent on purpose. A tag push publishes, and a
    release published from that tag does too. ``latest`` is a stable tag
    only: a pre-release suffix (a hyphen in the tag name) does not move it.
    Version tags come from the semver patterns, not from the branch name.
    """
    workflow = _workflow()
    trigger = _trigger(workflow)
    assert "workflow_dispatch" not in trigger
    assert "pull_request" not in trigger
    assert trigger["push"]["branches"] == ["main"]
    assert trigger["push"]["tags"] == ["v*"]
    assert trigger["release"]["types"] == ["published"]
    assert workflow["permissions"] == {"contents": "read"}

    for job_id in IMAGE_JOBS:
        job = workflow["jobs"][job_id]
        assert job["if"] == "startsWith(github.ref, 'refs/tags/v')", job_id
        assert job["permissions"] == {
            "contents": "read",
            "packages": "write",
        }, job_id
        login = next(
            step for step in job["steps"] if step.get("name") == "Log in to Docker Hub"
        )
        assert login["if"] == "vars.PUSH_TO_DOCKERHUB == 'true'"
        meta = next(
            step for step in job["steps"] if step.get("name") == "Extract metadata"
        )
        tags = meta["with"]["tags"]
        assert "type=semver,pattern={{version}}" in tags
        assert "type=semver,pattern={{major}}.{{minor}}" in tags
        assert (
            "type=raw,value=latest,enable=${{ !contains(github.ref_name, '-') }}"
            in tags
        )
        assert "type=ref,event=branch" not in tags
        assert "type=ref,event=pr" not in tags
        assert "enable={{is_default_branch}}" not in tags
        push = next(
            step for step in job["steps"] if step.get("name") == "Build and push"
        )
        assert push["with"]["push"] is True
