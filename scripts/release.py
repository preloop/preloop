#!/usr/bin/env python3
"""Interactive release preparation helper for Preloop."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")

ROOT_DIR = Path(__file__).resolve().parents[1]
RELEASE_REMOTE = "origin"


@dataclass(frozen=True)
class ReleasePlan:
    """Describes the files updated during release preparation."""

    version: str
    release_date: str

    @property
    def tag(self) -> str:
        """Return the git tag for this release."""
        return f"v{self.version}"


def prompt_yes_no(message: str, default: bool = True) -> bool:
    """Prompt for a yes/no answer."""
    suffix = "[Y/n]" if default else "[y/N]"
    response = input(f"{message} {suffix} ").strip().lower()
    if not response:
        return default
    return response in {"y", "yes"}


def read_text(path: Path) -> str:
    """Read a UTF-8 text file."""
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    """Write a UTF-8 text file."""
    path.write_text(content, encoding="utf-8")


def replace_once(text: str, pattern: str, replacement: str, path: Path) -> str:
    """Replace one required regex match."""
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"Expected one match for {pattern!r} in {path}")
    return updated


def update_version_file(plan: ReleasePlan) -> Path:
    """Update the canonical VERSION file."""
    path = ROOT_DIR / "VERSION"
    write_text(path, f"{plan.version}\n")
    return path


def update_pyproject(plan: ReleasePlan) -> Path:
    """Update the Python package version."""
    path = ROOT_DIR / "pyproject.toml"
    text = read_text(path)
    updated = replace_once(
        text,
        r'^version = "[^"]+"$',
        f'version = "{plan.version}"',
        path,
    )
    write_text(path, updated)
    return path


def update_openapi_spec(plan: ReleasePlan) -> Path:
    """Update the committed OpenAPI spec version.

    ``scripts/generate_openapi.py`` stamps ``info.version`` from the VERSION
    file, so bumping VERSION without restamping here leaves the committed spec
    stale. CI regenerates the spec and diffs it, which then fails the lint job
    on every release. The version line is the only part that changes at release
    time — schema drift is caught separately by the generate-openapi hook — so
    stamp it directly rather than importing the app, which would make releases
    depend on a fully installed backend environment.
    """
    path = ROOT_DIR / "openapi.yaml"
    text = read_text(path)
    updated = replace_once(
        text,
        r"^  version: .+$",
        f"  version: {plan.version}",
        path,
    )
    write_text(path, updated)
    return path


def update_frontend_package(plan: ReleasePlan) -> Path:
    """Update frontend package.json version."""
    path = ROOT_DIR / "frontend" / "package.json"
    data = json.loads(read_text(path))
    data["version"] = plan.version
    write_text(path, json.dumps(data, indent=2) + "\n")
    return path


def update_frontend_lockfile(plan: ReleasePlan) -> Path:
    """Update frontend package-lock.json root version entries."""
    path = ROOT_DIR / "frontend" / "package-lock.json"
    data = json.loads(read_text(path))
    data["version"] = plan.version
    packages = data.get("packages")
    if isinstance(packages, dict) and "" in packages and isinstance(packages[""], dict):
        packages[""]["version"] = plan.version
    write_text(path, json.dumps(data, indent=2) + "\n")
    return path


def update_chart(plan: ReleasePlan) -> Path:
    """Update Helm chart version and appVersion."""
    path = ROOT_DIR / "helm" / "preloop" / "Chart.yaml"
    text = read_text(path)
    updated = replace_once(text, r"^version: .*$", f"version: {plan.version}", path)
    updated = replace_once(
        updated,
        r'^appVersion: ".*"$',
        f'appVersion: "{plan.version}"',
        path,
    )
    write_text(path, updated)
    return path


def update_server_json(plan: ReleasePlan) -> Path:
    """Update the official MCP registry ``server.json`` version.

    The registry refuses to re-publish an existing version, so every release
    carries its own (scripts/validate_server_json.py checks they match).
    """
    path = ROOT_DIR / "server.json"
    data = json.loads(read_text(path))
    data["version"] = plan.version
    write_text(path, json.dumps(data, indent=2) + "\n")
    return path


def update_citation_metadata(plan: ReleasePlan) -> list[Path]:
    """Update the version in CITATION.cff and codemeta.json."""
    citation = ROOT_DIR / "CITATION.cff"
    text = read_text(citation)
    write_text(
        citation,
        replace_once(text, r'^version: ".*"$', f'version: "{plan.version}"', citation),
    )
    codemeta = ROOT_DIR / "codemeta.json"
    data = json.loads(read_text(codemeta))
    data["version"] = plan.version
    write_text(codemeta, json.dumps(data, indent=2) + "\n")
    return [citation, codemeta]


def update_cli_version(plan: ReleasePlan) -> Path:
    """Update the CLI's hardcoded fallback version.

    Release builds inject the real version via ldflags, but `go run` /
    from-source builds report this constant — it drifted to 0.9.0 while the
    stack was on 0.10.0.
    """
    path = ROOT_DIR / "cli" / "internal" / "version" / "check.go"
    text = read_text(path)
    updated = replace_once(
        text,
        r'^\tVersion = ".*"$',
        f'\tVersion = "{plan.version}"',
        path,
    )
    write_text(path, updated)
    return path


def update_readme(plan: ReleasePlan) -> Path:
    """Keep versioned examples aligned with the release."""
    path = ROOT_DIR / "README.md"
    text = read_text(path)
    updated, _ = re.subn(
        r"PRELOOP_VERSION=[^\s]+",
        f"PRELOOP_VERSION={plan.version}",
        text,
    )

    # Also update any ?version= query parameters
    updated, _ = re.subn(
        r"\?version=[^\s`]+",
        f"?version={plan.version}",
        updated,
    )

    updated, _ = re.subn(
        r"\./scripts/release\.sh [^\s]+",
        f"./scripts/release.sh {plan.version}",
        updated,
    )
    write_text(path, updated)
    return path


def generate_changelog_with_ai(plan: ReleasePlan) -> None:
    """Use an AI agent to generate changelog entries based on git history."""
    if not command_exists("opencode") and not command_exists("codex"):
        print("Neither opencode nor codex CLI found; skipping AI changelog generation.")
        return

    # Find the previous release tag
    result = run_command(
        ["git", "describe", "--tags", "--abbrev=0"], capture_output=True, check=False
    )
    if result.returncode != 0:
        print("Could not find previous tag; skipping AI changelog generation.")
        return
    prev_tag = result.stdout.strip()

    prompt = (
        f"Update CHANGELOG.md. Add a new section for version {plan.version} ({plan.release_date}) "
        f"under the ## [Unreleased] header. "
        f"Read the git commits since {prev_tag} to figure out what changed, "
        f"and summarize them into bullet points under Added, Fixed, Changed, or Deprecated categories."
    )

    print("Triggering AI to generate changelog updates...")
    if command_exists("opencode"):
        result = run_command(
            ["opencode", "run", prompt], capture_output=False, check=False
        )
    else:
        result = run_command(
            ["codex", "run", prompt], capture_output=False, check=False
        )

    if result.returncode != 0:
        print(
            "AI changelog generation failed; continuing with deterministic "
            "release heading update."
        )


def update_changelog(plan: ReleasePlan, *, generate_ai: bool = False) -> Path:
    """Move the current unreleased section under a new release heading."""
    path = ROOT_DIR / "CHANGELOG.md"

    if generate_ai:
        generate_changelog_with_ai(plan)

    text = read_text(path)
    existing_heading = re.search(
        rf"^## \[{re.escape(plan.version)}\](?: - .+)?$",
        text,
        flags=re.MULTILINE,
    )
    if existing_heading:
        return path

    marker = "## [Unreleased]"
    if marker not in text:
        raise ValueError(f"Could not find {marker!r} in {path}")

    heading = f"## [{plan.version}] - {plan.release_date}"
    updated = text.replace(marker, f"{marker}\n\n{heading}", 1)
    write_text(path, updated)
    return path


def run_command(
    args: list[str],
    *,
    capture_output: bool = False,
    check: bool = True,
    cwd: Path = ROOT_DIR,
) -> subprocess.CompletedProcess[str]:
    """Run a command in the repository root."""
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=capture_output,
        check=check,
    )


def command_exists(name: str) -> bool:
    """Return whether a command exists on PATH."""
    return shutil.which(name) is not None


def get_current_branch() -> str:
    """Return the current git branch name."""
    result = run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True
    )
    return result.stdout.strip()


def get_head_sha() -> str:
    """Return the current HEAD commit SHA."""
    result = run_command(["git", "rev-parse", "HEAD"], capture_output=True)
    return result.stdout.strip()


def git_has_changes(paths: list[Path]) -> bool:
    """Return whether the provided paths have uncommitted changes."""
    relative_paths = [str(path.relative_to(ROOT_DIR)) for path in paths]
    result = run_command(
        ["git", "status", "--short", "--", *relative_paths],
        capture_output=True,
        check=False,
    )
    return bool(result.stdout.strip())


def get_origin_repo_slug() -> str | None:
    """Infer the GitHub owner/repo slug from origin."""
    result = run_command(
        ["git", "remote", "get-url", RELEASE_REMOTE], capture_output=True, check=False
    )
    if result.returncode != 0:
        return None

    remote = result.stdout.strip()
    match = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/.]+)(?:\.git)?$", remote)
    if not match:
        return None
    return match.group("slug")


def create_release_commit(plan: ReleasePlan, paths: list[Path]) -> None:
    """Commit the release preparation changes."""
    relative_paths = [str(path.relative_to(ROOT_DIR)) for path in paths]
    run_command(["pre-commit", "run", "--files", *relative_paths])
    run_command(["git", "add", "--", *relative_paths])

    staged = run_command(
        ["git", "diff", "--cached", "--name-only", "--", *relative_paths],
        capture_output=True,
    )
    if not staged.stdout.strip():
        print("No staged release changes to commit.")
        return

    print("Creating release commit...")
    run_command(["git", "commit", "-m", f"release: {plan.tag}"])


def create_git_tag(plan: ReleasePlan) -> None:
    """Create an annotated git tag for the release."""
    existing = run_command(
        ["git", "tag", "--list", plan.tag], capture_output=True, check=False
    )
    if existing.stdout.strip():
        raise SystemExit(f"Tag {plan.tag} already exists")

    print(f"Creating tag {plan.tag}...")
    run_command(["git", "tag", "-a", plan.tag, "-m", f"Release {plan.tag}"])


def push_current_branch() -> None:
    """Push the current branch to origin."""
    branch = get_current_branch()
    if branch in {"main", "master", "HEAD"}:
        raise SystemExit("Release preparation must be pushed through a pull request")
    print(f"Pushing branch {branch}...")
    run_command(["git", "push", RELEASE_REMOTE, branch])


def push_git_tag(plan: ReleasePlan) -> None:
    """Push the release tag to origin."""
    print(f"Pushing tag {plan.tag}...")
    run_command(["git", "push", RELEASE_REMOTE, plan.tag])


def select_release_remote(explicit: str | None) -> str:
    """Prefer the GitHub remote used to publish OSS in multi-remote checkouts."""
    remotes = run_command(["git", "remote"], capture_output=True).stdout.split()
    candidates = (
        [explicit]
        if explicit
        else [name for name in ("github", "origin") if name in remotes]
    )
    for remote in candidates:
        if remote not in remotes:
            raise SystemExit(f"Unknown release remote: {remote}")
        url = run_command(
            ["git", "remote", "get-url", remote], capture_output=True
        ).stdout.strip()
        push_urls = run_command(
            ["git", "remote", "get-url", "--push", "--all", remote],
            capture_output=True,
        ).stdout.splitlines()
        urls = [url, *push_urls]
        if push_urls and all(
            re.fullmatch(
                r"(?:https://github\.com/|git@github\.com:)preloop/preloop(?:\.git)?",
                value,
            )
            for value in urls
        ):
            return remote
    raise SystemExit("Release remote must point to github.com/preloop/preloop")


def ensure_version_metadata(plan: ReleasePlan) -> None:
    """Check every shipped version before allowing a tag."""
    checks = {
        "VERSION": read_text(ROOT_DIR / "VERSION").strip(),
        "pyproject.toml": re.search(
            r'^version = "([^\"]+)"$', read_text(ROOT_DIR / "pyproject.toml"), re.M
        ).group(1),
        "openapi.yaml": re.search(
            r"^  version: (.+)$", read_text(ROOT_DIR / "openapi.yaml"), re.M
        ).group(1),
        "cli/internal/version/check.go": re.search(
            r'^\tVersion = "([^\"]+)"$',
            read_text(ROOT_DIR / "cli/internal/version/check.go"),
            re.M,
        ).group(1),
    }
    package = json.loads(read_text(ROOT_DIR / "frontend/package.json"))
    lock = json.loads(read_text(ROOT_DIR / "frontend/package-lock.json"))
    checks.update(
        {
            "frontend/package.json": package["version"],
            "frontend/package-lock.json": lock["version"],
            "frontend lock root": lock["packages"][""]["version"],
        }
    )
    chart = read_text(ROOT_DIR / "helm/preloop/Chart.yaml")
    checks["chart version"] = re.search(r"^version: (.+)$", chart, re.M).group(1)
    checks["chart appVersion"] = re.search(
        r'^appVersion: "([^\"]+)"$', chart, re.M
    ).group(1)
    checks["server.json"] = json.loads(read_text(ROOT_DIR / "server.json"))["version"]
    checks["codemeta.json"] = json.loads(read_text(ROOT_DIR / "codemeta.json"))[
        "version"
    ]
    citation = re.search(
        r'^version: "([^\"]+)"$', read_text(ROOT_DIR / "CITATION.cff"), re.M
    )
    checks["CITATION.cff"] = citation.group(1) if citation else ""
    wrong = [name for name, value in checks.items() if value != plan.version]
    if wrong:
        raise SystemExit("Release version mismatch: " + ", ".join(wrong))


def ensure_clean_checkout() -> None:
    """Refuse to mix release operations with unrelated local changes."""
    result = run_command(["git", "status", "--porcelain"], capture_output=True)
    if result.stdout.strip():
        raise SystemExit("Commit or stash local changes before release operations")


def prepare_release_branch(plan: ReleasePlan) -> None:
    """Create a release branch from fetched main, never commit directly on main."""
    branch = f"release/{plan.tag}"
    status = run_command(["git", "status", "--porcelain"], capture_output=True)
    if status.stdout.strip():
        raise SystemExit(
            "Preparation has local changes. Review them, run pre-commit, then "
            "commit or stash before retrying. See docs/releasing.md#recovery."
        )
    run_command(["git", "fetch", RELEASE_REMOTE])
    existing = run_command(
        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
        check=False,
    )
    if existing.returncode == 0:
        run_command(["git", "switch", branch])
        return
    remote_branch = run_command(
        ["git", "show-ref", "--verify", f"refs/remotes/{RELEASE_REMOTE}/{branch}"],
        capture_output=True,
        check=False,
    )
    if remote_branch.returncode == 0:
        run_command(
            ["git", "switch", "-c", branch, "--track", f"{RELEASE_REMOTE}/{branch}"]
        )
    else:
        run_command(["git", "switch", "-c", branch, f"{RELEASE_REMOTE}/main"])


def finalize_release(
    plan: ReleasePlan, *, push: bool, target: str | None = None
) -> None:
    """Tag only the reviewed commit at fetched origin/main, without editing files."""
    ensure_clean_checkout()
    run_command(["git", "fetch", RELEASE_REMOTE, "main", "--tags"])
    main_sha = run_command(
        ["git", "rev-parse", f"{RELEASE_REMOTE}/main"], capture_output=True
    ).stdout.strip()
    target_sha = run_command(
        ["git", "rev-parse", "--verify", f"{target or main_sha}^{{commit}}"],
        capture_output=True,
    ).stdout.strip()
    merged = run_command(
        ["git", "merge-base", "--is-ancestor", target_sha, main_sha], check=False
    )
    if merged.returncode != 0:
        raise SystemExit("Release commit must be merged into origin/main")
    if get_head_sha() != target_sha:
        raise SystemExit(
            "Check out the selected merged release commit before finalizing"
        )
    if read_text(ROOT_DIR / "VERSION").strip() != plan.version:
        raise SystemExit("Merged main VERSION does not match the requested release")
    ensure_version_metadata(plan)
    ensure_release_workflow_ready()
    existing = run_command(
        ["git", "rev-parse", "--verify", f"refs/tags/{plan.tag}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != target_sha:
            raise SystemExit(f"Tag {plan.tag} already points to a different commit")
    else:
        create_git_tag(plan)
    if push:
        push_git_tag(plan)
        monitor_release_workflow(plan)
    else:
        print(
            f"Created {plan.tag} locally; rerun with --finalize --push-tag to publish"
        )


def open_release_pr(plan: ReleasePlan) -> None:
    """Push a release branch and open its reviewable pull request."""
    push_current_branch()
    existing = run_command(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "preloop/preloop",
            "--head",
            get_current_branch(),
            "--base",
            "main",
            "--state",
            "open",
            "--json",
            "url",
        ],
        capture_output=True,
    )
    prs = json.loads(existing.stdout)
    if prs:
        print(prs[0]["url"])
        return
    run_command(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            "preloop/preloop",
            "--base",
            "main",
            "--head",
            get_current_branch(),
            "--title",
            f"release: {plan.tag}",
            "--body",
            f"Prepare {plan.tag} version metadata and changelog. "
            "After review and successful checks, merge this PR and finalize the "
            "release from the merged main commit using scripts/release.sh "
            f"{plan.version} --finalize --push-tag. No release tag is created by this PR.",
        ]
    )


def extract_job(workflow: str, job_id: str) -> str:
    """Return one top-level workflow job body, including its key line."""
    match = re.search(
        rf"^  {re.escape(job_id)}:\n.*?(?=^  [A-Za-z0-9-]+:\n|\Z)",
        workflow,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        return ""
    return match.group(0)


def workflow_release_problems(job_text: str) -> list[str]:
    """Return reasons the create-release job cannot publish signed assets.

    v0.16.0 already called ``actions/attest-build-provenance``, then died in
    the changelog step before that attestation ran. Pushing the branch and the
    tag together starts a second Release run for the same commit on ``main``,
    and that run succeeds because Create Release is skipped. A local check has
    to reject the changelog-in-the-environment pattern, and the post-push
    check has to follow the tag run.
    """
    if not job_text.strip():
        return ["release workflow has no create-release job"]

    problems: list[str] = []
    required = (
        (
            "id-token: write",
            "create-release is missing id-token: write, so Sigstore cannot sign",
        ),
        (
            "attestations: write",
            "create-release is missing attestations: write",
        ),
        (
            "actions/attest-build-provenance@",
            "create-release does not attest build provenance",
        ),
        (
            "subject-path: 'release-assets/*'",
            "provenance attestation does not cover release-assets/*",
        ),
        (
            "preloop-${GITHUB_REF_NAME}.intoto.jsonl",
            "provenance bundle is not staged as preloop-<tag>.intoto.jsonl",
        ),
        (
            "files: release-assets/*",
            "Create Release does not upload release-assets/*, so the bundle "
            "would be left behind",
        ),
        (
            "body_path:",
            "release notes must be passed with body_path so the changelog "
            "cannot skip attestation",
        ),
        (
            "release_assets.py verify",
            "release assets are not verified before the GitHub release is created",
        ),
        (
            "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')",
            "create-release must run only on the tag push, not when a release "
            "is published",
        ),
        (
            "Refusing to create",
            "the job must refuse to create a release when the signed bundle is missing",
        ),
        (
            "draft: true",
            "Create Release must stay a draft until provenance is attached",
        ),
        (
            "Refusing to publish",
            "the job must refuse to publish a release whose provenance asset "
            "is missing",
        ),
        (
            "--draft=false",
            "the draft is never published after the provenance check",
        ),
    )
    for snippet, message in required:
        if snippet not in job_text:
            problems.append(message)
    if re.search(r"CHANGELOG_CONTENT\s*:", job_text):
        problems.append(
            "changelog text is passed through the environment; that overflow "
            "skipped attestation on v0.16.0"
        )
    if re.search(r"draft:\s*false", job_text):
        problems.append(
            "Create Release publishes immediately; keep it a draft until the "
            "provenance asset is attached"
        )

    attest = job_text.find("actions/attest-build-provenance@")
    staged = job_text.find("preloop-${GITHUB_REF_NAME}.intoto.jsonl")
    upload = job_text.find("softprops/action-gh-release@")
    publish = job_text.find("--draft=false")
    if min(attest, staged, upload) >= 0 and not (attest < staged < upload):
        problems.append(
            "provenance must be attested and staged before Create Release "
            "uploads assets"
        )
    if upload >= 0 and publish >= 0 and not upload < publish:
        problems.append(
            "the release is published before the provenance asset is uploaded"
        )
    return problems


def ensure_release_workflow_ready() -> None:
    """Refuse to publish when this checkout cannot produce a signed release."""
    workflow_path = ROOT_DIR / ".github" / "workflows" / "release.yml"
    job = extract_job(read_text(workflow_path), "create-release")
    problems = workflow_release_problems(job)
    if not problems:
        return
    detail = "\n".join(f"  - {item}" for item in problems)
    raise SystemExit(f"Release workflow cannot publish signed assets:\n{detail}")


def python_package_version(version: str) -> str:
    """Return the PEP 440 name used for the wheel and sdist.

    The release workflow calls this function. Keeping a second copy in the
    workflow would make the post-publish asset check look for a name the
    build never produced.
    """
    match = re.fullmatch(
        r"(?P<core>\d+\.\d+\.\d+)"
        r"(?:-(?P<label>alpha|beta|rc)\.(?P<num>\d+))?"
        r"(?:\+[0-9A-Za-z.-]+)?",
        version,
    )
    if match and match.group("label"):
        label_map = {"alpha": "a", "beta": "b", "rc": "rc"}
        return (
            f"{match.group('core')}{label_map[match.group('label')]}"
            f"{match.group('num')}"
        )
    return version


def vex_asset_names(root: Path | None = None) -> list[str]:
    """Return OpenVEX filenames the release workflow copies onto the release."""
    vex_dir = (root or ROOT_DIR) / "security" / "vex"
    if not vex_dir.is_dir():
        return []
    return sorted(path.name for path in vex_dir.glob("*.openvex.json"))


def expected_release_assets(version: str, extra: list[str] | None = None) -> set[str]:
    """Return the asset names a published release must carry."""
    package = python_package_version(version)
    names = {
        f"preloop-v{version}.intoto.jsonl",
        "SHA256SUMS",
        "docker-compose.release.yaml",
        "install-cli.sh",
        "install-cli.ps1",
        "install-oss.sh",
        f"preloop-{version}.tgz",
        f"preloop-{package}.tar.gz",
        f"preloop-{package}-py3-none-any.whl",
        f"preloop-sbom-backend-{version}.cdx.json",
        f"preloop-sbom-frontend-{version}.cdx.json",
        f"preloop-sbom-cli-{version}.cdx.json",
        "preloop-linux-amd64",
        "preloop-linux-arm64",
        "preloop-darwin-amd64",
        "preloop-darwin-arm64",
        "preloop-windows-amd64.exe",
        "preloop-windows-arm64.exe",
    }
    for image in ("backend", "frontend", "scanner", "universal"):
        names.add(f"preloop-image-{image}-{version}.json")
        for arch in ("amd64", "arm64"):
            names.add(f"preloop-sbom-image-{image}-linux-{arch}-{version}.spdx.json")
    names.update({"release-audit.json", "release-audit-runs.json"})
    names.update(extra or [])
    return names


def missing_release_assets(expected: set[str], actual: set[str]) -> list[str]:
    """Return expected asset names that are absent from a release."""
    return sorted(expected - actual)


def select_tag_push_run(
    runs: list[dict[str, object]], head_sha: str, tag: str
) -> dict[str, object] | None:
    """Return the tag push run, ignoring the main push of the same commit.

    Pushing the release commit and ``v*`` tag starts two Release workflow
    runs with the same SHA. The main run succeeds without creating a release.
    """
    for run in runs:
        if (
            run.get("headSha") == head_sha
            and run.get("event") == "push"
            and run.get("headBranch") == tag
        ):
            return run
    return None


def release_run_problem(
    run: dict[str, object], jobs: list[dict[str, object]]
) -> str | None:
    """Return why a finished tag run did not publish, if it did not."""
    if run.get("conclusion") != "success":
        return f"Release workflow conclusion={run.get('conclusion')}"
    for job in jobs:
        if job.get("name") == "Create Release":
            if job.get("conclusion") != "success":
                return f"Create Release conclusion={job.get('conclusion')}"
            return None
    return "Create Release job did not run"


def published_release_problem(version: str, payload: dict[str, object]) -> str | None:
    """Return why a GitHub release payload is not a complete signed release."""
    tag = f"v{version}"
    if payload.get("isDraft"):
        return f"{tag} is still a draft"
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return f"{tag} response did not include assets"
    actual: set[str] = set()
    for asset in assets:
        if isinstance(asset, dict) and isinstance(asset.get("name"), str):
            actual.add(asset["name"])
    missing = missing_release_assets(
        expected_release_assets(version, vex_asset_names()), actual
    )
    if not missing:
        return None
    detail = "\n".join(f"  - {name}" for name in missing)
    return f"{tag} is missing release assets:\n{detail}"


def find_github_release_run(
    repo: str, head_sha: str, tag: str, timeout_seconds: int = 180
) -> dict[str, object] | None:
    """Poll GitHub Actions until the tag's Release workflow run appears."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = run_command(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--workflow",
                "Release",
                "--limit",
                "20",
                "--json",
                "databaseId,headSha,headBranch,status,conclusion,url,"
                "displayTitle,event",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            runs = json.loads(result.stdout)
            selected = select_tag_push_run(runs, head_sha, tag)
            if selected is not None:
                return selected
        time.sleep(5)
    return None


def monitor_release_workflow(plan: ReleasePlan) -> None:
    """Watch the tag's Release workflow and require a signed GitHub release.

    Raises:
        SystemExit: ``gh`` cannot see the tag run, the run failed, or the
            published release is missing provenance or another required asset.
    """
    if not command_exists("gh"):
        raise SystemExit(
            "gh CLI not found; the tag was pushed but the release was not verified"
        )

    repo = get_origin_repo_slug()
    if not repo:
        raise SystemExit(
            "Could not infer GitHub repository from origin; "
            "the tag was pushed but the release was not verified"
        )

    auth_status = run_command(
        ["gh", "auth", "status"], check=False, capture_output=True
    )
    if auth_status.returncode != 0:
        raise SystemExit(
            "gh is not authenticated; the tag was pushed but the release "
            "was not verified"
        )

    head_sha = get_head_sha()
    print(f"Waiting for the {plan.tag} Release workflow run...")
    run_data = find_github_release_run(repo, head_sha, plan.tag)
    if not run_data:
        raise SystemExit(
            f"Could not find the Release workflow run for {plan.tag}. "
            "The main-branch run for this commit does not publish a release."
        )

    run_id = str(run_data["databaseId"])
    run_url = str(run_data.get("url", ""))
    print(f"Watching Release workflow run {run_id}")
    if run_url:
        print(run_url)

    watch = run_command(
        [
            "gh",
            "run",
            "watch",
            run_id,
            "--repo",
            repo,
            "--interval",
            "10",
            "--exit-status",
        ],
        check=False,
    )
    final_view = run_command(
        [
            "gh",
            "run",
            "view",
            run_id,
            "--repo",
            repo,
            "--json",
            "status,conclusion,url,jobs",
        ],
        capture_output=True,
        check=False,
    )
    if final_view.returncode != 0:
        raise SystemExit(f"Could not read Release workflow run {run_id}")

    final_data = json.loads(final_view.stdout)
    print(
        f"Release workflow finished with status={final_data.get('status')} "
        f"conclusion={final_data.get('conclusion')}"
    )
    if final_data.get("url"):
        print(final_data["url"])

    jobs = final_data.get("jobs")
    problem = release_run_problem(final_data, jobs if isinstance(jobs, list) else [])
    if problem or watch.returncode != 0:
        raise SystemExit(problem or "Release workflow failed")

    release_view = run_command(
        [
            "gh",
            "release",
            "view",
            plan.tag,
            "--repo",
            repo,
            "--json",
            "assets,isDraft,tagName",
        ],
        capture_output=True,
        check=False,
    )
    if release_view.returncode != 0:
        raise SystemExit(f"Could not read GitHub release {plan.tag}")
    release_problem = published_release_problem(
        plan.version, json.loads(release_view.stdout)
    )
    if release_problem:
        raise SystemExit(release_problem)
    print(f"{plan.tag} has signed provenance and the required release assets.")


def maybe_package_chart() -> None:
    """Optionally run Helm lint and package steps."""
    helm = shutil.which("helm")
    if not helm:
        print("helm not found; skipping chart lint/package.")
        return

    chart_dir = ROOT_DIR / "helm" / "preloop"
    release_dir = ROOT_DIR / "helm-releases"
    release_dir.mkdir(exist_ok=True)

    print("Running helm lint...")
    subprocess.run([helm, "lint", str(chart_dir)], check=True)

    print("Packaging Helm chart...")
    subprocess.run(
        [helm, "package", str(chart_dir), "--destination", str(release_dir)],
        check=True,
    )

    repo_url = (
        os.environ.get("HELM_REPO_URL", "https://charts.preloop.ai")  # type: ignore[name-defined]
    )
    print(f"Generating Helm index for {repo_url}...")
    subprocess.run(
        [helm, "repo", "index", str(release_dir), "--url", repo_url],
        check=True,
    )


def print_checklist(plan: ReleasePlan) -> None:
    """Show the release checklist before mutating files."""
    checklist = [
        "Confirm the release scope is final and CHANGELOG entries are accurate.",
        "Verify README install paths and feature matrix still match the OSS release.",
        "Run backend tests, frontend build/tests, and any smoke tests you consider release-blocking.",
        "Confirm container image names, Helm defaults, and GitHub release assets are aligned.",
        "Confirm installer scripts and release assets will point to the target version.",
        "Validate production auth env vars use SECRET_KEY / ACCESS_TOKEN_EXPIRE_MINUTES consistently.",
        "Ensure SECURITY.md, Code of Conduct, and issue templates are present before the first public release.",
    ]

    print("")
    print(f"Preparing release {plan.version} ({plan.release_date})")
    print("")
    print("Pre-release checklist:")
    for item in checklist:
        print(f"  - {item}")
    print("")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "version",
        nargs="?",
        help="Target release version (for example 0.8.0 or 0.8.0-beta.1)",
    )
    parser.add_argument(
        "--date",
        dest="release_date",
        default=date.today().isoformat(),
        help="Release date for CHANGELOG heading (default: today)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompts",
    )
    parser.add_argument(
        "--skip-changelog",
        action="store_true",
        help="Do not create or update the release heading in CHANGELOG.md",
    )
    parser.add_argument(
        "--generate-changelog-ai",
        action="store_true",
        help=(
            "Ask opencode or codex to draft CHANGELOG entries before creating "
            "the release heading. Disabled by default for reproducible releases."
        ),
    )
    parser.add_argument(
        "--package-chart",
        action="store_true",
        help="Run helm lint/package/index after updating versions",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Commit the release preparation changes",
    )
    parser.add_argument(
        "--tag",
        action="store_true",
        help="Deprecated: use --finalize after the release PR merges",
    )
    parser.add_argument(
        "--push-branch",
        action="store_true",
        help="Push the current branch to origin",
    )
    parser.add_argument(
        "--push-tag",
        action="store_true",
        help="Push the release tag to origin",
    )
    parser.add_argument(
        "--monitor-release",
        action="store_true",
        help=(
            "Watch an already-pushed tag's Release workflow and require its "
            "signed assets. Pushing a tag always does this."
        ),
    )
    parser.add_argument(
        "--open-pr",
        action="store_true",
        help="Commit, push the release branch and open a PR",
    )
    parser.add_argument("--remote", help="GitHub remote (default: github, then origin)")
    parser.add_argument("--target-commit", help="Exact merged release PR commit to tag")
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Tag the clean merged origin/main commit without edits",
    )
    return parser.parse_args()


def resolve_version(raw_version: str | None) -> str:
    """Resolve and validate the target version."""
    version = raw_version
    if not version:
        version = input("Release version: ").strip()

    if not SEMVER_RE.match(version):
        raise SystemExit(
            "Version must match semantic versioning, for example 0.8.0 or 0.8.0-beta.1"
        )
    return version


def main() -> int:
    """Run the release preparation workflow."""
    global RELEASE_REMOTE
    args = parse_args()
    RELEASE_REMOTE = select_release_remote(args.remote)
    version = resolve_version(args.version)
    plan = ReleasePlan(version=version, release_date=args.release_date)

    if args.monitor_release:
        monitor_release_workflow(plan)
        return 0
    if args.finalize:
        finalize_release(plan, push=args.push_tag, target=args.target_commit)
        return 0
    if args.tag or args.push_tag:
        raise SystemExit("Tagging requires --finalize after the release PR merges")
    if args.open_pr and not command_exists("gh"):
        raise SystemExit("gh is required to open a release PR")
    print_checklist(plan)
    if not args.yes and not prompt_yes_no(
        "Prepare a release branch now?", default=True
    ):
        return 1
    prepare_release_branch(plan)
    updated_paths = [
        update_version_file(plan),
        update_pyproject(plan),
        update_openapi_spec(plan),
        update_frontend_package(plan),
        update_frontend_lockfile(plan),
        update_chart(plan),
        update_cli_version(plan),
        update_readme(plan),
        update_server_json(plan),
        *update_citation_metadata(plan),
    ]

    if not args.skip_changelog:
        updated_paths.append(
            update_changelog(plan, generate_ai=args.generate_changelog_ai)
        )

    if args.package_chart:
        maybe_package_chart()

    ensure_release_workflow_ready()
    commit_release = args.commit or args.open_pr
    if not args.yes:
        commit_release = commit_release or prompt_yes_no(
            "Commit the release preparation changes?", default=True
        )
    if commit_release:
        create_release_commit(plan, updated_paths)
    if args.open_pr:
        open_release_pr(plan)
    elif args.push_branch:
        push_current_branch()
    if not args.open_pr:
        print("Review the diff, commit it, then rerun with --yes --open-pr.")
    print("Once the release PR exists, merge it after checks and review pass.")
    print("Then check out merged main and run:")
    print(f"  ./scripts/release.sh {plan.version} --finalize --push-tag")

    return 0


if __name__ == "__main__":
    sys.exit(main())
