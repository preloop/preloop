"""Disposable Docker object stores; fetch secrets never enter the offline probe.

The runtime is optional. If Docker cannot enforce the requested container
isolation/resources, callers receive unknown and no host Git command runs.
"""

from __future__ import annotations

import asyncio
import base64
import re
import tempfile
from datetime import UTC, datetime
from typing import Any

import aiodocker
from aiohttp import ClientError

from preloop.schemas.readiness import GateEvidence, GateState
from preloop.services.managed_credentials import (
    CredentialSource,
    ManagedCredentialError,
)

IMAGE = (
    "alpine/git@sha256:4a0e72d49596a1f5d3701aeedafdadc5c0da4062be4657c7bdc4017387f591cc"
)
PROBE_VERSION = "docker-git-2.52.0-ort-v1"
MAX_SCRATCH = 1024 * 1024 * 1024
MAX_OUTPUT = 10 * 1024 * 1024
_BASE_ENV = (
    "PATH=/usr/bin:/bin",
    "HOME=/scratch/home",
    "XDG_CONFIG_HOME=/scratch/home",
    "LC_ALL=C",
    "GIT_CONFIG_NOSYSTEM=1",
    "GIT_CONFIG_GLOBAL=/dev/null",
    "GIT_CONFIG_SYSTEM=/dev/null",
    "GIT_CONFIG_COUNT=0",
    "GIT_TERMINAL_PROMPT=0",
    "GIT_NO_REPLACE_OBJECTS=1",
    "GIT_NO_LAZY_FETCH=1",
    "GIT_EXEC_PATH=/usr/libexec/git-core",
    "GIT_ATTR_NOSYSTEM=1",
    "GIT_ALLOW_PROTOCOL=https",
)
# Credentials are injected only for the fetch process. The config command
# arguments here are fixed and the remote is validated before container creation.
_SETUP = r"""
set -eu
mkdir -p /scratch/home /scratch/repo
[ "$(/usr/bin/git --version)" = "git version 2.52.0" ] || exit 2
git() {
 /usr/bin/git -c core.hooksPath=/dev/null -c credential.helper= \
 -c http.followRedirects=false -c protocol.allow=never \
 -c protocol.https.allow=always -c core.attributesFile=/dev/null \
 -c core.fsmonitor=false -c core.useReplaceRefs=false \
 -c submodule.recurse=false -C /scratch/repo "$@"
}
"""
_FETCH = (
    _SETUP
    + r"""
{
 git init --bare --template= /scratch/repo
 export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="http.$REMOTE.extraHeader"
 export GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH"
 git fetch --no-tags --no-write-fetch-head --no-recurse-submodules "$REMOTE" "$SOURCE" "$TARGET"
 unset AUTH GIT_CONFIG_VALUE_0 GIT_CONFIG_KEY_0
 export GIT_CONFIG_COUNT=0
 git cat-file -e "$SOURCE^{commit}"
 git cat-file -e "$TARGET^{commit}"
} >/scratch/output 2>&1 || { echo fetch_failed; exit 1; }
rm -f /scratch/output
echo fetched
exec /bin/sleep 3600
"""
)
_OFFLINE = (
    _SETUP
    + r"""
while [ ! -f /scratch/input_ready ]; do sleep 0.05; done
calculate() {
 base=$(git merge-base --all "$SOURCE" "$TARGET") || return 3
 set -- $base
 [ "$#" = 1 ] || { echo unsupported_merge_semantics > /scratch/result; return 4; }
 # Inspect fetched objects without checkout. Conservatively reject any
 # attribute directive, including custom merge/filter or unsupported macros.
 for tree in "$SOURCE" "$TARGET" "$base"; do
   git ls-tree -r "$tree" > /scratch/paths
   while read -r mode type object path; do
     [ "$mode" != 160000 ] || { echo unsupported_merge_semantics > /scratch/result; return 4; }
     case "$path" in
       *gitattributes*)
         git cat-file blob "$object" > /scratch/attrs
         if /usr/bin/awk '/^[[:space:]]*($|#)/ {next} {found=1} END {exit !found}' /scratch/attrs; then
           echo unsupported_merge_semantics > /scratch/result
           return 4
         fi ;;
     esac
   done < /scratch/paths
 done
 git merge-tree --write-tree --no-messages "$TARGET" "$SOURCE" > /scratch/merge
 code=$?
 [ "$code" = 0 ] && echo pass > /scratch/result
 [ "$code" = 1 ] && echo fail > /scratch/result
 [ "$code" -le 1 ] || return 3
}
calculate >/scratch/output 2>&1
code=$?
if [ -f /scratch/result ]; then cat /scratch/result; else echo missing_objects; fi
exit "$code"
"""
)
# `set -e` must not skip the merge-tree conflict return-code handling.
_OFFLINE = _OFFLINE.replace("set -eu", "set -u")


class IsolatedConflictProbe:
    """Credentialed fetch container is destroyed before an offline one starts."""

    def __init__(self, credential_source: CredentialSource | None) -> None:
        self.credential_source = credential_source

    async def assess(
        self, repository: str, source_sha: str, target_sha: str
    ) -> GateEvidence:
        """Return sampled ort evidence, or fail closed with a specific reason."""
        reason: str | None = "isolation_unavailable"
        state: GateState = "unknown"
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository
        ) or not all(
            re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (source_sha, target_sha)
        ):
            reason = "invalid_identity"
        elif self.credential_source is None:
            reason = "credential_lease_unavailable"
        else:
            try:
                state, reason = await self._assess(repository, source_sha, target_sha)
            except (
                aiodocker.DockerError,
                ClientError,
                OSError,
                ValueError,
                asyncio.TimeoutError,
                ManagedCredentialError,
            ):
                reason = "isolation_unavailable"
        return GateEvidence(
            name="conflict",
            state=state,
            reason=reason,
            source="isolated_object_merge_tree",
            retrieved_at=datetime.now(UTC),
            source_sha=source_sha,
            target_sha=target_sha,
            probe_version=PROBE_VERSION,
            strategy="ort",
        )

    @staticmethod
    def _config(script: str, env: list[str], *, offline: bool) -> dict[str, Any]:
        return {
            "Image": IMAGE,
            "Entrypoint": [
                "/usr/bin/timeout",
                "-s",
                "KILL",
                "19" if offline else "120",
                "/bin/sh",
                "-c",
            ],
            "Cmd": [script],
            "Env": [*_BASE_ENV, *env],
            "AttachStdout": True,
            "AttachStderr": False,
            "HostConfig": {
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "NetworkMode": "none" if offline else "bridge",
                "Memory": 512 * 1024 * 1024,
                "MemorySwap": 512 * 1024 * 1024,
                "NanoCpus": 1_000_000_000,
                "PidsLimit": 16,
                "Tmpfs": {
                    "/scratch": "rw,noexec,nosuid,nodev,size=1g",
                    "/git": "ro,noexec,nosuid,nodev,size=1m",
                },
                "Ulimits": [
                    {"Name": "fsize", "Soft": MAX_SCRATCH, "Hard": MAX_SCRATCH},
                    {"Name": "core", "Soft": 0, "Hard": 0},
                    {
                        "Name": "cpu",
                        "Soft": 20 if offline else 120,
                        "Hard": 20 if offline else 120,
                    },
                ],
                "LogConfig": {
                    "Type": "json-file",
                    "Config": {"max-size": "10m", "max-file": "1"},
                },
            },
        }

    async def _line(self, container: Any, seconds: float) -> str:
        async def read() -> str:
            total = 0
            async for line in container.log(stdout=True, follow=True):
                total += len(line.encode())
                if total > MAX_OUTPUT:
                    return "output_limit"
                if line.strip():
                    return str(line).strip()
            return "process_failed"

        try:
            return await asyncio.wait_for(read(), seconds)
        except asyncio.TimeoutError:
            return "time_limit"

    async def _assess(
        self, repository: str, source: str, target: str
    ) -> tuple[GateState, str | None]:
        assert self.credential_source is not None
        credential = await self.credential_source(force_refresh=False)
        remaining = credential.seconds_remaining()
        # Long-lived credentials cannot enter this measurement runner.
        if remaining < 125 or remaining > 3600:
            return "unknown", "credential_lease_unavailable"
        auth = base64.b64encode(
            f"x-token-auth:{credential.access_token}".encode()
        ).decode()
        docker = aiodocker.Docker()
        fetch = None
        offline = None
        try:
            info = await docker.system.info()
            if (
                info.get("OSType") != "linux"
                or not info.get("MemoryLimit")
                or not info.get("SwapLimit")
            ):
                return "unknown", "isolation_unavailable"
            fetch = await docker.containers.create(
                config=self._config(
                    _FETCH,
                    [
                        f"REMOTE=https://bitbucket.org/{repository}.git",
                        f"SOURCE={source}",
                        f"TARGET={target}",
                        f"AUTH={auth}",
                    ],
                    offline=False,
                )
            )
            await fetch.start()
            result = await self._line(fetch, 120)
            if result != "fetched":
                return "unknown", result
            # Only the bare object repository is transported. Secrets, output,
            # config/home and environment never become part of this archive.
            with tempfile.TemporaryFile() as archive:
                async with docker._query(
                    f"containers/{fetch.id}/archive",
                    method="GET",
                    params={"path": "/scratch/repo"},
                ) as response:
                    count = 0
                    async for chunk in response.content.iter_chunked(65536):
                        count += len(chunk)
                        if count > MAX_SCRATCH:
                            return "unknown", "scratch_limit"
                        archive.write(chunk)
                await fetch.delete(force=True, v=True)
                fetch = None
                del auth, credential
                return await self._offline_archive(docker, archive, source, target)
        finally:
            for container in (fetch, offline):
                if container is not None:
                    await container.delete(force=True, v=True)
            await docker.close()

    async def _offline_archive(
        self, docker: Any, archive: Any, source: str, target: str
    ) -> tuple[GateState, str | None]:
        try:
            return await asyncio.wait_for(
                self._run_offline_archive(docker, archive, source, target), 19.8
            )
        except asyncio.TimeoutError:
            return "unknown", "time_limit"

    async def _run_offline_archive(
        self, docker: Any, archive: Any, source: str, target: str
    ) -> tuple[GateState, str | None]:
        """Probe a fresh container after the secret-bearing fetch was destroyed."""
        offline = None
        try:
            offline = await docker.containers.create(
                config=self._config(
                    _OFFLINE, [f"SOURCE={source}", f"TARGET={target}"], offline=True
                )
            )
            await offline.start()
            archive.seek(0)
            extraction = await offline.exec(
                ["/bin/tar", "-x", "-C", "/scratch"], stdin=True
            )
            async with extraction.start() as stream:
                while chunk := archive.read(65536):
                    await stream.write_in(chunk)
            while (await extraction.inspect()).get("Running"):
                await asyncio.sleep(0.05)
            if (await extraction.inspect()).get("ExitCode") != 0:
                return "unknown", "scratch_limit"
            marker = await offline.exec(["/bin/touch", "/scratch/input_ready"])
            async with marker.start() as stream:
                while await stream.read_out() is not None:
                    pass
            # One CPU cgroup + wall bound below 20 seconds bounds total
            # CPU for all descendants, stricter than the 30s wall default.
            result = await self._line(offline, 19.8)
            if result == "pass":
                return "pass", None
            if result == "fail":
                return "fail", "conflict"
            state = (await offline.show()).get("State", {})
            if state.get("OOMKilled"):
                return "unknown", "memory_limit"
            if state.get("ExitCode", 0) >= 128:
                return "unknown", "process_limit"
            return "unknown", result
        finally:
            if offline is not None:
                await offline.delete(force=True, v=True)
