"""Fixed private-runner helper for freezing and publishing immutable Git bundles.

The helper image is operator pinned. Source is mounted read-only after all agent
writers are removed; publisher input is a distinct frozen volume. Credentials
arrive only on stdin in the publishing process and never enter repository code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from preloop.services.publication_credentials import revoke_repository_lease
from preloop.services.trusted_publisher import (
    MAX_BUNDLE_BYTES,
    CleanGitRepository,
    PublicationBinding,
    PublicationError,
    PublicationLease,
    publish_verified_bundle,
)
from preloop.utils.pr_metadata import PublicationRecord

MAX_RESULT_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 512 * 1024


def read_regular_file(directory: Path, name: str, limit: int) -> bytes:
    """Open only a fixed regular file without following links or accepting aliases."""
    if name not in {"branch.bundle", "result.json"}:
        raise PublicationError("Unsupported publication input name")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
    finally:
        os.close(directory_fd)
    with os.fdopen(fd, "rb") as stream:
        observed = os.fstat(stream.fileno())
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise PublicationError(
                "Publication input must be an unaliased regular file"
            )
        if observed.st_size > limit:
            raise PublicationError("Publication input exceeds size limit")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(data) > limit or (
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise PublicationError("Publication input changed while freezing")
        return data


def inspect_bundle(bundle: bytes, base_sha: str) -> dict[str, Any]:
    """Derive the only HEAD, tree, and diff from an independently imported bundle."""
    if not re.fullmatch(r"[a-f0-9]{40}", base_sha):
        raise PublicationError("Verification requires the trusted exact base commit")
    if not bundle or len(bundle) > MAX_BUNDLE_BYTES:
        raise PublicationError("Invalid publication bundle size")
    with tempfile.TemporaryDirectory(prefix="preloop-freeze-") as temporary:
        repo = CleanGitRepository(Path(temporary))
        path = Path(temporary) / "candidate.bundle"
        path.write_bytes(bundle)
        heads = repo.run("bundle", "list-heads", str(path)).splitlines()
        if len(heads) != 1:
            raise PublicationError("Publication bundle must advertise exactly one HEAD")
        fields = heads[0].split()
        if (
            len(fields) != 2
            or fields[1] != "HEAD"
            or not re.fullmatch(r"[a-f0-9]{40}", fields[0])
        ):
            raise PublicationError("Publication bundle has no unambiguous HEAD")
        head_sha = fields[0]
        repo.import_bundle(bundle, head_sha)
        if repo.run("rev-parse", f"{base_sha}^{{commit}}") != base_sha:
            raise PublicationError("Trusted base is missing from the frozen bundle")
        tree_sha = repo.run("rev-parse", f"{head_sha}^{{tree}}")
        changed = repo.run(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            base_sha,
            head_sha,
            "--",
            strip_output=False,
        )
        return {
            "version": 1,
            "head_sha": head_sha,
            "tree_sha": tree_sha,
            "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
            "changed_files": [name for name in changed.split("\0") if name],
        }


def freeze_publication(
    source: Path, destination: Path, base_sha: str
) -> dict[str, Any]:
    """Copy fixed inputs to a distinct controller-owned volume and inspect bytes."""
    if source.resolve() == destination.resolve():
        raise PublicationError("Frozen publication input must have independent storage")
    bundle = read_regular_file(source, "branch.bundle", MAX_BUNDLE_BYTES)
    manifest = inspect_bundle(bundle, base_sha)
    try:
        result = read_regular_file(source, "result.json", MAX_RESULT_BYTES)
    except FileNotFoundError:
        result = None
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in (("branch.bundle", bundle), ("result.json", result)):
        if data is not None:
            fd = os.open(
                destination / name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o444,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
    return manifest


async def publish_frozen(source: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Validate immutable bytes before using the independently issued writer lease."""
    binding_data = dict(request["binding"])
    binding_data["records"] = tuple(
        PublicationRecord(**record) for record in binding_data["records"]
    )
    binding = PublicationBinding(**binding_data)
    lease_data = dict(request["lease"])
    lease_data["expires_at"] = datetime.fromisoformat(
        lease_data["expires_at"].replace("Z", "+00:00")
    )
    lease = PublicationLease(**lease_data)
    async with httpx.AsyncClient() as client:
        try:
            bundle = read_regular_file(source, "branch.bundle", MAX_BUNDLE_BYTES)
            if hashlib.sha256(bundle).hexdigest() != request["bundle_sha256"]:
                raise PublicationError("Frozen publication bundle digest changed")
            try:
                result = read_regular_file(source, "result.json", MAX_RESULT_BYTES)
            except FileNotFoundError:
                result = None

            async def acquire_lease() -> PublicationLease:
                lease.validate(binding)
                return lease

            return await publish_verified_bundle(
                binding=binding,
                bundle=bundle,
                result_json=result,
                acquire_lease=acquire_lease,
                client=client,
            )
        finally:
            await revoke_repository_lease(lease, client)


def main() -> None:
    """Bounded stdin/stdout protocol; never print exception content or credentials."""
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise PublicationError("Helper request exceeds limit")
        request = json.loads(raw)
        if sys.argv[1:] == ["freeze"]:
            result = freeze_publication(
                Path("/source"), Path("/input"), request["base_sha"]
            )
        elif sys.argv[1:] == ["publish"]:
            result = asyncio.run(publish_frozen(Path("/input"), request))
        else:
            raise PublicationError("Unknown helper phase")
        output = json.dumps(result)
        if len(output.encode()) > 1024 * 1024:
            raise PublicationError("Helper result exceeds limit")
        print(output)
    except Exception:
        print(
            '{"error":"Trusted publication helper failed; see runner recovery status"}',
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
