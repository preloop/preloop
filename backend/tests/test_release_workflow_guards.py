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


def test_release_publication_requires_complete_tag_audit() -> None:
    jobs = _workflow()["jobs"]
    for job_id in ("create-release", "publish-pypi"):
        assert "release-audit" in jobs[job_id]["needs"]
    audit = jobs["release-audit"]
    assert set(audit["needs"]) == {"build-backend", "build-frontend", "sbom"}
    assert audit["if"] == "startsWith(github.ref, 'refs/tags/v')"
    for step in audit["steps"]:
        assert not step.get("continue-on-error")
    commands = "\n".join(step.get("run", "") for step in audit["steps"])
    assert "release_audit.py collect" in commands
    assert "--verify-registry --require-workflows" in commands
    assert '--tag "$GITHUB_REF_NAME" --source-sha "$GITHUB_SHA"' in commands
    prepare = next(
        s
        for s in jobs["create-release"]["steps"]
        if s.get("name") == "Prepare release assets"
    )
    assert (
        "release_audit.py validate --directory artifacts/audited-sbom" in prepare["run"]
    )
    assert "cp artifacts/audited-sbom/*.json release-assets/" in prepare["run"]
    assert prepare["run"].index("release_audit.py validate") < prepare["run"].index(
        "release_assets.py checksum"
    )


def test_published_backend_console_have_digest_bound_sboms() -> None:
    jobs = _workflow()["jobs"]
    for job_id in IMAGE_JOBS:
        steps = jobs[job_id]["steps"]
        build = next(s for s in steps if s.get("id") == "image")
        assert "@sha256:" in build["with"]["sbom"]
        assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
        export = next(
            s for s in steps if s.get("name") == "Verify and export image SBOMs"
        )
        assert export["env"]["IMAGE_REF"].endswith("@${{ steps.image.outputs.digest }}")
        assert "image_sbom.py" in export["run"]
        upload = next(s for s in steps if s.get("name") == "Upload image SBOM evidence")
        assert upload["with"]["if-no-files-found"] == "error"


def test_codeql_runs_on_release_tags_for_the_audit_gate() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
    assert _trigger(workflow)["push"]["tags"] == ["v*"]


def test_manual_validation_cannot_publish_images_or_releases() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/release-validation.yml").read_text()
    )
    assert "workflow_dispatch" in _trigger(workflow)
    assert workflow["permissions"] == {"contents": "read"}
    for step in workflow["jobs"]["validate"]["steps"]:
        command = step.get("run", "")
        assert "--push" not in command
        assert "docker push" not in command
        assert "gh release" not in command
        assert "docker/login-action" not in step.get("uses", "")


def test_manual_remote_audit_is_read_only_and_runs_the_full_gate() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/release-validation.yml").read_text()
    )
    job = workflow["jobs"]["remote-audit"]
    assert "workflow_dispatch" in job["if"]
    assert set(job["permissions"].values()) == {"read"}
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "release_audit.py collect" in commands
    assert "--verify-registry --require-workflows" in commands
    for forbidden in ("--push", "docker push", "gh release", "secrets."):
        assert forbidden not in commands
    for step in job["steps"]:
        assert "docker/login-action" not in step.get("uses", "")
        assert "${{" not in step.get("run", "")


def test_every_image_evidence_job_checks_out_its_validator() -> None:
    for filename in (
        "release.yml",
        "publish-scanner-image.yml",
        "publish-universal-image.yml",
    ):
        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows" / filename).read_text()
        )
        for job in workflow["jobs"].values():
            checked_out = False
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@"):
                    checked_out = True
                if "scripts/image_sbom.py" in step.get("run", ""):
                    assert checked_out, (filename, job["name"])


def test_universal_exports_attestations_for_the_smoked_image() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/publish-universal-image.yml").read_text()
    )
    steps = workflow["jobs"]["image"]["steps"]
    publish = next(s for s in steps if s.get("name") == "Push smoked amd64 image")
    assert "--sbom=generator=" in publish["run"]
    assert (
        publish["env"]["SMOKED_MANIFEST"] == "${{ steps.smoke_build.outputs.digest }}"
    )
    assert '[ "$platform_digest" != "$SMOKED_MANIFEST" ]' in publish["run"]
    for step in steps[: steps.index(publish)]:
        if "buildx prune" in step.get("run", ""):
            assert "needs.gate.outputs.publish != 'true'" in step["if"]
    arm = next(s for s in steps if s.get("name") == "Build and push arm64")
    assert "@sha256:" in arm["with"]["sbom"]


def test_virustotal_cannot_modify_release_contents() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    job = workflow["jobs"]["virustotal-windows-cli"]
    assert job["permissions"] == {"contents": "read"}
    scan = next(step for step in job["steps"] if "virustotal@" in step.get("uses", ""))
    assert scan["with"]["update_release_body"] is False
    assert "github_token" not in scan["with"]
