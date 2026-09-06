"""Exercise frozen helper boundaries using real local Git bundles."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from preloop.services.publication_worker import (
    freeze_publication,
    inspect_bundle,
    read_regular_file,
)
from preloop.services.trusted_publisher import PublicationError


@pytest.fixture
def candidate(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repository), *args], stderr=subprocess.DEVNULL, text=True
        ).strip()

    git("init")
    git("config", "user.name", "Local test")
    git("config", "user.email", "test@example.invalid")
    (repository / "initial.txt").write_text("base")
    git("add", ".")
    git("commit", "-m", "Base")
    base = git("rev-parse", "HEAD")
    (repository / "test with spaces.py").write_text("pass\n")
    git("add", ".")
    git("commit", "-m", "Implementation")
    source = tmp_path / "source"
    source.mkdir()
    git("bundle", "create", str(source / "branch.bundle"), "HEAD")
    return source, base, git("rev-parse", "HEAD"), git


def test_freeze_derives_real_commit_and_preserves_independent_bytes(
    candidate, tmp_path
):
    source, base, head, _ = candidate
    destination = tmp_path / "frozen"
    original = (source / "branch.bundle").read_bytes()
    manifest = freeze_publication(source, destination, base)
    assert manifest["head_sha"] == head
    assert manifest["changed_files"] == ["test with spaces.py"]
    assert manifest["bundle_sha256"] == hashlib.sha256(original).hexdigest()
    (source / "branch.bundle").write_bytes(b"agent rewrites source")
    assert (destination / "branch.bundle").read_bytes() == original
    assert (destination / "branch.bundle").stat().st_mode & 0o222 == 0


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "oversize"])
def test_fixed_input_rejects_filesystem_aliases_and_special_files(tmp_path, kind):
    target = tmp_path / "branch.bundle"
    other = tmp_path / "other"
    other.write_bytes(b"contents")
    if kind == "symlink":
        target.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"x" * 20)
    with pytest.raises((OSError, PublicationError)):
        read_regular_file(tmp_path, "branch.bundle", 10)


def test_freeze_rejects_missing_base_and_ambiguous_head(candidate, tmp_path):
    source, base, _, git = candidate
    bundle = (source / "branch.bundle").read_bytes()
    with pytest.raises(PublicationError):
        inspect_bundle(bundle, "a" * 40)
    git("bundle", "create", str(source / "branch.bundle"), "--all")
    with pytest.raises(PublicationError, match="exactly one HEAD"):
        freeze_publication(source, tmp_path / "frozen", base)


def test_freeze_never_reuses_existing_destination(candidate, tmp_path):
    source, base, _, _ = candidate
    destination = tmp_path / "frozen"
    destination.mkdir()
    (destination / "branch.bundle").write_bytes(b"previous attempt")
    with pytest.raises(FileExistsError):
        freeze_publication(source, destination, base)
    assert (destination / "branch.bundle").read_bytes() == b"previous attempt"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("PRELOOP_PUBLICATION_DOCKER_IMAGE"),
    reason="Explicit local Docker fixture image required",
)
async def test_real_docker_checks_run_fresh_without_credentials_and_fail_closed(
    candidate,
):
    from types import SimpleNamespace
    from uuid import uuid4
    from preloop.agents.container import ContainerAgentExecutor
    from preloop.services.publication_hosted_verifier import verify_hosted_publication
    from preloop.services.verification import resolve_verification_policy

    source, base, head, _ = candidate
    executor = ContainerAgentExecutor(
        "codex", {}, os.environ["PRELOOP_PUBLICATION_DOCKER_IMAGE"]
    )
    checks = [
        {
            "id": "first",
            "command": 'test -z "$GITHUB_TOKEN$PRELOOP_API_TOKEN" && test ! -e /var/run/docker.sock && echo changed > "test with spaces.py"',
            "reason": "isolation",
        },
        {
            "id": "second",
            "command": 'test "$(cat "test with spaces.py")" = pass',
            "reason": "fresh checkout",
        },
    ]
    policy = SimpleNamespace(
        execution_id=str(uuid4()),
        base_sha=base,
        verification_image=executor.image,
        verification_policy=resolve_verification_policy(
            {
                "verification": {
                    "mode": "gate",
                    "profile": {
                        "profile_id": "local",
                        "version": "v1",
                        "always": checks,
                    },
                }
            }
        ),
    )
    try:
        result = await verify_hosted_publication(
            executor, policy, (source / "branch.bundle").read_bytes()
        )
        assert result.verification.head_sha == head
        assert [check["exit_code"] for check in result.checks] == [0, 0]
        policy.verification_policy.profile.always[0].command = "exit 7"
        with pytest.raises(PublicationError, match="exit 7"):
            await verify_hosted_publication(
                executor, policy, (source / "branch.bundle").read_bytes()
            )
        docker = await executor._get_docker_client()
        residual = await docker.containers.list(
            all=True, filters={"label": [f"preloop.execution_id={policy.execution_id}"]}
        )
        assert residual == []
    finally:
        await executor.cleanup()


@pytest.mark.asyncio
async def test_publish_wire_fixture_rejects_digest_before_using_lease(candidate):
    import json
    from unittest.mock import AsyncMock, patch
    from preloop.services.publication_worker import publish_frozen

    source, _, _, _ = candidate
    request = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/publication_publish_request.json"
        ).read_text()
    )
    with (
        patch(
            "preloop.services.publication_worker.publish_verified_bundle",
            new=AsyncMock(),
        ) as publish,
        patch(
            "preloop.services.publication_worker.revoke_repository_lease",
            new=AsyncMock(),
        ) as revoke,
    ):
        with pytest.raises(PublicationError, match="digest changed"):
            await publish_frozen(source, request)
    publish.assert_not_awaited()
    revoke.assert_awaited_once()
    assert revoke.call_args.args[0].token == "fixture-test-token"


@pytest.mark.asyncio
async def test_publish_wire_fixture_binds_exact_bytes_and_revokes_on_success(candidate):
    import json
    from unittest.mock import AsyncMock, patch
    from preloop.services.publication_worker import publish_frozen

    source, _, head, _ = candidate
    request = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/publication_publish_request.json"
        ).read_text()
    )
    bundle = (source / "branch.bundle").read_bytes()
    request["bundle_sha256"] = hashlib.sha256(bundle).hexdigest()
    request["binding"]["head_sha"] = head
    request["binding"]["records"][0]["head_sha"] = head

    async def publish(**kwargs):
        assert kwargs["bundle"] == bundle
        assert kwargs["binding"].head_sha == head
        lease = await kwargs["acquire_lease"]()
        assert lease.token == "fixture-test-token"
        return {"url": "https://github.com/example/project/pull/1"}

    with (
        patch(
            "preloop.services.publication_worker.publish_verified_bundle", new=publish
        ),
        patch(
            "preloop.services.publication_worker.revoke_repository_lease",
            new=AsyncMock(),
        ) as revoke,
    ):
        result = await publish_frozen(source, request)
    assert result["url"].endswith("/pull/1")
    revoke.assert_awaited_once()
