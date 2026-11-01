#!/usr/bin/env python3
"""Fail closed on incomplete release SBOMs or unsuccessful tag security checks.

All remote operations are reads. `collect` waits for the exact tag's independent
image/security workflows; `validate` can run entirely offline on fixture assets.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from image_sbom import (
    IMAGES,
    check_identity,
    platform_digests,
    registry_evidence,
    require,
    verify_manifest,
    verify_tag,
    write_json,
)

# The minimum-elements measurer is dependency-free and shared with the SBOM
# generation job. Always measure this checkout, including in offline validation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from preloop.cra.sbom_measure import measure_inputs  # noqa: E402

WORKFLOWS = {
    "ci.yml": None,
    "codeql.yml": None,
    "secret-scan.yml": None,
    "zizmor.yml": None,
    "publish-scanner-image.yml": "scanner",
    "publish-universal-image.yml": "universal",
}

# The workflow that must have signed each image's Sigstore build provenance.
SIGNER_WORKFLOWS = {
    "backend": "release.yml",
    "frontend": "release.yml",
    "scanner": "publish-scanner-image.yml",
    "universal": "publish-universal-image.yml",
}


def select_run(runs: list[dict], tag: str, source_sha: str) -> dict | None:
    """Select the newest push run for this tag and commit, never another ref."""
    matches = [
        run
        for run in runs
        if run.get("event") == "push"
        and run.get("head_branch") == tag
        and run.get("head_sha") == source_sha
    ]
    return max(matches, key=lambda run: run["id"], default=None)


def require_success(run: dict) -> bool:
    """Pending is not a pass; failed, skipped or cancelled runs fail the gate."""
    if run.get("status") != "completed":
        return False
    require(
        run.get("conclusion") == "success",
        f"Required workflow failed: {run.get('html_url', run['id'])}",
    )
    return True


def api(path: str) -> list | dict:
    """Read paginated API data using the runner's existing GitHub identity."""
    pages = json.loads(
        subprocess.check_output(["gh", "api", "--paginate", "--slurp", path])
    )
    if isinstance(pages[0], list):
        return [item for page in pages for item in page]
    return {"workflow_runs": [run for page in pages for run in page["workflow_runs"]]}


def collect(
    directory: Path, repo: str, tag: str, source_sha: str, wait_seconds: int
) -> None:
    """Wait for exact-tag workflows, then download their image SBOM artifacts."""
    check_identity(tag, source_sha)
    deadline = time.monotonic() + wait_seconds
    while True:
        completed: dict[str, dict] = {}
        for workflow in WORKFLOWS:
            query = urlencode(
                {
                    "event": "push",
                    "branch": tag,
                    "head_sha": source_sha,
                    "per_page": 100,
                }
            )
            response = api(f"repos/{repo}/actions/workflows/{workflow}/runs?{query}")
            run = select_run(response["workflow_runs"], tag, source_sha)
            if run and require_success(run):
                completed[workflow] = run
        if set(completed) == set(WORKFLOWS):
            break
        pending = sorted(set(WORKFLOWS) - set(completed))
        require(
            time.monotonic() < deadline,
            f"Timed out waiting for tag workflows: {pending}",
        )
        print(f"Waiting for {tag}@{source_sha}: {', '.join(pending)}", flush=True)
        time.sleep(min(30, max(0, deadline - time.monotonic())))
    # A successful CodeQL workflow means the analysis uploaded, not that it
    # found no issues. Check the tag's active findings too. No severity waiver.
    # The alert listing filters by ref; first prove that code scanning holds
    # an analysis for this exact tag ref and commit, so an unsupported or
    # ignored tag ref fails closed instead of reading as zero findings.
    ref = f"refs/tags/{tag}"
    query = urlencode({"ref": ref, "sha": source_sha, "per_page": 100})
    analyses = api(f"repos/{repo}/code-scanning/analyses?{query}")
    require(
        any(
            item.get("ref") == ref and item.get("commit_sha") == source_sha
            for item in analyses
        ),
        f"No code-scanning analysis recorded for {ref}@{source_sha}",
    )
    query = urlencode({"state": "open", "ref": ref, "per_page": 100})
    alerts = api(f"repos/{repo}/code-scanning/alerts?{query}")
    require(not alerts, f"Tag has {len(alerts)} open code-scanning findings")
    directory.mkdir(parents=True, exist_ok=True)
    for workflow, name in WORKFLOWS.items():
        if name:
            subprocess.run(
                [
                    "gh",
                    "run",
                    "download",
                    str(completed[workflow]["id"]),
                    "--repo",
                    repo,
                    "--name",
                    f"image-sbom-{name}",
                    "--dir",
                    str(directory),
                ],
                check=True,
            )
    write_json(
        directory / "release-audit-runs.json",
        {
            "tag": tag,
            "source_sha": source_sha,
            "open_code_scanning_alerts": 0,
            "workflows": {
                name: {
                    key: run[key]
                    for key in (
                        "id",
                        "run_attempt",
                        "head_sha",
                        "head_branch",
                        "event",
                        "status",
                        "conclusion",
                        "html_url",
                    )
                }
                for name, run in completed.items()
            },
        },
    )


def verify_provenance(
    image: str, name: str, repo: str, tag: str, source_sha: str
) -> None:
    """Require signed build provenance for the image digest from its workflow.

    Args:
        image: Digest-pinned image reference.
        name: Release image name, a key of ``SIGNER_WORKFLOWS``.
        repo: ``owner/name`` of the repository that must have signed it.
        tag: Release tag the provenance must name as its source ref.
        source_sha: Commit the provenance must name as its source digest.

    Raises:
        ValueError: If no attestation from the expected workflow verifies.
    """
    signer = f"{repo}/.github/workflows/{SIGNER_WORKFLOWS[name]}"
    result = subprocess.run(
        [
            "gh",
            "attestation",
            "verify",
            f"oci://{image}",
            "--repo",
            repo,
            "--signer-workflow",
            signer,
            "--source-ref",
            f"refs/tags/{tag}",
            "--source-digest",
            source_sha,
            "--deny-self-hosted-runners",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    verified = []
    if result.returncode == 0:
        try:
            verified = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            verified = []
    require(
        isinstance(verified, list) and verified,
        f"Missing build provenance for {name} image",
    )


def validate(
    directory: Path,
    tag: str,
    source_sha: str,
    verify_registry: bool = False,
    require_workflows: bool = False,
    verify_provenance_for: str | None = None,
    images_required: set[str] | None = None,
) -> dict:
    """Require every image/platform and component SBOM for this release.

    Args:
        directory: Directory holding the downloaded evidence.
        tag: Release tag.
        source_sha: Commit the tag points at.
        verify_registry: Compare the evidence with the published registry.
        require_workflows: Require successful tag workflow evidence.
        verify_provenance_for: Repository whose workflows must have signed
            build provenance for every image digest; ``None`` skips the check.
        images_required: Images that must be present. Defaults to every
            release image; a subset is only for rehearsals.

    Returns:
        The audit report.

    Raises:
        ValueError: On missing or inconsistent evidence.
    """
    check_identity(tag, source_sha)
    required = set(IMAGES) if images_required is None else images_required
    require(required and required <= set(IMAGES), "Unknown required images")
    manifests = sorted(directory.glob("preloop-image-*.json"))
    images: dict[str, dict] = {}
    for path in manifests:
        require(not path.is_symlink(), "Image manifest must not be a symlink")
        data = verify_manifest(path, tag, source_sha)
        require(data["name"] not in images, "Duplicate image evidence")
        images[data["name"]] = data
        if verify_registry:
            version_tag = tag[1:] if data["name"] in {"backend", "frontend"} else tag
            for reference in [data["image"], *data.get("mirrors", [])]:
                verify_tag(reference, version_tag)
                index, published = registry_evidence(reference)
                platforms = platform_digests(index)
                for entry in data["sboms"]:
                    require(
                        platforms[entry["platform"]] == entry["image_digest"],
                        "Published platform digest mismatch",
                    )
                    document = json.loads((directory / entry["file"]).read_text())
                    require(
                        published.get(entry["platform"], {}).get("SPDX") == document,
                        "Published SBOM differs from release asset",
                    )
        if verify_provenance_for:
            verify_provenance(
                data["image"], data["name"], verify_provenance_for, tag, source_sha
            )
    require(
        set(images) == required,
        f"Missing release images: {sorted(required - set(images))}",
    )
    components = []
    for name in ("backend", "frontend", "cli"):
        path = directory / f"preloop-sbom-{name}-{tag[1:]}.cdx.json"
        require(
            path.is_file() and not path.is_symlink(), f"Missing component SBOM: {name}"
        )
        document = json.loads(path.read_text())
        require(
            document.get("bomFormat") == "CycloneDX" and document.get("components"),
            f"Invalid component SBOM: {name}",
        )
        components.append(path.name)
    measured = measure_inputs(
        [(name, (directory / name).read_bytes()) for name in components]
    )
    require(
        measured.get("passed") is True,
        f"Component SBOM minimum elements failed: {measured.get('missing')}",
    )
    if require_workflows:
        evidence = json.loads((directory / "release-audit-runs.json").read_text())
        require(
            evidence.get("tag") == tag and evidence.get("source_sha") == source_sha,
            "Stale workflow evidence",
        )
        require(
            evidence.get("open_code_scanning_alerts") == 0,
            "Open code-scanning findings",
        )
        runs = evidence.get("workflows", {})
        require(set(runs) == set(WORKFLOWS), "Missing required workflow evidence")
        for run in runs.values():
            require(
                select_run([run], tag, source_sha) is not None and require_success(run),
                "Incomplete tag workflow",
            )
    report = {
        "tag": tag,
        "source_sha": source_sha,
        "images": images,
        "component_sboms": components,
        "component_minimum_elements": measured,
        "registry_verified": verify_registry,
        "tag_workflows_verified": require_workflows,
        "provenance_verified": bool(verify_provenance_for),
        "images_required": sorted(required),
    }
    return report


def main() -> None:
    """Collect or validate evidence; neither command publishes anything."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "validate"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--repo", default="preloop/preloop")
    parser.add_argument("--wait-seconds", type=int, default=10800)
    parser.add_argument("--verify-registry", action="store_true")
    parser.add_argument("--require-workflows", action="store_true")
    parser.add_argument("--verify-provenance", action="store_true")
    parser.add_argument(
        "--images",
        help="Comma-separated image subset, for non-publishing rehearsals only",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.command == "collect":
        collect(args.directory, args.repo, args.tag, args.source_sha, args.wait_seconds)
    else:
        report = validate(
            args.directory,
            args.tag,
            args.source_sha,
            args.verify_registry,
            args.require_workflows,
            args.repo if args.verify_provenance else None,
            set(args.images.split(",")) if args.images else None,
        )
        if args.report:
            write_json(args.report, report)
        if (
            args.verify_registry
            and args.require_workflows
            and args.verify_provenance
            and not args.images
        ):
            print("Release SBOM audit passed")
        else:
            print(
                "SBOM validation passed for "
                f"{', '.join(report['images_required'])} (registry, tag workflows, "
                "provenance and the full image set were not all verified; "
                "this is not a passing tag audit)"
            )


if __name__ == "__main__":
    main()
