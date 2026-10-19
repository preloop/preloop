#!/usr/bin/env python3
"""Endorse exact existing release bytes without claiming historical provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY = "preloop/preloop"
GH_REPOSITORY = f"github.com/{REPOSITORY}"
SELECTED_RELEASES = {
    "v0.16.0": 392678101,
    "v0.14.0": 366698662,
    "v0.13.1": 361033319,
    "v0.13.0": 359977947,
}
CERTIFICATE_IDENTITY = (
    "https://github.com/preloop/preloop/.github/workflows/"
    "historical-release-signing.yml@refs/heads/main"
)
ISSUER = "https://token.actions.githubusercontent.com"
STATEMENT = (
    "This is a current maintainer endorsement of the exact historical release "
    "bytes listed below, not original build provenance. The observed tag commit "
    "identifies the release reference; it does not prove those bytes were built "
    "from that commit. Existing artifacts have not been rebuilt or replaced."
)


def canonical_json(value: Any) -> bytes:
    """Serialize an inventory deterministically for explicit approval by digest."""
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def api_json(endpoint: str) -> Any:
    """Read public GitHub metadata without inheriting another default host."""
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", endpoint],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def attachment_names(tag: str) -> tuple[str, str]:
    """Name the two new attachments without impersonating build provenance."""
    base = f"preloop-{tag}-historical-endorsement"
    return f"{base}.json", f"{base}.sigstore"


def validate_asset_name(name: str) -> None:
    """Allow a single safe basename, never paths or command-line options."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name):
        raise ValueError(f"Unsupported release asset name: {name!r}")


def safe_directory(path: Path) -> None:
    """Reject a symlink instead of traversing it for downloaded content."""
    if path.is_symlink():
        raise ValueError(f"Directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    """Hash a regular file in bounded memory without following symlinks."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Asset must be a regular file: {path}")
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tag_commit(tag: str) -> str:
    """Peel annotated tag objects, refusing cycles or unexpected object types."""
    obj = api_json(f"repos/{REPOSITORY}/git/ref/tags/{tag}")["object"]
    for _ in range(8):
        if obj["type"] == "commit" and re.fullmatch(r"[0-9a-f]{40}", obj["sha"]):
            return str(obj["sha"])
        if obj["type"] != "tag":
            break
        obj = api_json(f"repos/{REPOSITORY}/git/tags/{obj['sha']}")["object"]
    raise ValueError(f"Could not peel release tag {tag}")


def snapshot_release(tag: str) -> dict[str, Any]:
    """Snapshot the authorized release, excluding only our two new attachments."""
    expected_id = SELECTED_RELEASES[tag]
    release = api_json(f"repos/{REPOSITORY}/releases/{expected_id}")
    if release["id"] != expected_id or release["tag_name"] != tag or release["draft"]:
        raise ValueError(f"Unexpected release identity or draft state for {tag}")
    if release.get("immutable"):
        raise ValueError(f"Release {tag} is immutable; no attachment is possible")
    assets = []
    names: set[str] = set()
    ids: set[int] = set()
    for asset in release["assets"]:
        name = asset["name"]
        validate_asset_name(name)
        if name in names or asset["id"] in ids:
            raise ValueError(f"Duplicate release asset name or ID for {tag}")
        names.add(name)
        ids.add(asset["id"])
        if name in attachment_names(tag):
            continue
        assets.append(
            {
                "id": asset["id"],
                "name": name,
                "size": asset["size"],
                "api_digest": asset.get("digest"),
                "created_at": asset["created_at"],
                "updated_at": asset["updated_at"],
            }
        )
    if not assets:
        raise ValueError(f"No original release assets for {tag}")
    return {
        "id": expected_id,
        "tag": tag,
        "published_at": release["published_at"],
        "tag_commit": tag_commit(tag),
        "immutable": False,
        "assets": sorted(assets, key=lambda asset: asset["name"]),
    }


def download_asset(asset_id: int, path: Path) -> None:
    """Download one exact API object exclusively; never overwrite local files."""
    with path.open("xb") as stream:
        try:
            subprocess.run(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    f"repos/{REPOSITORY}/releases/assets/{asset_id}",
                    "-H",
                    "Accept: application/octet-stream",
                ],
                stdout=stream,
                check=True,
            )
        except subprocess.CalledProcessError:
            path.unlink()
            raise


def validate_download(asset: dict[str, Any], path: Path) -> str:
    """Compare actual bytes with both recorded size and API digest, when present."""
    digest = sha256_file(path)
    if path.stat().st_size != asset["size"]:
        raise ValueError(f"Size mismatch: {path.name}")
    if asset["api_digest"] not in {None, f"sha256:{digest}"}:
        raise ValueError(f"API digest mismatch: {path.name}")
    return digest


def checksum_coverage(release: dict[str, Any], directory: Path) -> dict[str, list[str]]:
    """Validate every old checksum entry while preserving legitimate partial lists."""
    assets = {asset["name"]: asset for asset in release["assets"]}
    payloads = set(assets) - {"SHA256SUMS"}
    covered: set[str] = set()
    if "SHA256SUMS" in assets:
        for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"([a-f0-9]{64}) [ *](?:\./)?(.+)", line)
            if match is None:
                raise ValueError("Malformed historical SHA256SUMS entry")
            digest, name = match.groups()
            validate_asset_name(name)
            if name not in payloads or name in covered:
                raise ValueError(f"Unexpected or duplicate checksum entry: {name}")
            if assets[name]["sha256"] != digest:
                raise ValueError(f"Checksum mismatch: {name}")
            covered.add(name)
    return {"covered": sorted(covered), "uncovered": sorted(payloads - covered)}


def release_metadata(release: dict[str, Any]) -> dict[str, Any]:
    """Remove only locally computed fields before comparing API snapshots."""
    return {
        key: (
            [{k: v for k, v in asset.items() if k != "sha256"} for asset in value]
            if key == "assets"
            else value
        )
        for key, value in release.items()
        if key != "checksum_coverage"
    }


def assert_unchanged(release: dict[str, Any]) -> None:
    """Refuse any added, removed, renamed, replaced or changed original object."""
    if snapshot_release(release["tag"]) != release_metadata(release):
        raise ValueError(f"Release assets or tag changed: {release['tag']}")


def prepare_inventory(directory: Path) -> dict[str, Any]:
    """Download and independently hash all original files in the authorized scope."""
    safe_directory(directory)
    releases = [snapshot_release(tag) for tag in SELECTED_RELEASES]
    jobs: list[tuple[dict[str, Any], Path]] = []
    for release in releases:
        release_dir = directory / release["tag"]
        safe_directory(release_dir)
        jobs.extend((asset, release_dir / asset["name"]) for asset in release["assets"])

    def prepare_asset(job: tuple[dict[str, Any], Path]) -> None:
        asset, path = job
        if not path.exists() and not path.is_symlink():
            download_asset(asset["id"], path)
        asset["sha256"] = validate_download(asset, path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(prepare_asset, jobs))
    for release in releases:
        release["checksum_coverage"] = checksum_coverage(
            release, directory / release["tag"]
        )
        assert_unchanged(release)
    return {"schema_version": 1, "repository": REPOSITORY, "releases": releases}


def existing_endorsements(tag: str) -> dict[str, Any]:
    """Read only the predetermined new attachment names for a release."""
    release = api_json(f"repos/{REPOSITORY}/releases/{SELECTED_RELEASES[tag]}")
    return {
        asset["name"]: asset
        for asset in release["assets"]
        if asset["name"] in attachment_names(tag)
    }


def download_endorsement(tag: str, name: str, path: Path) -> None:
    """Re-download one new attachment by its exact published API object ID."""
    download_asset(existing_endorsements(tag)[name]["id"], path)


def verify_signature(document: Path, bundle: Path, workflow_sha: str) -> None:
    """Require Sigstore trust, transparency evidence and exact workflow identity."""
    subprocess.run(
        [
            "cosign",
            "verify-blob",
            "--bundle",
            str(bundle),
            "--certificate-identity",
            CERTIFICATE_IDENTITY,
            "--certificate-oidc-issuer",
            ISSUER,
            "--certificate-github-workflow-sha",
            workflow_sha,
            str(document),
        ],
        check=True,
    )


def publish_endorsements(
    inventory: dict[str, Any], directory: Path, workflow_sha: str, run_url: str
) -> None:
    """Verify all originals first, then sign and attach without replacing anything."""
    if not re.fullmatch(r"[0-9a-f]{40}", workflow_sha):
        raise ValueError("Expected the trusted signing workflow's full commit SHA")
    # Preflight the entire scope before issuing any signing identity or upload.
    for release in inventory["releases"]:
        assert_unchanged(release)
        for asset in release["assets"]:
            path = directory / release["tag"] / asset["name"]
            if validate_download(asset, path) != asset["sha256"]:
                raise ValueError(f"Reviewed digest mismatch: {path.name}")
        if existing_endorsements(release["tag"]):
            raise ValueError(
                f"Endorsement attachment already exists for {release['tag']}; verify it instead of replacing or re-signing"
            )

    output = directory / "endorsements"
    safe_directory(output)
    for release in inventory["releases"]:
        tag = release["tag"]
        release_output = output / tag
        release_output.mkdir()  # A rerun requires a clean work directory.
        document, bundle = (release_output / name for name in attachment_names(tag))
        body = {
            "kind": "historical-artifact-endorsement",
            "schema_version": 1,
            "statement": STATEMENT,
            "repository": REPOSITORY,
            "release": release,
            "endorsed_at": datetime.now(timezone.utc).isoformat(),
            "signing_workflow_commit": workflow_sha,
            "signing_workflow_run": run_url,
            "reviewed_inventory_sha256": hashlib.sha256(
                canonical_json(inventory)
            ).hexdigest(),
        }
        document.write_bytes(canonical_json(body))
        subprocess.run(
            [
                "cosign",
                "sign-blob",
                "--yes",
                "--oidc-provider",
                "github-actions",
                "--bundle",
                str(bundle),
                str(document),
            ],
            check=True,
        )
        verify_signature(document, bundle, workflow_sha)
        assert_unchanged(release)
        if existing_endorsements(tag):
            raise ValueError(f"Endorsement attachment appeared during signing: {tag}")
        subprocess.run(
            [
                "gh",
                "release",
                "upload",
                tag,
                "--repo",
                GH_REPOSITORY,
                str(document),
                str(bundle),
            ],
            check=True,
        )
        downloaded = release_output / "downloaded"
        downloaded.mkdir()
        for original in (document, bundle):
            target = downloaded / original.name
            download_endorsement(tag, original.name, target)
            if sha256_file(target) != sha256_file(original):
                raise ValueError(f"Published endorsement differs: {original.name}")
        verify_signature(
            downloaded / document.name, downloaded / bundle.name, workflow_sha
        )
        assert_unchanged(release)
        print(f"Verified historical endorsement for {tag}; original assets unchanged.")


def verify_published(
    inventory: dict[str, Any], directory: Path, workflow_sha: str
) -> None:
    """Verify published endorsements against a separately trusted signing commit."""
    if not re.fullmatch(r"[0-9a-f]{40}", workflow_sha):
        raise ValueError("Expected the trusted signing workflow's full commit SHA")
    for release in inventory["releases"]:
        tag = release["tag"]
        names = attachment_names(tag)
        if set(existing_endorsements(tag)) != set(names):
            raise ValueError(
                f"Missing or partial endorsement for {tag}; nothing was replaced"
            )
        with tempfile.TemporaryDirectory(
            prefix="published-endorsement-", dir=directory
        ) as temporary:
            document, bundle = (Path(temporary) / name for name in names)
            for path in (document, bundle):
                download_endorsement(tag, path.name, path)
            verify_signature(document, bundle, workflow_sha)
            body = json.loads(document.read_bytes())
            if (
                body.get("kind") != "historical-artifact-endorsement"
                or body.get("statement") != STATEMENT
                or body.get("repository") != REPOSITORY
                or body.get("release") != release
                or body.get("signing_workflow_commit") != workflow_sha
                or body.get("reviewed_inventory_sha256")
                != hashlib.sha256(canonical_json(inventory)).hexdigest()
            ):
                raise ValueError(
                    f"Published endorsement differs from reviewed inventory: {tag}"
                )
        assert_unchanged(release)
        print(f"Verified published endorsement and all original bytes: {tag}")


def main() -> int:
    """Prepare a read-only inventory or publish against a reviewed inventory digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "publish", "verify"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256")
    parser.add_argument(
        "--signing-sha", help="Trusted main commit used for the signing run"
    )
    args = parser.parse_args()
    if args.command == "prepare":
        manifest = canonical_json(prepare_inventory(args.directory))
        with args.manifest.open("xb") as stream:
            stream.write(manifest)
        print(f"Reviewed inventory SHA256: {hashlib.sha256(manifest).hexdigest()}")
        return 0

    raw = args.manifest.read_bytes()
    if (
        not args.manifest_sha256
        or hashlib.sha256(raw).hexdigest() != args.manifest_sha256
    ):
        raise ValueError("The approved inventory SHA256 does not match --manifest")
    inventory = json.loads(raw)
    if raw != canonical_json(inventory):
        raise ValueError("The approved inventory must use canonical JSON")
    if args.command == "verify":
        if not args.signing_sha:
            raise ValueError("Verification requires a separately trusted --signing-sha")
        if prepare_inventory(args.directory) != inventory:
            raise ValueError(
                "Downloaded bytes or metadata differ from the approved inventory"
            )
        verify_published(inventory, args.directory, args.signing_sha)
        return 0
    if (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise ValueError("Signing must run in the trusted repository on main")
    workflow_sha = os.environ["GITHUB_SHA"]
    if os.environ.get("GITHUB_WORKFLOW_SHA") != workflow_sha:
        raise ValueError("Signing workflow and checked-out source commits must match")
    if prepare_inventory(args.directory) != inventory:
        raise ValueError(
            "Downloaded bytes or metadata differ from the approved inventory"
        )
    run_url = (
        f"https://github.com/{REPOSITORY}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    )
    publish_endorsements(inventory, args.directory, workflow_sha, run_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
