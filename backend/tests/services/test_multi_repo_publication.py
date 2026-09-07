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


def _saved_flow() -> SimpleNamespace:
    """Explicit default saved policy for finish tests that do not use a Session."""
    return SimpleNamespace(
        account_id="account",
        git_clone_config={"publication_mode": "isolated"},
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
            flow=_saved_flow(),
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
            flow=_saved_flow(),
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
                flow=_saved_flow(),
            )
        assert first.value.receipt["status"] == "partial"
        recovered = await finish_multi_repo_isolated_publication(
            db=MagicMock(),
            policy=policy,
            agent_result={"result": {}},
            archive=archive,
            verify=verify,
            flow=_saved_flow(),
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
async def test_private_runner_prepares_product_topology(tracker: Any) -> None:
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
    responses: list[httpx.Response] = []
    for name in (
        "example/firmware",
        "example/companion-app",
        "example/product-compliance",
    ):
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

    async def mint(
        tracker_obj: Any, url: str, *, write: bool, client: Any
    ) -> PublicationLease:
        assert write is False
        return PublicationLease(
            f"read-{url}", url, datetime.now(timezone.utc) + timedelta(minutes=10)
        )

    with (
        patch(
            "preloop.services.runner_service.resolve_runner_pool",
            return_value="private-pool",
        ),
        patch(
            "preloop.services.private_publication.restore_private_publication",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.isolated_publication.crud_tracker.get_by_id_and_account",
            return_value=tracker,
        ),
        patch("preloop.services.isolated_publication.validate_publication_tracker"),
        patch(
            "preloop.services.isolated_publication.httpx.AsyncClient",
            return_value=MagicMock(
                __aenter__=AsyncMock(return_value=client),
                __aexit__=AsyncMock(return_value=None),
            ),
        ),
        patch(
            "preloop.services.isolated_publication.mint_repository_lease",
            new=AsyncMock(side_effect=mint),
        ),
        patch(
            "preloop.services.isolated_publication.revoke_repository_lease",
            new=AsyncMock(),
        ),
    ):
        policy = await prepare_isolated_publication(MagicMock(), flow, context)
    assert policy.private is True
    assert len(policy.targets) == 3
    assert {target.clone_path for target in policy.targets} == {
        "firmware",
        "companion-app",
        "compliance",
    }


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


def _verification_config() -> dict[str, Any]:
    return {
        "mode": "gate",
        "image": "toolchain@sha256:" + "a" * 64,
        "profile": {
            "profile_id": "test",
            "version": "v1",
            "always": [{"id": "check", "command": "true", "reason": "required"}],
        },
    }


def _bind_responses(*pairs: tuple[str, str, str]) -> list[httpx.Response]:
    """Repo info + base ref + missing publication branch, in bind order."""
    responses: list[httpx.Response] = []
    dummy = httpx.Request("GET", "https://api.github.com")
    for name, base_sha, _branch in pairs:
        responses.extend(
            [
                httpx.Response(
                    200,
                    json={"full_name": name, "default_branch": "main"},
                    request=dummy,
                ),
                httpx.Response(200, json={"object": {"sha": base_sha}}, request=dummy),
                httpx.Response(404, request=dummy),
            ]
        )
    return responses


def _resume_bind_responses(*pairs: tuple[str, str | None]) -> list[httpx.Response]:
    """Resume uses a stored base pin, so only repo info + publication branch."""
    responses: list[httpx.Response] = []
    dummy = httpx.Request("GET", "https://api.github.com")
    for name, expected in pairs:
        responses.append(
            httpx.Response(
                200,
                json={"full_name": name, "default_branch": "main"},
                request=dummy,
            )
        )
        if expected is None:
            responses.append(httpx.Response(404, request=dummy))
        else:
            responses.append(
                httpx.Response(200, json={"object": {"sha": expected}}, request=dummy)
            )
    return responses


def test_pinned_clone_does_not_follow_a_moving_branch() -> None:
    executor = ContainerAgentExecutor(agent_type="codex", config={}, image="test")
    pin = "a" * 40
    moved = "f" * 40
    commands = executor._build_repository_clone_command_block(
        repo_config={
            "repository_url": FIRMWARE,
            "clone_path": "firmware",
            "source_branch": "main",
            "target_branch": "preloop/flow-1",
            "pin_sha": pin,
            "commit": pin,
        },
        repo_index=0,
        execution_context={
            "git_clone_config": {"publication_mode": "isolated"},
            "git_credentials_map": {
                FIRMWARE: {
                    "token": "read-only",
                    "tracker_type": "github",
                    "permission": "read",
                }
            },
        },
        source_branch="main",
        target_branch="preloop/flow-1",
        commit_sha=moved,
        trigger_data={},
    )
    assert commands is not None
    script = "\n".join(commands)
    assert pin in script
    assert "git clone --branch main" not in script
    assert "A moving branch tip is not a verified checkout" in script
    assert (
        f"git checkout --force {pin}" in script
        or f"git checkout --force '{pin}'" in script
    )


def test_per_repo_clone_config_keeps_distinct_bases() -> None:
    executor = ContainerAgentExecutor(agent_type="codex", config={}, image="test")
    firmware_pin = "a" * 40
    app_pin = "b" * 40
    firmware = executor._build_repository_clone_command_block(
        repo_config={
            "repository_url": FIRMWARE,
            "clone_path": "firmware",
            "source_branch": "main",
            "target_branch": "preloop/flow-1",
            "pin_sha": firmware_pin,
        },
        repo_index=0,
        execution_context={"git_clone_config": {}, "git_credentials_map": {}},
        source_branch="main",
        target_branch="preloop/flow-1",
        commit_sha=None,
        trigger_data={},
    )
    app = executor._build_repository_clone_command_block(
        repo_config={
            "repository_url": APP,
            "clone_path": "companion-app",
            "source_branch": "release",
            "target_branch": "preloop/flow-1",
            "pin_sha": app_pin,
        },
        repo_index=1,
        execution_context={"git_clone_config": {}, "git_credentials_map": {}},
        source_branch="main",
        target_branch="preloop/flow-1",
        commit_sha=None,
        trigger_data={},
    )
    assert firmware is not None and app is not None
    firmware_script = "\n".join(firmware)
    app_script = "\n".join(app)
    assert firmware_pin in firmware_script
    assert app_pin in app_script
    assert firmware_pin not in app_script
    assert "release" not in firmware_script or app_pin in app_script


@pytest.mark.asyncio
async def test_prepare_pins_distinct_per_repo_bases(tracker: Any) -> None:
    from preloop.services.isolated_publication import prepare_isolated_publication

    tracker.id = "tracker"
    flow = SimpleNamespace(account_id="account", id="flow")
    firmware_sha, app_sha, compliance_sha = "a" * 40, "b" * 40, "c" * 40
    context = {
        "execution_id": EXECUTION,
        "git_clone_config": {
            "publication_mode": "isolated",
            "verification": _verification_config(),
            "repositories": [
                {
                    "repository_url": FIRMWARE,
                    "tracker_id": "tracker",
                    "clone_path": "firmware",
                    "source_branch": "main",
                },
                {
                    "repository_url": APP,
                    "tracker_id": "tracker",
                    "clone_path": "companion-app",
                    "source_branch": "release",
                },
                {
                    "repository_url": COMPLIANCE,
                    "tracker_id": "tracker",
                    "clone_path": "compliance",
                    "source_branch": "docs",
                },
            ],
        },
        "trigger_event_data": {},
    }
    client = AsyncMock()
    client.get.side_effect = _bind_responses(
        ("example/firmware", firmware_sha, "main"),
        ("example/companion-app", app_sha, "release"),
        ("example/product-compliance", compliance_sha, "docs"),
    )

    async def mint(
        tracker_obj: Any, url: str, *, write: bool, client: Any
    ) -> PublicationLease:
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
    assert [target.base for target in policy.targets] == ["main", "release", "docs"]
    assert [target.base_sha for target in policy.targets] == [
        firmware_sha,
        app_sha,
        compliance_sha,
    ]
    pins = {
        row["clone_path"]: row["pin_sha"]
        for row in context["git_clone_config"]["repositories"]
    }
    assert pins == {
        "firmware": firmware_sha,
        "companion-app": app_sha,
        "compliance": compliance_sha,
    }


@pytest.mark.asyncio
async def test_prepare_partial_publish_resume_does_not_duplicate_prs(
    tmp_path: Path, tracker: Any
) -> None:
    from preloop.services.isolated_publication import prepare_isolated_publication
    from preloop.services.multi_repo_publication import (
        IncompleteMultiRepoPublicationError,
        finish_multi_repo_isolated_publication,
    )

    tracker.id = "tracker"
    flow = SimpleNamespace(account_id="account", id="flow")
    _, fw_head, fw_bundle = _init_repo(tmp_path, "firmware", "fw")
    _, app_head, app_bundle = _init_repo(tmp_path, "app", "app")
    _, comp_head, comp_bundle = _init_repo(tmp_path, "compliance", "pack")
    archive = _archive(
        {"firmware": fw_bundle, "companion-app": app_bundle, "compliance": comp_bundle}
    )
    first_context = {
        "execution_id": EXECUTION,
        "git_clone_config": {
            "publication_mode": "isolated",
            "verification": _verification_config(),
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
    client = AsyncMock()
    client.get.side_effect = _bind_responses(
        ("example/firmware", fw_head, "main"),
        ("example/companion-app", app_head, "main"),
        ("example/product-compliance", comp_head, "main"),
    )

    async def mint(
        tracker_obj: Any, url: str, *, write: bool, client: Any
    ) -> PublicationLease:
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
        first_policy = await prepare_isolated_publication(
            MagicMock(), flow, first_context
        )

    created_prs: list[str] = []
    remote_heads: dict[str, str] = {}
    fail_compliance = True

    async def publisher(**kwargs: Any) -> dict[str, Any]:
        nonlocal fail_compliance
        binding = kwargs["binding"]
        url = binding.repository_url
        observed = remote_heads.get(url)
        if url == COMPLIANCE and fail_compliance:
            fail_compliance = False
            raise PublicationError("compliance remote rejected")
        if observed == binding.head_sha:
            slug = {
                FIRMWARE: "firmware",
                APP: "companion-app",
                COMPLIANCE: "product-compliance",
            }[url]
            return {
                "url": f"https://github.com/example/{slug}/pull/1",
                "number": 1,
                "branch": binding.branch,
                "provider": "github",
                "head_sha": binding.head_sha,
                "metadata_warnings": [],
            }
        if observed != binding.expected_remote_sha:
            raise PublicationError("remote compare-and-swap mismatch")
        if url in created_prs:
            raise AssertionError(f"duplicate pull request for {url}")
        created_prs.append(url)
        remote_heads[url] = binding.head_sha
        slug = {
            FIRMWARE: "firmware",
            APP: "companion-app",
            COMPLIANCE: "product-compliance",
        }[url]
        return {
            "url": f"https://github.com/example/{slug}/pull/1",
            "number": 1,
            "branch": binding.branch,
            "provider": "github",
            "head_sha": binding.head_sha,
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

    with (
        patch(
            "preloop.services.multi_repo_publication.crud_tracker.get_by_id_and_account",
            return_value=tracker,
        ),
        patch(
            "preloop.services.multi_repo_publication.publish_verified_bundle",
            new=AsyncMock(side_effect=publisher),
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
                policy=first_policy,
                agent_result={"result": {}},
                archive=archive,
                verify=verify,
                flow=_saved_flow(),
            )
    receipt = first.value.receipt
    assert receipt["complete"] is False
    assert created_prs == [FIRMWARE, APP]

    resume_execution = "22222222-2222-4222-8222-222222222222"
    prior = SimpleNamespace(flow_id="flow", result={"trusted_publication": receipt})
    resume_context = {
        "execution_id": resume_execution,
        "git_clone_config": first_context["git_clone_config"],
        "trigger_event_data": {"_resume": {"execution_id": EXECUTION}},
    }
    resume_client = AsyncMock()
    published_fw = next(
        row for row in receipt["repositories"] if row["repository_url"] == FIRMWARE
    )
    published_app = next(
        row for row in receipt["repositories"] if row["repository_url"] == APP
    )
    resume_client.get.side_effect = _resume_bind_responses(
        ("example/firmware", published_fw["head_sha"]),
        ("example/companion-app", published_app["head_sha"]),
        ("example/product-compliance", None),
    )
    with (
        patch("preloop.services.runner_service.resolve_runner_pool", return_value=None),
        patch(
            "preloop.services.isolated_publication.crud_tracker.get_by_id_and_account",
            return_value=tracker,
        ),
        patch("preloop.services.isolated_publication.validate_publication_tracker"),
        patch(
            "preloop.services.isolated_publication.crud_flow_execution.get",
            return_value=prior,
        ),
        patch(
            "preloop.services.isolated_publication.mint_repository_lease",
            new=AsyncMock(side_effect=mint),
        ),
        patch("preloop.services.isolated_publication.httpx.AsyncClient") as factory,
    ):
        factory.return_value.__aenter__.return_value = resume_client
        resumed = await prepare_isolated_publication(MagicMock(), flow, resume_context)
    by_url = {target.repository_url: target for target in resumed.targets}
    assert by_url[FIRMWARE].branch == published_fw["branch"]
    assert by_url[FIRMWARE].expected_remote_sha == published_fw["head_sha"]
    assert by_url[FIRMWARE].previous_records
    assert by_url[APP].expected_remote_sha == published_app["head_sha"]
    assert by_url[COMPLIANCE].expected_remote_sha is None

    async def verify_resume(policy: Any, bundle: bytes) -> SimpleNamespace:
        digest = hashlib.sha256(bundle).hexdigest()
        head = {
            hashlib.sha256(fw_bundle).hexdigest(): fw_head,
            hashlib.sha256(app_bundle).hexdigest(): app_head,
            hashlib.sha256(comp_bundle).hexdigest(): comp_head,
        }[digest]
        return SimpleNamespace(
            verification=VerifiedPublication(resume_execution, head, digest)
        )

    with (
        patch(
            "preloop.services.multi_repo_publication.crud_tracker.get_by_id_and_account",
            return_value=tracker,
        ),
        patch(
            "preloop.services.multi_repo_publication.publish_verified_bundle",
            new=AsyncMock(side_effect=publisher),
        ),
        patch(
            "preloop.services.multi_repo_publication.mint_repository_lease",
            new=AsyncMock(
                return_value=PublicationLease(
                    "write",
                    COMPLIANCE,
                    datetime.now(timezone.utc) + timedelta(minutes=10),
                )
            ),
        ),
        patch(
            "preloop.services.multi_repo_publication.revoke_repository_lease",
            new=AsyncMock(),
        ),
    ):
        recovered = await finish_multi_repo_isolated_publication(
            db=MagicMock(),
            policy=resumed,
            agent_result={"result": {}},
            archive=archive,
            verify=verify_resume,
            flow=_saved_flow(),
        )
    assert recovered["complete"] is True
    assert created_prs == [FIRMWARE, APP, COMPLIANCE]


@pytest.mark.asyncio
async def test_resume_rejects_topology_remap(tracker: Any) -> None:
    from preloop.services.isolated_publication import prepare_isolated_publication

    tracker.id = "tracker"
    flow = SimpleNamespace(account_id="account", id="flow")
    prior = SimpleNamespace(
        flow_id="flow",
        result={
            "trusted_publication": {
                "branch": "preloop/flow-11111111",
                "repositories": [
                    {
                        "repository_url": FIRMWARE,
                        "clone_path": "firmware",
                        "branch": "preloop/flow-11111111",
                        "base": "main",
                        "base_sha": "a" * 40,
                        "status": "published",
                    },
                    {
                        "repository_url": APP,
                        "clone_path": "companion-app",
                        "branch": "preloop/flow-11111111",
                        "base": "main",
                        "base_sha": "b" * 40,
                        "status": "failed",
                    },
                ],
            }
        },
    )
    context = {
        "execution_id": "22222222-2222-4222-8222-222222222222",
        "git_clone_config": {
            "publication_mode": "isolated",
            "verification": _verification_config(),
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
        "trigger_event_data": {"_resume": {"execution_id": EXECUTION}},
    }
    with (
        patch("preloop.services.runner_service.resolve_runner_pool", return_value=None),
        patch(
            "preloop.services.isolated_publication.crud_flow_execution.get",
            return_value=prior,
        ),
        patch(
            "preloop.services.isolated_publication.crud_tracker.get_by_id_and_account",
            return_value=tracker,
        ),
        patch("preloop.services.isolated_publication.validate_publication_tracker"),
    ):
        with pytest.raises(PublicationError, match="cannot add, remove, or remap"):
            await prepare_isolated_publication(MagicMock(), flow, context)


@pytest.mark.asyncio
async def test_named_recovery_persists_three_repo_archive_without_workspace(
    tmp_path: Path,
) -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    _, fw_head, fw_bundle = _init_repo(tmp_path, "firmware", "fw")
    _, app_head, app_bundle = _init_repo(tmp_path, "app", "app")
    _, comp_head, comp_bundle = _init_repo(tmp_path, "compliance", "pack")
    archive = _archive(
        {"firmware": fw_bundle, "companion-app": app_bundle, "compliance": comp_bundle}
    )
    targets = (
        _target(FIRMWARE, "firmware", base_sha=fw_head),
        _target(APP, "companion-app", base_sha=app_head),
        _target(COMPLIANCE, "compliance", role="compliance", base_sha=comp_head),
    )
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.execution_log = SimpleNamespace(id="execution")
    orchestrator.execution_logger = MagicMock()
    orchestrator._workspace_snapshot = None
    orchestrator._evidence_archive = archive
    orchestrator._isolated_publication_policy = _policy(targets)
    await orchestrator._persist_isolated_recovery()
    orchestrator.db.commit.assert_called_once()
    assert orchestrator.execution_log.evidence_archive == archive


@pytest.mark.asyncio
async def test_missing_named_bundle_keeps_runtime(tmp_path: Path) -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    _, fw_head, fw_bundle = _init_repo(tmp_path, "firmware", "fw")
    _, app_head, app_bundle = _init_repo(tmp_path, "app", "app")
    archive = _archive({"firmware": fw_bundle, "companion-app": app_bundle})
    targets = (
        _target(FIRMWARE, "firmware", base_sha=fw_head),
        _target(APP, "companion-app", base_sha=app_head),
        _target(COMPLIANCE, "compliance", role="compliance", base_sha="c" * 40),
    )
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.execution_log = SimpleNamespace(id="execution")
    orchestrator.execution_logger = MagicMock()
    orchestrator._workspace_snapshot = None
    orchestrator._evidence_archive = archive
    orchestrator._isolated_publication_policy = _policy(targets)
    with pytest.raises(PublicationError, match="missing a bundle"):
        await orchestrator._persist_isolated_recovery()
    orchestrator.db.commit.assert_not_called()


def test_private_descriptor_lists_named_targets() -> None:
    from preloop.services.private_publication import public_publication_descriptor

    targets = (
        _target(FIRMWARE, "firmware", base_sha="a" * 40),
        _target(APP, "companion-app", base_sha="b" * 40),
        _target(COMPLIANCE, "compliance", role="compliance", base_sha="c" * 40),
    )
    descriptor = public_publication_descriptor(
        {
            "nonce": "n" * 64,
            "phase": "agent",
            "policy": {
                "tracker_id": "tracker",
                "repository_url": FIRMWARE,
                "branch": "preloop/flow-11111111",
                "base": "main",
                "base_sha": "a" * 40,
                "expected_remote_sha": None,
                "verification_image": "img",
                "verification_policy": {"gate_budget_seconds": 30},
                "targets": [
                    {
                        "tracker_id": target.tracker_id,
                        "repository_url": target.repository_url,
                        "clone_path": target.clone_path,
                        "role": target.role,
                        "branch": target.branch,
                        "base": target.base,
                        "expected_remote_sha": target.expected_remote_sha,
                        "base_sha": target.base_sha,
                        "previous_records": [],
                    }
                    for target in targets
                ],
            },
        }
    )
    assert len(descriptor["targets"]) == 3
    assert [row["clone_path"] for row in descriptor["targets"]] == [
        "firmware",
        "companion-app",
        "compliance",
    ]
    assert "token" not in str(descriptor)
