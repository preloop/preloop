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


def update_readme(plan: ReleasePlan) -> Path:
    """Keep versioned examples aligned with the release."""
    path = ROOT_DIR / "README.md"
    text = read_text(path)
    updated, count = re.subn(
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
        f"and summarize them into bullet points under Added, Fixed, or Changed categories."
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
        ["git", "remote", "get-url", "origin"], capture_output=True, check=False
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
    print(f"Pushing branch {branch}...")
    run_command(["git", "push", "origin", branch])


def push_git_tag(plan: ReleasePlan) -> None:
    """Push the release tag to origin."""
    print(f"Pushing tag {plan.tag}...")
    run_command(["git", "push", "origin", plan.tag])


def find_github_release_run(
    repo: str, head_sha: str, timeout_seconds: int = 180
) -> dict[str, object] | None:
    """Poll GitHub Actions until the Release workflow run appears."""
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
                "databaseId,headSha,status,conclusion,url,displayTitle,event",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            runs = json.loads(result.stdout)
            for run in runs:
                if run.get("headSha") == head_sha and run.get("event") == "push":
                    return run
        time.sleep(5)
    return None


def monitor_release_workflow(plan: ReleasePlan) -> None:
    """Watch the GitHub Release workflow if gh is available."""
    if not command_exists("gh"):
        print("gh CLI not found; skipping workflow monitoring.")
        return

    repo = get_origin_repo_slug()
    if not repo:
        print("Could not infer GitHub repository from origin; skipping monitoring.")
        return

    auth_status = run_command(
        ["gh", "auth", "status"], check=False, capture_output=True
    )
    if auth_status.returncode != 0:
        print("gh is not authenticated; skipping workflow monitoring.")
        return

    head_sha = get_head_sha()
    print("Waiting for GitHub Release workflow to appear...")
    run_data = find_github_release_run(repo, head_sha)
    if not run_data:
        print("Could not find the Release workflow run yet.")
        return

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
            "status,conclusion,url",
        ],
        capture_output=True,
        check=False,
    )

    if final_view.returncode == 0:
        final_data = json.loads(final_view.stdout)
        print(
            f"Release workflow finished with status={final_data.get('status')} "
            f"conclusion={final_data.get('conclusion')}"
        )
        if final_data.get("url"):
            print(final_data["url"])

    if watch.returncode != 0:
        raise SystemExit("Release workflow failed")


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
        help="Create the release tag after committing",
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
        help="Watch the GitHub Release workflow after pushing the tag",
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
    args = parse_args()
    version = resolve_version(args.version)
    plan = ReleasePlan(version=version, release_date=args.release_date)

    print_checklist(plan)
    if not args.yes and not prompt_yes_no("Update versioned files now?", default=True):
        print("Aborted.")
        return 1

    updated_paths = [
        update_version_file(plan),
        update_pyproject(plan),
        update_frontend_package(plan),
        update_frontend_lockfile(plan),
        update_chart(plan),
        update_readme(plan),
    ]

    if not args.skip_changelog:
        updated_paths.append(
            update_changelog(plan, generate_ai=args.generate_changelog_ai)
        )

    if args.package_chart:
        maybe_package_chart()

    commit_release = args.commit
    create_tag = args.tag
    push_branch = args.push_branch
    push_tag = args.push_tag
    monitor_release = args.monitor_release

    if not args.yes:
        commit_release = commit_release or prompt_yes_no(
            "Commit the release preparation changes?", default=True
        )
        if commit_release:
            create_tag = create_tag or prompt_yes_no(
                f"Create git tag {plan.tag}?", default=True
            )
            push_branch = push_branch or prompt_yes_no(
                "Push the current branch to origin?", default=False
            )
            if create_tag:
                push_tag = push_tag or prompt_yes_no(
                    f"Push tag {plan.tag} to origin?", default=False
                )
                if push_tag:
                    monitor_release = monitor_release or prompt_yes_no(
                        "Monitor the GitHub Release workflow with gh?", default=True
                    )
        else:
            if git_has_changes(updated_paths):
                print("Release files are still uncommitted; skipping tag/push steps.")
                create_tag = False
                push_branch = False
                push_tag = False
                monitor_release = False

    if commit_release:
        create_release_commit(plan, updated_paths)

    if create_tag:
        create_git_tag(plan)

    if push_branch:
        push_current_branch()

    if push_tag:
        push_git_tag(plan)

    if monitor_release:
        monitor_release_workflow(plan)

    print("")
    print("Updated files:")
    for path in updated_paths:
        print(f"  - {path.relative_to(ROOT_DIR)}")

    print("")
    print("Next steps:")
    print(
        "  1. Review the diff, especially CHANGELOG.md, README.md, and release assets."
    )
    print("  2. Run your release validation commands.")
    print(
        f"  3. If you did not use automation above, commit/tag/push {plan.tag} when ready."
    )
    print("  4. Verify the GitHub release artifacts and install commands.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
