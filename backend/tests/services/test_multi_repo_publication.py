"""Isolated multi-repo publication: local git bundles, mocked provider."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from preloop.agents.container import ContainerAgentExecutor
from preloop.services.isolated_publication import IsolatedPublicationPolicy
from preloop.services.multi_repo_publication import (
    IncompleteMultiRepoPublicationError,
    IsolatedPublicationTarget,
    aggregate_publication_receipts,
    authorize_publication_target,
    finish_multi_repo_isolated_publication,
    published_receipt,
    read_named_publication_bundles,
)
from preloop.services.publication_verification import VerifiedPublication
from preloop.services.trusted_publisher import PublicationError, PublicationLease


@pytest.fixture
def tracker() -> Any:
    return SimpleNamespace(
        tracker_type="github",
        auth_type="github_app",
        oauth_installation=SimpleNamespace(external_id="123"),
    )


EXECUTION = "11111111-1111-4111-8111-111111111111"
FIRMWARE = "https://github.com/example/firmware.git"
APP = "https://github.com/example/companion-app.git"
COMPLIANCE = "https://github.com/example/product-compliance.git"
OUTSIDER = "https://github.com/example/outsider.git"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        stderr=subprocess.DEVNULL,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    ).strip()


def _init_repo(root: Path, name: str, content: str) -> tuple[Path, str, bytes]:
    repo = root / name
    repo.mkdir()
    subprocess.check_call(
        ["git", "init", "-b", "main"],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"base {name}")
    _git(repo, "rev-parse", "HEAD")
    (repo / ".preloop").mkdir()
    (repo / ".preloop" / "stub.json").write_text("{}")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"evidence {name}")
    head = _git(repo, "rev-parse", "HEAD")
    bundle = repo / "branch.bundle"
    _git(repo, "bundle", "create", str(bundle), "HEAD")
    return repo, head, bundle.read_bytes()


def _archive(bundles: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for slug, data in bundles.items():
            info = tarfile.TarInfo(f"evidence/repos/{slug}/branch.bundle")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _target(
    url: str,
    slug: str,
    *,
    role: str = "code",
    base_sha: str = "b" * 40,
    expected: str | None = None,
) -> IsolatedPublicationTarget:
    return IsolatedPublicationTarget(
        tracker_id="tracker",
        repository_url=url,
        clone_path=slug,
        role=role,
        branch="preloop/flow-11111111",
        base="main",
        expected_remote_sha=expected,
        base_sha=base_sha,
    )


def _policy(
    targets: tuple[IsolatedPublicationTarget, ...],
) -> IsolatedPublicationPolicy:
    primary = targets[0]
    return IsolatedPublicationPolicy(
        primary.tracker_id,
        "account",
        primary.repository_url,
        primary.branch,
        primary.base,
        primary.expected_remote_sha,
        EXECUTION,
        (),
        None,
        "title",
        "body",
        "",
        primary.base_sha,
        SimpleNamespace(mode="gate", profile=object(), gate_budget_seconds=30),  # type: ignore[arg-type]
        "toolchain@sha256:" + "a" * 64,
        False,
        "n" * 64,
        targets,
        (),
    )


def test_cross_repo_authorization_rejects_outsider() -> None:
    target = _target(OUTSIDER, "outsider")
    with pytest.raises(PublicationError, match="outside the authorized"):
        authorize_publication_target(
            target,
            authorized_remotes={FIRMWARE: "account", APP: "account"},
            account_id="account",
        )


def test_named_archive_rejects_bundle_outside_manifest(tmp_path: Path) -> None:
    _, _, firmware_bundle = _init_repo(tmp_path, "firmware", "fw")
    _, _, extra_bundle = _init_repo(tmp_path, "outsider", "nope")
    archive = _archive({"firmware": firmware_bundle, "outsider": extra_bundle})
    with pytest.raises(PublicationError, match="outside the authorized"):
        read_named_publication_bundles(archive, (_target(FIRMWARE, "firmware"),))


def test_partial_remote_failure_is_incomplete_not_success(tmp_path: Path) -> None:
    receipts = aggregate_publication_receipts(
        [
            published_receipt(
                _target(FIRMWARE, "firmware"),
                {
                    "url": "https://github.com/example/firmware/pull/1",
                    "number": 1,
                    "branch": "preloop/flow-11111111",
                    "provider": "github",
                    "head_sha": "a" * 40,
                },
            ),
            {
                "repository_url": APP,
                "clone_path": "companion-app",
                "role": "code",
                "status": "failed",
                "url": None,
                "head_sha": None,
                "error": "provider unavailable",
            },
            published_receipt(
                _target(COMPLIANCE, "compliance", role="compliance"),
                {
                    "url": "https://github.com/example/product-compliance/pull/1",
                    "number": 1,
                    "branch": "preloop/flow-11111111",
                    "provider": "github",
                    "head_sha": "c" * 40,
                },
            ),
        ]
    )
    assert receipts["complete"] is False
    assert receipts["status"] == "partial"
    assert "url" not in receipts
    published = [
        row for row in receipts["repositories"] if row["status"] == "published"
    ]
    assert len(published) == 2


@pytest.mark.asyncio
async def test_successful_receipt_aggregation_and_idempotent_retry(
    tmp_path: Path,
) -> None:
    _, fw_head, fw_bundle = _init_repo(tmp_path, "firmware", "fw")
    _, app_head, app_bundle = _init_repo(tmp_path, "app", "app")
    _, comp_head, comp_bundle = _init_repo(tmp_path, "compliance", "pack")
    targets = (
        _target(FIRMWARE, "firmware", base_sha=fw_head),
        _target(APP, "companion-app", base_sha=app_head),
        _target(COMPLIANCE, "compliance", role="compliance", base_sha=comp_head),
    )
    archive = _archive(
        {
            "firmware": fw_bundle,
            "companion-app": app_bundle,
            "compliance": comp_bundle,
        }
    )
    published: list[str] = []

    async def fake_publish(**kwargs: Any) -> dict[str, Any]:
        binding = kwargs["binding"]
        url = binding.repository_url
        if url in published:
            raise AssertionError("retry duplicated a successful publish")
        published.append(url)
        head = binding.head_sha
        slug = {
            FIRMWARE: "firmware",
            APP: "companion-app",
            COMPLIANCE: "compliance",
        }[url]
        return {
            "url": f"https://github.com/example/{slug}/pull/1",
            "number": 1,
            "branch": binding.branch,
            "provider": "github",
            "head_sha": head,
            "metadata_warnings": [],
        }

    async def verify(policy: Any, bundle: bytes) -> SimpleNamespace:
        digest = hashlib.sha256(bundle).hexdigest()
        head = {
            hashlib.sha256(fw_bundle).hexdigest(): fw_head,
            hashlib.sha256(app_bundle).hexdigest(): app_head,
            hashlib.sha256(comp_bundle).hexdigest(): comp_head,
        }[digest]
        return SimpleNamespace(
            verification=VerifiedPublication(EXECUTION, head, digest)
        )

    policy = _policy(targets)
    tracker = SimpleNamespace(id="tracker")
    with (
        patch(
            "preloop.services.multi_repo_publication.crud_tracker.get_by_id_and_account",
            return_value=tracker,
        ),
        patch(
            "preloop.services.multi_repo_publication.publish_verified_bundle",
            new=AsyncMock(side_effect=fake_publish),
        ),
        patch(
            "preloop.services.multi_repo_publication.mint_repository_lease",
            new=AsyncMock(
                return_value=PublicationLease(
                    "write",
                    FIRMWARE,
                    datetime.now(timezone.utc) + timedelta(minutes=10),
                )
            ),
        ),
        patch(
            "preloop.services.multi_repo_publication.revoke_repository_lease",
            new=AsyncMock(),
        ),
    ):
        first = await finish_multi_repo_isolated_publication(
            db=MagicMock(),
            policy=policy,
            agent_result={"result": {"verdict": "pass"}},
            archive=archive,
            verify=verify,
        )
        assert first["complete"] is True
        assert len(first["repositories"]) == 3
        assert {row["clone_path"] for row in first["repositories"]} == {
            "firmware",
            "companion-app",
            "compliance",
        }
        # Idempotent retry: publisher treats already-published remotes as
        # success without a second push. Simulate by clearing the guard and
        # making publish_verified_bundle return the same receipt again.
        published.clear()
        second = await finish_multi_repo_isolated_publication(
            db=MagicMock(),
            policy=policy,
            agent_result={"result": {"verdict": "pass"}},
            archive=archive,
            verify=verify,
        )
    assert second["complete"] is True
    assert second["repositories"][0]["url"] == first["repositories"][0]["url"]


@pytest.mark.asyncio
async def test_partial_failure_then_retry_recovers_remaining(
    tmp_path: Path,
) -> None:
    _, fw_head, fw_bundle = _init_repo(tmp_path, "firmware", "fw")
    _, app_head, app_bundle = _init_repo(tmp_path, "app", "app")
    _, comp_head, comp_bundle = _init_repo(tmp_path, "compliance", "pack")
    targets = (
        _target(FIRMWARE, "firmware", base_sha=fw_head),
        _target(APP, "companion-app", base_sha=app_head),
        _target(COMPLIANCE, "compliance", role="compliance", base_sha=comp_head),
    )
    archive = _archive(
        {
            "firmware": fw_bundle,
            "companion-app": app_bundle,
            "compliance": comp_bundle,
        }
    )
    attempts: dict[str, int] = {}

    async def fake_publish(**kwargs: Any) -> dict[str, Any]:
        url = kwargs["binding"].repository_url
        attempts[url] = attempts.get(url, 0) + 1
        if url == APP and attempts[url] == 1:
            raise PublicationError("provider unavailable")
        slug = {
            FIRMWARE: "firmware",
            APP: "companion-app",
            COMPLIANCE: "compliance",
        }[url]
        return {
            "url": f"https://github.com/example/{slug}/pull/1",
            "number": 1,
            "branch": kwargs["binding"].branch,
            "provider": "github",
            "head_sha": kwargs["binding"].head_sha,
            "metadata_warnings": [],
        }

    async def verify(policy: Any, bundle: bytes) -> SimpleNamespace:
        digest = hashlib.sha256(bundle).hexdigest()
        head = {
            hashlib.sha256(fw_bundle).hexdigest(): fw_head,
            hashlib.sha256(app_bundle).hexdigest(): app_head,
            hashlib.sha256(comp_bundle).hexdigest(): comp_head,
        }[digest]
        return SimpleNamespace(
            verification=VerifiedPublication(EXECUTION, head, digest)
        )

    policy = _policy(targets)
    with (
        patch(
            "preloop.services.multi_repo_publication.crud_tracker.get_by_id_and_account",
            return_value=SimpleNamespace(id="tracker"),
        ),
        patch(
            "preloop.services.multi_repo_publication.publish_verified_bundle",
            new=AsyncMock(side_effect=fake_publish),
        ),
        patch(
            "preloop.services.multi_repo_publication.mint_repository_lease",
            new=AsyncMock(
                return_value=PublicationLease(
                    "write",
                    FIRMWARE,
                    datetime.now(timezone.utc) + timedelta(minutes=10),
                )
            ),
        ),
        patch(
            "preloop.services.multi_repo_publication.revoke_repository_lease",
            new=AsyncMock(),
        ),
    ):
        with pytest.raises(IncompleteMultiRepoPublicationError) as first:
            await finish_multi_repo_isolated_publication(
                db=MagicMock(),
                policy=policy,
                agent_result={"result": {}},
                archive=archive,
                verify=verify,
            )
        assert first.value.receipt["status"] == "partial"
        recovered = await finish_multi_repo_isolated_publication(
            db=MagicMock(),
            policy=policy,
            agent_result={"result": {}},
            archive=archive,
            verify=verify,
        )
    assert recovered["complete"] is True
    assert attempts[APP] == 2
    assert attempts[FIRMWARE] == 2


def test_local_commit_is_not_a_publication_success() -> None:
    receipt = aggregate_publication_receipts(
        [
            {
                "repository_url": FIRMWARE,
                "clone_path": "firmware",
                "role": "code",
                "status": "failed",
                "url": None,
                "head_sha": None,
                "error": "remote rejected",
                "local_commit": "a" * 40,
            }
        ]
    )
    assert receipt["complete"] is False
    assert receipt["status"] == "failed"


def test_container_exports_per_repo_bundles_without_push() -> None:
    executor = ContainerAgentExecutor(agent_type="codex", config={}, image="test")
    context = {
        "git_clone_config": {
            "publication_mode": "isolated",
            "repositories": [
                {
                    "tracker_id": "tracker",
                    "repository_url": FIRMWARE,
                    "clone_path": "firmware",
                },
                {
                    "tracker_id": "tracker",
                    "repository_url": APP,
                    "clone_path": "companion-app",
                },
                {
                    "tracker_id": "tracker",
                    "repository_url": COMPLIANCE,
                    "clone_path": "compliance",
                },
            ],
        },
        "git_credentials_map": {
            FIRMWARE: {
                "token": "read-lease-fw",
                "tracker_type": "github",
                "permission": "read",
            },
            APP: {
                "token": "read-lease-app",
                "tracker_type": "github",
                "permission": "read",
            },
            COMPLIANCE: {
                "token": "read-lease-comp",
                "tracker_type": "github",
                "permission": "read",
            },
        },
        "_git_target_branch": "preloop/flow-1",
        "_git_source_branch": "main",
    }
    script = executor._prepare_git_post_execution_commands(context)
    assert "repos/firmware/branch.bundle" in script
    assert "repos/companion-app/branch.bundle" in script
    assert "repos/compliance/branch.bundle" in script
    assert "git push" not in script
    assert "curl" not in script


@pytest.mark.asyncio
async def test_private_runner_still_rejects_product_topology(tracker: Any) -> None:
    from preloop.services.isolated_publication import prepare_isolated_publication

    tracker.id = "tracker"
    flow = SimpleNamespace(account_id="account", id="flow")
    context = {
        "execution_id": EXECUTION,
        "git_clone_config": {
            "publication_mode": "isolated",
            "verification": {
                "mode": "gate",
                "image": "toolchain@sha256:" + "a" * 64,
                "profile": {
                    "profile_id": "test",
                    "version": "v1",
                    "always": [
                        {"id": "check", "command": "true", "reason": "required"}
                    ],
                },
            },
            "repositories": [
                {
                    "repository_url": FIRMWARE,
                    "tracker_id": "tracker",
                    "clone_path": "firmware",
                },
                {
                    "repository_url": COMPLIANCE,
                    "tracker_id": "tracker",
                    "clone_path": "compliance",
                },
            ],
        },
        "trigger_event_data": {},
    }
    with (
        patch(
            "preloop.services.runner_service.resolve_runner_pool",
            return_value="private-pool",
        ),
        patch(
            "preloop.services.private_publication.restore_private_publication",
            new=AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(PublicationError, match="private-runner"):
            await prepare_isolated_publication(MagicMock(), flow, context)


@pytest.mark.asyncio
async def test_prepare_hosted_multi_repo_mints_per_repo_read_leases(
    tracker: Any,
) -> None:
    from preloop.services.isolated_publication import prepare_isolated_publication

    tracker.id = "tracker"
    flow = SimpleNamespace(account_id="account", id="flow")
    context = {
        "execution_id": EXECUTION,
        "git_clone_config": {
            "publication_mode": "isolated",
            "verification": {
                "mode": "gate",
                "image": "toolchain@sha256:" + "a" * 64,
                "profile": {
                    "profile_id": "test",
                    "version": "v1",
                    "always": [
                        {"id": "check", "command": "true", "reason": "required"}
                    ],
                },
            },
            "repositories": [
                {
                    "repository_url": FIRMWARE,
                    "tracker_id": "tracker",
                    "clone_path": "firmware",
                },
                {
                    "repository_url": APP,
                    "tracker_id": "tracker",
                    "clone_path": "companion-app",
                },
                {
                    "repository_url": COMPLIANCE,
                    "tracker_id": "tracker",
                    "clone_path": "compliance",
                },
            ],
        },
        "trigger_event_data": {},
    }
    read = PublicationLease(
        "read-only",
        FIRMWARE,
        datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    repo_info = {
        FIRMWARE: "example/firmware",
        APP: "example/companion-app",
        COMPLIANCE: "example/product-compliance",
    }
    responses: list[httpx.Response] = []
    for url in (FIRMWARE, APP, COMPLIANCE):
        name = repo_info[url]
        responses.extend(
            [
                httpx.Response(
                    200,
                    json={"full_name": name, "default_branch": "main"},
                    request=httpx.Request("GET", "https://api.github.com"),
                ),
                httpx.Response(
                    200,
                    json={"object": {"sha": "b" * 40}},
                    request=httpx.Request("GET", "https://api.github.com"),
                ),
                httpx.Response(
                    404, request=httpx.Request("GET", "https://api.github.com")
                ),
            ]
        )
    client = AsyncMock()
    client.get.side_effect = responses
    minted: list[str] = []

    async def mint(
        tracker_obj: Any, url: str, *, write: bool, client: Any
    ) -> PublicationLease:
        minted.append(url)
        assert write is False
        return PublicationLease(
            f"read-{url}", url, datetime.now(timezone.utc) + timedelta(minutes=10)
        )

    with (
        patch("preloop.services.runner_service.resolve_runner_pool", return_value=None),
        patch(
            "preloop.services.isolated_publication.crud_tracker.get_by_id_and_account",
            return_value=tracker,
        ),
        patch("preloop.services.isolated_publication.validate_publication_tracker"),
        patch(
            "preloop.services.isolated_publication.mint_repository_lease",
            new=AsyncMock(side_effect=mint),
        ),
        patch("preloop.services.isolated_publication.httpx.AsyncClient") as factory,
    ):
        factory.return_value.__aenter__.return_value = client
        policy = await prepare_isolated_publication(MagicMock(), flow, context)
    assert minted == [FIRMWARE, APP, COMPLIANCE]
    assert len(policy.targets) == 3
    assert policy.targets[2].role == "compliance"
    assert context["git_credentials_map"][FIRMWARE]["permission"] == "read"
    assert "write" not in str(context["git_credentials_map"])
