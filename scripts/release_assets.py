#!/usr/bin/env python3
"""Checksum and verify the exact assets staged for a GitHub release."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path


CHECKSUMS = "SHA256SUMS"


def assets_in(directory: Path) -> list[Path]:
    """Return staged files, rejecting ambiguous names and indirect content."""
    assets = sorted(directory.iterdir())
    if not assets:
        raise ValueError("Release assets directory is empty")
    for asset in assets:
        if asset.is_symlink() or not asset.is_file():
            raise ValueError(f"Release asset must be a regular file: {asset}")
        if any(character in asset.name for character in "\n\r\\"):
            raise ValueError(f"Unsupported release asset name: {asset.name!r}")
    return assets


def write_checksums(directory: Path) -> None:
    """Hash every payload, including packages, installers, Compose and SBOMs."""
    assets = [asset for asset in assets_in(directory) if asset.name != CHECKSUMS]
    if not assets:
        raise ValueError("No release payloads to checksum")
    lines = []
    for asset in assets:
        with asset.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        lines.append(f"{digest}  {asset.name}\n")
    (directory / CHECKSUMS).write_text("".join(lines), encoding="utf-8")


def verify_assets(
    directory: Path, bundle: Path, repo: str, tag: str, source_sha: str
) -> None:
    """Fail unless every payload and manifest has trusted release provenance.

    Keep the bundle outside the asset directory until this check succeeds.
    Verification delegates cryptography and Sigstore trust roots to GitHub CLI.
    """
    assets = assets_in(directory)
    if not (directory / CHECKSUMS).is_file():
        raise ValueError("Release assets are missing SHA256SUMS")
    if not bundle.is_file():
        raise ValueError("Provenance bundle is missing")
    if bundle.resolve() in [asset.resolve() for asset in assets]:
        raise ValueError("Verify before copying the bundle into release assets")
    for asset in assets:
        subprocess.run(
            [
                "gh",
                "attestation",
                "verify",
                str(asset),
                "--bundle",
                str(bundle),
                "--repo",
                repo,
                "--signer-workflow",
                f"{repo}/.github/workflows/release.yml",
                "--source-ref",
                f"refs/tags/{tag}",
                "--source-digest",
                source_sha,
                "--deny-self-hosted-runners",
            ],
            check=True,
        )


def main() -> None:
    """Run a release asset preparation or verification step."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    checksum = commands.add_parser("checksum")
    checksum.add_argument("directory", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("directory", type=Path)
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--repo", required=True)
    verify.add_argument("--tag", required=True)
    verify.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    if args.command == "checksum":
        write_checksums(args.directory)
    else:
        verify_assets(args.directory, args.bundle, args.repo, args.tag, args.source_sha)


if __name__ == "__main__":
    main()
