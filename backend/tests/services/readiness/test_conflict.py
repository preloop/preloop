"""Disposable local git fixtures exercise the pinned isolated ort runtime."""

from typing import Any

import io
import subprocess
import tarfile
from pathlib import Path

import aiodocker
import pytest

from preloop.services.readiness.conflict import IsolatedConflictProbe, IMAGE


async def docker_for_fixture() -> Any:
    """Docker-specific regressions skip only when their isolated runtime is absent."""
    docker = None
    try:
        docker = aiodocker.Docker()
        await docker.system.info()
        await docker.images.inspect(IMAGE)
        return docker
    except (aiodocker.DockerError, OSError, ValueError):
        if docker is not None:
            await docker.close()
        pytest.skip("Pinned local Docker readiness fixture runtime unavailable")


def git(repo: Path, *args: str) -> str:
    return (
        subprocess.check_output(
            ["git", "-C", str(repo), *args],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(repo),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )


def fixture(tmp_path: Path, conflict: bool, malicious: bool = False) -> Any:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "fixture@example.com")
    git(repo, "config", "user.name", "Fixture")
    (repo / "file").write_text("base\n")
    git(repo, "add", "file")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "source")
    (repo / "file").write_text("source\n")
    if malicious:
        (repo / ".gitattributes").write_text("file merge=evil filter=evil\n")
        git(repo, "add", ".gitattributes")
    git(repo, "add", "file")
    git(repo, "commit", "-m", "source")
    source = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    (repo / ("file" if conflict else "another")).write_text("target\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "target")
    target = git(repo, "rev-parse", "HEAD")
    bare = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "--bare", str(repo), str(bare)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if malicious:
        import shlex

        marker = shlex.quote(str(tmp_path / "marker"))
        git(bare, "config", "merge.evil.driver", f"touch {marker}")
        git(bare, "config", "filter.evil.clean", f"touch {marker}")
        git(bare, "config", "credential.helper", f"!touch {marker}")
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as archive:
        archive.add(bare, arcname="repo")
    return repo, source, target, result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict,malicious,expected",
    [
        (False, False, ("pass", None)),
        (True, False, ("fail", "conflict")),
        (False, True, ("unknown", "unsupported_merge_semantics")),
    ],
)
async def test_object_only_probe(
    tmp_path: Any, conflict: Any, malicious: Any, expected: Any, monkeypatch: Any
) -> Any:
    repo, source, target, archive = fixture(tmp_path, conflict, malicious)
    if malicious:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "merge.evil.driver")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"touch {tmp_path / 'marker'}")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "evil-config"))
    before = git(repo, "show-ref")
    probe = IsolatedConflictProbe(None)
    docker = await docker_for_fixture()
    try:
        await docker.system.info()
        result = await probe._offline_archive(docker, archive, source, target)
    finally:
        await docker.close()
    assert result == expected
    assert git(repo, "show-ref") == before
    assert (repo / "file").read_text() == ("target\n" if conflict else "base\n")
    assert not (tmp_path / "marker").exists()


@pytest.mark.asyncio
async def test_offline_environment_blocks_host_canaries_and_network(
    tmp_path: Any, monkeypatch: Any
) -> Any:
    marker = tmp_path / "marker"
    marker.write_text("credential-canary")
    monkeypatch.setenv("READINESS_CREDENTIAL_CANARY", "credential-canary")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "merge.evil.driver")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"touch {marker}")
    config = IsolatedConflictProbe._config("test", [], offline=True)
    assert "credential-canary" not in str(config)
    assert config["HostConfig"]["NetworkMode"] == "none"
    assert "Binds" not in config["HostConfig"]
    docker = await docker_for_fixture()
    container = None
    try:
        script = f'test ! -e "{marker}" && test -z "$READINESS_CREDENTIAL_CANARY" && test "$GIT_CONFIG_COUNT" = 0 && ! /usr/bin/wget -T 1 -q -O /dev/null http://93.184.216.34 && echo isolated'
        container = await docker.containers.create(
            config=IsolatedConflictProbe._config(script, [], offline=True)
        )
        await container.start()
        assert await IsolatedConflictProbe(None)._line(container, 5) == "isolated"
    finally:
        if container:
            await container.delete(force=True, v=True)
        await docker.close()
    assert marker.read_text() == "credential-canary"


@pytest.mark.asyncio
async def test_timeout_and_output_limit_do_not_pass() -> Any:
    import asyncio

    class Logs:
        def log(self, **kwargs) -> Any:
            async def lines() -> Any:
                await asyncio.sleep(0.1)
                yield "pass"

            return lines()

    assert await IsolatedConflictProbe(None)._line(Logs(), 0.001) == "time_limit"

    class LargeLogs:
        def log(self, **kwargs) -> Any:
            async def lines() -> Any:
                yield "x" * (10 * 1024 * 1024 + 1)

            return lines()

    assert await IsolatedConflictProbe(None)._line(LargeLogs(), 1) == "output_limit"
