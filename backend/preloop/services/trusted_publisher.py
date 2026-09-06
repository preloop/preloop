"""Publish verified commit bundles outside the agent runtime.

Only the control plane constructs bindings and leases. No checkout, Git config,
credential helper, hook, filter, or command supplied by the agent is imported.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

import httpx

from preloop.utils.pr_metadata import (
    PublicationRecord,
    select_metadata,
    upsert_provenance,
)

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


class PublicationError(ValueError):
    """A recoverable, safe-to-display publication failure."""


@dataclass(frozen=True)
class PublicationBinding:
    """Destination and policy resolved by the trusted control plane."""

    repository_url: str
    branch: str
    base: str
    head_sha: str
    expected_remote_sha: str | None
    records: tuple[PublicationRecord, ...]
    public_url: str
    provider: str
    configured_title: str = ""
    configured_body: str = ""
    issue_number: str = ""

    def __post_init__(self) -> None:
        parsed = urlsplit(self.repository_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise PublicationError(
                "Publisher requires a credential-free HTTPS repository binding"
            )
        if self.provider not in {"github", "gitlab"}:
            raise PublicationError("Unsupported publication provider")
        if self.provider == "github" and parsed.hostname != "github.com":
            raise PublicationError("GitHub publication requires github.com")
        if not re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", parsed.path):
            raise PublicationError("Invalid repository path")
        for branch in (self.branch, self.base):
            if (
                not _REF.fullmatch(branch)
                or any(
                    part.startswith(".") or part.endswith((".lock", "."))
                    for part in branch.split("/")
                )
                or ".." in branch
                or "//" in branch
                or branch.endswith("/")
            ):
                raise PublicationError("Invalid bound branch")
        if self.branch == self.base:
            raise PublicationError("Publication to the base branch is prohibited")
        if not _SHA.fullmatch(self.head_sha) or (
            self.expected_remote_sha is not None
            and not _SHA.fullmatch(self.expected_remote_sha)
        ):
            raise PublicationError("Publication requires exact full SHA-1 commit IDs")
        if not self.records or self.records[-1].head_sha != self.head_sha:
            raise PublicationError(
                "Publication provenance does not match the tested head"
            )

    @property
    def project_path(self) -> str:
        """Provider project identifier from the trusted repository URL."""
        return urlsplit(self.repository_url).path.lstrip("/").removesuffix(".git")


@dataclass(frozen=True)
class PublicationLease:
    """Short-lived, repository-scoped credential issued after verification."""

    token: str = field(repr=False)
    repository_url: str
    expires_at: datetime

    def validate(self, binding: PublicationBinding) -> None:
        """Reject wrong-repository or expired leases without exposing secrets."""
        if not self.token or self.repository_url != binding.repository_url:
            raise PublicationError(
                "Publisher credential is not bound to this repository"
            )
        if (
            self.expires_at.tzinfo is None
            or (self.expires_at - datetime.now(timezone.utc)).total_seconds() < 30
        ):
            raise PublicationError(
                "Publisher credential expired or has less than 30 seconds remaining"
            )


def read_publication_bundle(archive: bytes) -> bytes:
    """Read a single regular bundle without extracting any agent-owned paths."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise PublicationError("Publication archive exceeds transfer limit")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            matches = []
            expanded = 0
            count = 0
            for member in tar:
                count += 1
                expanded += max(0, member.size)
                if expanded > MAX_BUNDLE_BYTES * 2 or count > 1024:
                    raise PublicationError(
                        "Publication archive exceeds expansion limit"
                    )
                if member.name in {
                    "branch.bundle",
                    "evidence/branch.bundle",
                    "./branch.bundle",
                    "./evidence/branch.bundle",
                }:
                    if not member.isfile() or member.size > MAX_BUNDLE_BYTES:
                        raise PublicationError(
                            "Publication bundle must be a bounded regular file"
                        )
                    stream = tar.extractfile(member)
                    if stream is None:
                        raise PublicationError("Publication bundle unreadable")
                    matches.append(stream.read(MAX_BUNDLE_BYTES + 1))
            if len(matches) != 1:
                raise PublicationError(
                    "Publication archive must contain exactly one branch.bundle"
                )
            return matches[0]
    except (tarfile.TarError, OSError) as exc:
        raise PublicationError("Invalid publication archive") from exc


class CleanGitRepository:
    """Disposable bare object store; never uses the sandbox repository."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.environment = {
            "PATH": os.defpath,
            "HOME": str(directory),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "0",
            "GIT_ALLOW_PROTOCOL": "https",
            "LC_ALL": "C",
        }
        self.run("init", "--bare", "--template=", str(directory))

    def run(
        self,
        *args: str,
        lease: PublicationLease | None = None,
        check: bool = True,
        strip_output: bool = True,
    ) -> str:
        """Run only publisher-authored Git arguments with bounded time/output."""
        environment = dict(self.environment)
        if lease:
            header = base64.b64encode(f"x-access-token:{lease.token}".encode()).decode()
            environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"http.{lease.repository_url}.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {header}",
                }
            )
        command = [
            "/usr/bin/git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "core.commitGraph=false",
            "-C",
            str(self.directory),
            *args,
        ]
        # Apply resource limits in a dedicated child, never preexec_fn in a
        # multithreaded server. -I prevents Python startup/PYTHONPATH hooks.
        limit_script = (
            "import os, resource, sys; "
            "resource.setrlimit(resource.RLIMIT_CPU, (60, 60)); "
            "resource.setrlimit(resource.RLIMIT_FSIZE, (268435456, 268435456)); "
            "resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128)); "
            "resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); "
            "resource.setrlimit(resource.RLIMIT_AS, (536870912, 536870912)) "
            "if sys.platform.startswith('linux') else None; "
            "os.execv(sys.argv[1], sys.argv[1:])"
        )
        command = [sys.executable, "-I", "-c", limit_script, *command]
        try:
            # No stderr from Git escapes this boundary: it may include auth data.
            result = subprocess.run(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublicationError(
                "Publisher Git operation unavailable or timed out"
            ) from exc
        if len(result.stdout) > 1024 * 1024:
            raise PublicationError("Publisher Git output exceeds limit")
        if check and result.returncode:
            raise PublicationError(
                f"Publisher Git {args[0]} failed; publication can be retried"
            )
        if not check and result.returncode:
            return ""
        decoded = result.stdout.decode("utf-8", errors="replace")
        return decoded.strip() if strip_output else decoded

    def import_bundle(self, bundle: bytes, head_sha: str) -> None:
        """Verify a self-contained bundle and exact commit before any lease exists."""
        if not bundle or len(bundle) > MAX_BUNDLE_BYTES:
            raise PublicationError("Invalid publication bundle size")
        path = self.directory / "input.bundle"
        path.write_bytes(bundle)
        self.run("bundle", "verify", str(path))
        self.run("bundle", "unbundle", str(path))
        if self.run("rev-parse", f"{head_sha}^{{commit}}") != head_sha:
            raise PublicationError("Bundle does not contain the verified commit")
        self.run("fsck", "--strict", "--no-reflogs", head_sha)
        path.unlink()

    def publish(self, binding: PublicationBinding, lease: PublicationLease) -> None:
        """Enforce expected-head comparison and ancestry before an atomic lease push."""
        lease.validate(binding)
        ref = f"refs/heads/{binding.branch}"
        remote = self.run(
            "ls-remote", "--refs", binding.repository_url, ref, lease=lease
        )
        observed = remote.split()[0] if remote else None
        if observed == binding.head_sha:
            return  # A retry after a successful push but failed provider call.
        if observed != binding.expected_remote_sha:
            raise PublicationError(
                "Remote branch changed concurrently; resume and reverify before publishing"
            )
        self.run(
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--depth=1",
            binding.repository_url,
            f"refs/heads/{binding.base}:refs/preloop/base",
            lease=lease,
        )
        # Bundle contains the full history. Fetch must not introduce shallow
        # boundaries on commits already present in that history.
        (self.directory / "shallow").unlink(missing_ok=True)
        if not self.run("merge-base", "refs/preloop/base", binding.head_sha):
            raise PublicationError("Published commit is unrelated to the bound base")
        if observed:
            ancestor = self.run("merge-base", observed, binding.head_sha)
            if ancestor != observed:
                raise PublicationError(
                    "Non-fast-forward publication requires a new verified repair"
                )
        lease.validate(binding)
        # The ancestry check prohibits rewriting history. The lease also rejects
        # a remote race between ls-remote and receive-pack, including first push.
        self.run(
            "push",
            "--porcelain",
            f"--force-with-lease={ref}:{observed or ''}",
            binding.repository_url,
            f"{binding.head_sha}:{ref}",
            lease=lease,
        )


class PullRequestPublisher:
    """Provider operations scoped to a trusted repository and source/base pair."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def upsert(
        self,
        binding: PublicationBinding,
        lease: PublicationLease,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        """Reuse retries and update only provenance on an existing PR/MR."""
        lease.validate(binding)
        github = binding.provider == "github"
        host = urlsplit(binding.repository_url).netloc
        endpoint = (
            f"https://api.github.com/repos/{binding.project_path}/pulls"
            if github
            else f"https://{host}/api/v4/projects/{quote(binding.project_path, safe='')}/merge_requests"
        )
        headers = (
            {"Authorization": f"Bearer {lease.token}"}
            if github
            else {"PRIVATE-TOKEN": lease.token}
        )
        params = (
            {
                "state": "open",
                "head": f"{binding.project_path.split('/')[0]}:{binding.branch}",
                "base": binding.base,
            }
            if github
            else {
                "state": "opened",
                "source_branch": binding.branch,
                "target_branch": binding.base,
            }
        )

        async def request(method: str, url: str, **kwargs: Any) -> Any:
            try:
                response = await self.client.request(
                    method,
                    url,
                    headers=headers,
                    timeout=30,
                    follow_redirects=False,
                    **kwargs,
                )
                response.raise_for_status()
                if len(response.content) > 1024 * 1024:
                    raise PublicationError("PR provider response exceeds limit")
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                if isinstance(exc, PublicationError):
                    raise
                raise PublicationError(
                    "PR provider request failed; publication can be retried"
                ) from exc

        async def lookup() -> dict[str, Any] | None:
            rows = await request("GET", endpoint, params=params)
            if not isinstance(rows, list) or len(rows) > 1:
                raise PublicationError(
                    "PR lookup returned an ambiguous or invalid binding"
                )
            if not rows:
                return None
            row = rows[0]
            if not isinstance(row, dict):
                raise PublicationError("Invalid PR provider response")
            if github:
                if (
                    row.get("head", {}).get("ref") != binding.branch
                    or row.get("base", {}).get("ref") != binding.base
                    or row.get("head", {}).get("repo", {}).get("full_name")
                    != binding.project_path
                ):
                    raise PublicationError(
                        "Provider PR does not match the bound repository/branches"
                    )
            elif (
                row.get("source_branch") != binding.branch
                or row.get("target_branch") != binding.base
                or row.get("source_project_id") != row.get("target_project_id")
            ):
                raise PublicationError(
                    "Provider MR does not match the bound branches/project"
                )
            return row

        existing = await lookup()
        field_name = "body" if github else "description"
        if existing is None:
            payload = {
                "title": title,
                field_name: upsert_provenance(
                    body, binding.records, binding.public_url
                ),
            }
            payload.update(
                {"head": binding.branch, "base": binding.base}
                if github
                else {"source_branch": binding.branch, "target_branch": binding.base}
            )
            try:
                created = await request("POST", endpoint, json=payload)
            except PublicationError:
                # A concurrent create or lost create response can still be a
                # successful publication, but only after authoritative lookup.
                existing = await lookup()
                if existing is None:
                    raise
            else:
                existing = created
                if not isinstance(existing, dict):
                    raise PublicationError("Invalid PR create response")
        number = existing.get("number" if github else "iid")
        if not isinstance(number, int) or number < 1:
            raise PublicationError("Provider did not return a PR identity")
        current_body = existing.get(field_name) or ""
        if not isinstance(current_body, str):
            raise PublicationError("Invalid provider PR body")
        updated_body = upsert_provenance(
            current_body, binding.records, binding.public_url
        )
        if updated_body != current_body:
            await request(
                "PATCH" if github else "PUT",
                f"{endpoint}/{number}",
                json={field_name: updated_body},
            )
        url = existing.get("html_url" if github else "web_url")
        expected_prefix = f"https://{host}/{binding.project_path}/" + (
            "pull/" if github else "-/merge_requests/"
        )
        if url != f"{expected_prefix}{number}":
            raise PublicationError("Provider returned an unexpected PR URL")
        return {
            "url": url,
            "number": number,
            "branch": binding.branch,
            "provider": binding.provider,
            "head_sha": binding.head_sha,
        }


async def publish_verified_bundle(
    *,
    binding: PublicationBinding,
    bundle: bytes,
    result_json: bytes | None,
    acquire_lease: Callable[[], Awaitable[PublicationLease]],
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Import verified artifacts first, acquire write access last, publish once.

    The caller must validate #428 evidence against ``binding.head_sha`` before
    invoking this function. Artifacts never supply destination or credentials.
    """
    with tempfile.TemporaryDirectory(prefix="preloop-publisher-") as temporary:
        repo = await asyncio.to_thread(CleanGitRepository, Path(temporary))
        await asyncio.to_thread(repo.import_bundle, bundle, binding.head_sha)
        commit_title = await asyncio.to_thread(
            repo.run, "show", "-s", "--format=%s", binding.head_sha
        )
        commit_body = await asyncio.to_thread(
            repo.run, "show", "-s", "--format=%b", binding.head_sha
        )
        title, body, warnings = select_metadata(
            result_json,
            configured_title=binding.configured_title,
            configured_body=binding.configured_body,
            commit_title=commit_title,
            commit_body=commit_body,
            issue_number=binding.issue_number,
        )
        # Validate metadata/provenance before credentials are issued.
        upsert_provenance(body, binding.records, binding.public_url)
        lease = await acquire_lease()
        await asyncio.to_thread(repo.publish, binding, lease)
        result = await PullRequestPublisher(client).upsert(binding, lease, title, body)
        result["metadata_warnings"] = warnings
        return result
