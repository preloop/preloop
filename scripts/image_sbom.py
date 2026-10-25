#!/usr/bin/env python3
"""Export and validate the SPDX attestations attached to a built image index.

Registry mode is read-only and requires an immutable image reference. OCI mode
reads a local BuildKit export without loading, running or publishing the image.
The same validation and release artifact layout are used in both modes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import Path
from typing import Any

PLATFORMS = {"linux/amd64", "linux/arm64"}
IMAGES = {
    "backend": "ghcr.io/preloop/preloop",
    "frontend": "ghcr.io/preloop/console",
    "scanner": "ghcr.io/preloop/preloop-codex-scanner",
    "universal": "ghcr.io/preloop/preloop-codex-universal",
}
SCHEMA = "preloop.image-sbom/v1"


def digest(data: bytes) -> str:
    """Compute the OCI digest of bytes without normalizing JSON."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def require(condition: Any, message: str) -> None:
    """Fail closed on missing or inconsistent evidence."""
    if not condition:
        raise ValueError(message)


def check_digest(value: str) -> None:
    """Accept only a complete SHA-256 digest."""
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", value), "Invalid image digest")


def check_identity(tag: str, source_sha: str) -> None:
    """Validate identity before using it in filenames or API paths."""
    require(re.fullmatch(r"v[0-9][0-9A-Za-z._-]*", tag), "Invalid release tag")
    require(re.fullmatch(r"[0-9a-f]{40}", source_sha), "Invalid source SHA")


def platform_digests(index: dict) -> dict[str, str]:
    """Require both release architectures and their attached attestations."""
    platforms: dict[str, str] = {}
    attestations: set[str] = set()
    require(index.get("schemaVersion") == 2, "Invalid OCI index")
    for item in index.get("manifests", []):
        check_digest(item["digest"])
        annotations = item.get("annotations", {})
        if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
            subject = annotations.get("vnd.docker.reference.digest", "")
            check_digest(subject)
            attestations.add(subject)
            continue
        platform = item.get("platform", {})
        name = f"{platform.get('os')}/{platform.get('architecture')}"
        require(name in PLATFORMS, f"Unexpected image platform: {name}")
        require(name not in platforms, f"Duplicate image platform: {name}")
        platforms[name] = item["digest"]
    require(set(platforms) == PLATFORMS, "Missing release image architecture")
    require(
        set(platforms.values()) <= attestations,
        "Missing digest-bound image attestation",
    )
    require(attestations <= set(platforms.values()), "Orphan image attestation")
    return platforms


def validate_spdx(document: dict) -> None:
    """Check the SPDX inventory and document graph, not just a JSON filename.

    This checks the release's required SPDX fields, not every optional SPDX field.
    It does not manufacture supplier/license data or waive vulnerability findings.
    """
    require(document.get("spdxVersion") == "SPDX-2.3", "Expected SPDX 2.3")
    require(document.get("dataLicense") == "CC0-1.0", "Invalid SPDX data license")
    require(document.get("SPDXID") == "SPDXRef-DOCUMENT", "Missing SPDX document ID")
    require(
        document.get("name") and document.get("documentNamespace"),
        "Missing SPDX identity",
    )
    creation = document.get("creationInfo", {})
    require(
        creation.get("created") and creation.get("creators"),
        "Missing SPDX creation info",
    )
    packages = document.get("packages", [])
    require(packages, "Empty image package inventory")
    ids = {document["SPDXID"]}
    for item in [*packages, *document.get("files", [])]:
        identifier = item.get("SPDXID")
        require(identifier and identifier not in ids, "Missing or duplicate SPDX ID")
        ids.add(identifier)
    for package in packages:
        require(package.get("name"), "Missing package name")
    require(any(p.get("versionInfo") for p in packages), "No versioned image packages")
    relationships = document.get("relationships", [])
    require(relationships, "Missing SPDX relationships")
    require(
        any(
            r.get("spdxElementId") == document["SPDXID"]
            and r.get("relationshipType") == "DESCRIBES"
            and r.get("relatedSpdxElement") in {p["SPDXID"] for p in packages}
            for r in relationships
        ),
        "SPDX document does not describe its packages",
    )
    for relation in relationships:
        require(relation.get("spdxElementId") in ids, "Dangling SPDX relationship")
        require(
            relation.get("relatedSpdxElement") in ids | {"NONE", "NOASSERTION"},
            "Dangling SPDX relationship",
        )


def registry_evidence(image: str) -> tuple[dict, dict]:
    """Read the exact published index and its per-platform SPDX predicates."""
    require("@" in image, "Image must be pinned by digest")
    check_digest(image.rsplit("@", 1)[1])
    command = ["docker", "buildx", "imagetools", "inspect", image]
    raw = subprocess.check_output([*command, "--raw"])
    # imagetools prints a trailing newline after the registry's JSON bytes.
    require(
        digest(raw.rstrip(b"\n")) == image.rsplit("@", 1)[1], "Index digest mismatch"
    )
    sboms = json.loads(
        subprocess.check_output([*command, "--format", "{{json .SBOM}}"])
    )
    return json.loads(raw), sboms


def verify_tag(image: str, tag: str) -> None:
    """Ensure the user-facing version tag still resolves to the audited index."""
    repository, expected = image.rsplit("@", 1)
    actual = json.loads(
        subprocess.check_output(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                f"{repository}:{tag}",
                "--format",
                "{{json .Manifest.Digest}}",
            ]
        )
    )
    require(actual == expected, "Published version tag digest mismatch")


def oci_evidence(archive: Path) -> tuple[str, dict, dict]:
    """Read a BuildKit OCI tar without extracting untrusted archive paths."""
    with tarfile.open(archive) as tar:

        def read(name: str) -> bytes:
            member = tar.getmember(name)
            require(member.isfile(), f"Not a regular OCI entry: {name}")
            stream = tar.extractfile(member)
            require(stream is not None, f"Missing OCI entry: {name}")
            return stream.read()

        def blob(descriptor: dict) -> dict:
            expected = descriptor["digest"]
            check_digest(expected)
            data = read("blobs/" + expected.replace(":", "/"))
            require(digest(data) == expected, "OCI blob digest mismatch")
            return json.loads(data)

        root = json.loads(read("index.json"))
        require(len(root["manifests"]) == 1, "Expected one exported image index")
        index_descriptor = root["manifests"][0]
        index = blob(index_descriptor)
        platforms = platform_digests(index)
        sboms: dict[str, dict] = {}
        for item in index["manifests"]:
            subject = item.get("annotations", {}).get("vnd.docker.reference.digest")
            if subject not in platforms.values():
                continue
            platform = next(p for p, d in platforms.items() if d == subject)
            for layer in blob(item).get("layers", []):
                if (
                    layer.get("annotations", {}).get("in-toto.io/predicate-type")
                    != "https://spdx.dev/Document"
                ):
                    continue
                statement = blob(layer)
                require(
                    statement.get("predicateType") == "https://spdx.dev/Document",
                    "Wrong predicate type",
                )
                require(platform not in sboms, "Duplicate platform SBOM")
                sboms[platform] = {"SPDX": statement["predicate"]}
        return index_descriptor["digest"], index, sboms


def write_json(path: Path, value: dict) -> None:
    """Write deterministic, readable evidence."""
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def export_sboms(
    directory: Path,
    name: str,
    image: str,
    tag: str,
    source_sha: str,
    index: dict,
    sboms: dict,
    mirrors: list[str] | None = None,
) -> Path:
    """Validate both architectures before writing their release artifacts."""
    check_identity(tag, source_sha)
    require(name in IMAGES, "Unknown release image")
    require(image.startswith(IMAGES[name] + "@"), "Wrong image repository")
    check_digest(image.split("@", 1)[1])
    platforms = platform_digests(index)
    require(
        isinstance(sboms, dict) and set(sboms) == PLATFORMS, "Missing platform SBOM"
    )
    for platform in PLATFORMS:
        require(not sboms[platform].get("AdditionalSPDXs"), "Ambiguous image SBOM")
        validate_spdx(sboms[platform].get("SPDX", {}))
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for platform in sorted(PLATFORMS):
        filename = f"preloop-sbom-image-{name}-{platform.replace('/', '-')}-{tag[1:]}.spdx.json"
        path = directory / filename
        write_json(path, sboms[platform]["SPDX"])
        entries.append(
            {
                "platform": platform,
                "image_digest": platforms[platform],
                "file": filename,
                "digest": digest(path.read_bytes()),
            }
        )
    manifest = directory / f"preloop-image-{name}-{tag[1:]}.json"
    write_json(
        manifest,
        {
            "schema": SCHEMA,
            "name": name,
            "image": image,
            "tag": tag,
            "source_sha": source_sha,
            "mirrors": mirrors or [],
            "sboms": entries,
        },
    )
    return manifest


def verify_manifest(path: Path, tag: str, source_sha: str) -> dict:
    """Verify identity, completeness and checksums of a downloaded artifact."""
    check_identity(tag, source_sha)
    data = json.loads(path.read_text())
    require(data.get("schema") == SCHEMA, "Unknown image SBOM schema")
    name = data.get("name")
    require(name in IMAGES, "Unknown release image")
    require(
        data.get("tag") == tag and data.get("source_sha") == source_sha,
        "Stale image evidence",
    )
    image = data.get("image", "")
    require(image.startswith(IMAGES[name] + "@"), "Wrong image repository")
    check_digest(image.split("@", 1)[1])
    mirrors = data.get("mirrors", [])
    require(isinstance(mirrors, list), "Invalid mirror evidence")
    for mirror in mirrors:
        require(isinstance(mirror, str) and "@" in mirror, "Invalid mirror reference")
        require(
            mirror.rsplit("@", 1)[1] == image.split("@", 1)[1], "Mirror digest mismatch"
        )
    entries = data.get("sboms", [])
    require(
        len(entries) == 2 and {e["platform"] for e in entries} == PLATFORMS,
        "Incomplete platform coverage",
    )
    for entry in entries:
        check_digest(entry["image_digest"])
        filename = f"preloop-sbom-image-{name}-{entry['platform'].replace('/', '-')}-{tag[1:]}.spdx.json"
        require(entry["file"] == filename, "Unexpected SBOM filename")
        file = path.parent / filename
        require(file.is_file() and not file.is_symlink(), "Missing regular SBOM file")
        require(digest(file.read_bytes()) == entry["digest"], "SBOM checksum mismatch")
        validate_spdx(json.loads(file.read_text()))
    return data


def main() -> None:
    """Export published or locally built image evidence without publishing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=IMAGES, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mirror", action="append", default=[])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image")
    source.add_argument("--oci-archive", type=Path)
    args = parser.parse_args()
    if args.oci_archive:
        index_digest, index, sboms = oci_evidence(args.oci_archive)
        image = IMAGES[args.name] + "@" + index_digest
    else:
        image = args.image
        index, sboms = registry_evidence(image)
    for mirror in args.mirror:
        mirror_index, mirror_sboms = registry_evidence(mirror)
        require(
            mirror_index == index and mirror_sboms == sboms,
            "Published mirror SBOM mismatch",
        )
    manifest = export_sboms(
        args.output,
        args.name,
        image,
        args.tag,
        args.source_sha,
        index,
        sboms,
        args.mirror,
    )
    verify_manifest(manifest, args.tag, args.source_sha)
    print(manifest)


if __name__ == "__main__":
    main()
