"""Adversarial publication boundary and provider behavior tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from preloop.services.publication_verification import (
    VerifiedPublication,
    require_verified_publication,
)
from preloop.services.trusted_publisher import (
    CleanGitRepository,
    PublicationBinding,
    PublicationError,
    PublicationLease,
    PullRequestPublisher,
    publish_verified_bundle,
    read_publication_bundle,
)
from preloop.utils.pr_metadata import (
    PROVENANCE_START,
    PublicationRecord,
    discover_template,
    select_metadata,
    upsert_provenance,
)

EXECUTION = "11111111-1111-4111-8111-111111111111"
REPAIR = "22222222-2222-4222-8222-222222222222"
HEAD = "a" * 40
BASE = "b" * 40
PUBLIC_URL = "https://app.example.com"


def binding(**kwargs: Any) -> PublicationBinding:
    values = dict(
        repository_url="https://github.com/example/project.git",
        branch="preloop/issue-1",
        base="main",
        head_sha=HEAD,
        expected_remote_sha=None,
        records=(PublicationRecord(EXECUTION, HEAD),),
        public_url=PUBLIC_URL,
        provider="github",
    )
    values.update(kwargs)
    return PublicationBinding(**values)


def lease(
    repository_url: str = "https://github.com/example/project.git",
) -> PublicationLease:
    return PublicationLease(
        "test-write-secret",
        repository_url,
        datetime.now(timezone.utc) + timedelta(minutes=10),
    )


def archive_bundle(
    data: bytes, *, name: str = "evidence/branch.bundle", symlink: bool = False
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        if symlink:
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        else:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_metadata_precedence_and_unicode() -> None:
    title, body, warnings = select_metadata(
        json.dumps(
            {
                "pr_title": "Fix résumé 🚀",
                "pr_body": '## Summary\n\nPreserve "quotes".\n\n## Testing\n- [ ] UI unavailable',
            }
        ).encode(),
        configured_title="configured",
        configured_body="configured",
        issue_number="12",
    )
    assert title == "Fix résumé 🚀"
    assert "\n" in body and "Closes #12" in body
    assert "- [ ] UI unavailable" in body
    assert not warnings
    assert (
        select_metadata(
            b'{"pr_title": "Only title"}', configured_body="Configured body"
        )[1]
        == "Configured body"
    )
    assert select_metadata(
        b"broken", configured_title="configured", commit_body="commit"
    )[0:2] == ("configured", "commit")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        b"[]",
        b"false",
        b'"text"',
        b"\xff",
        b"{" * (256 * 1024 + 1),
        b'{"pr_title":42,"pr_body":{}}',
    ],
)
def test_invalid_metadata_falls_back_visibly(raw: bytes | None) -> None:
    title, body, warnings = select_metadata(raw, commit_title="Commit title")
    assert title == "Commit title"
    assert "## Summary" in body and "No test evidence supplied" in body
    assert warnings


@pytest.mark.parametrize(
    "bad", ["x" * 257, "title\nsecond line", "x\x00y", PROVENANCE_START + "pretend"]
)
def test_invalid_title_falls_back_without_truncating(bad: str) -> None:
    assert (
        select_metadata(
            json.dumps({"pr_title": bad}).encode(), configured_title="Valid"
        )[0]
        == "Valid"
    )


def test_provenance_all_sources_idempotent_and_preserves_human_edits() -> None:
    record = PublicationRecord(EXECUTION, HEAD)
    repaired = PublicationRecord(REPAIR, BASE)
    for raw, config, commit in [
        (b'{"pr_body":"Agent"}', "", ""),
        (None, "Configured", ""),
        (None, "", "Commit"),
    ]:
        _, body, _ = select_metadata(raw, configured_body=config, commit_body=commit)
        first = upsert_provenance(body, [record], PUBLIC_URL)
        edited = "Human prefix\n" + first + "\nHuman suffix 🚀\n"
        updated = upsert_provenance(edited, [record, repaired, repaired], PUBLIC_URL)
        assert updated.startswith("Human prefix\n" + body)
        assert updated.endswith("\nHuman suffix 🚀\n")
        assert updated.count(PROVENANCE_START) == 1
        assert updated.count(REPAIR) == 1
        assert updated == upsert_provenance(updated, [record, repaired], PUBLIC_URL)
        assert HEAD in updated and BASE in updated


def test_malformed_owned_block_fails_without_overwriting_humans() -> None:
    with pytest.raises(ValueError, match="Malformed"):
        upsert_provenance(
            "Human\n" + PROVENANCE_START,
            [PublicationRecord(EXECUTION, HEAD)],
            PUBLIC_URL,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://token@example.com",
        "https://example.com/?token=secret",
        "https://example.com/#secret",
        "javascript:evil",
        "https://example.com/)",
    ],
)
def test_execution_url_never_exposes_credentials(url: str) -> None:
    with pytest.raises(ValueError):
        upsert_provenance("Body", [PublicationRecord(EXECUTION, HEAD)], url)


def test_repository_templates_and_deterministic_selection() -> None:
    fixtures = Path(__file__).parents[1] / "fixtures/pr_templates"
    github = (fixtures / "github/pull_request_template.md").read_text()
    gitlab = (fixtures / "gitlab/Default.md").read_text()
    files = {
        ".github/pull_request_template.md": github,
        ".github/PULL_REQUEST_TEMPLATE/z.md": "Z",
        ".github/PULL_REQUEST_TEMPLATE/a.md": "A",
    }
    assert discover_template(files, provider="github") == (
        ".github/pull_request_template.md",
        github,
    )
    assert (
        discover_template(
            files, provider="github", configured=".github/PULL_REQUEST_TEMPLATE/z.md"
        )[1]
        == "Z"
    )
    del files[".github/pull_request_template.md"]
    assert discover_template(files, provider="github")[1] == "A"
    assert discover_template({}, provider="github") == (
        None,
        "## Summary\n\n## Testing\n",
    )
    assert (
        discover_template(
            {".gitlab/merge_request_templates/Default.md": gitlab}, provider="gitlab"
        )[1]
        == gitlab
    )
    assert "- [ ]" in github and "- [ ]" in gitlab
    with pytest.raises(ValueError):
        discover_template(files, provider="github", configured="../outside")


def test_verification_requires_controller_type_exact_execution_and_bundle() -> None:
    bundle = b"immutable bundle"
    envelope = VerifiedPublication(EXECUTION, HEAD, hashlib.sha256(bundle).hexdigest())
    assert (
        require_verified_publication(envelope, execution_id=EXECUTION, bundle=bundle)
        == HEAD
    )
    for value in [
        None,
        {"status": "passed", "head_sha": HEAD},
        replace(envelope, execution_id=REPAIR),
        replace(envelope, bundle_sha256="0" * 64),
    ]:
        with pytest.raises(PublicationError):
            require_verified_publication(value, execution_id=EXECUTION, bundle=bundle)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"repository_url": "https://evil.example/path.git"},
        {"repository_url": "https://secret@github.com/example/project.git"},
        {"repository_url": "ext::sh -c evil"},
        {"branch": "main"},
        {"branch": "-x"},
        {"branch": "a/../b"},
        {"head_sha": "short"},
        {"expected_remote_sha": "wrong"},
    ],
)
def test_forged_destinations_and_unsafe_bindings_rejected(
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(PublicationError):
        binding(**kwargs)


def test_archive_never_extracts_links_or_traversal(tmp_path: Path) -> None:
    assert read_publication_bundle(archive_bundle(b"bundle")) == b"bundle"
    for archive in [
        archive_bundle(b"", symlink=True),
        archive_bundle(b"bad", name="../../branch.bundle"),
        b"bad",
    ]:
        with pytest.raises(PublicationError):
            read_publication_bundle(archive)
    assert list(tmp_path.iterdir()) == []


def git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.defpath,
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "PRELOOP_DISABLE_TELEMETRY": "true",
    }
    return subprocess.run(
        ["/usr/bin/git", "-C", str(cwd), *args],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def malicious_checkout(tmp_path: Path) -> tuple[bytes, str, Path]:
    source = tmp_path / "agent"
    source.mkdir()
    git(source, "init", "--template=")
    git(source, "config", "user.name", "Test User")
    git(source, "config", "user.email", "test@example.com")
    (source / "file").write_text("safe content")
    (source / ".gitattributes").write_text("* filter=steal")
    git(source, "add", "file", ".gitattributes")
    git(source, "commit", "-m", "Verified change")
    head = git(source, "rev-parse", "HEAD")
    marker = tmp_path / "credential-extracted"
    malicious = f"touch {marker}"
    git(source, "config", "credential.helper", "!" + malicious)
    git(source, "config", "filter.steal.smudge", malicious)
    git(source, "config", "core.sshCommand", malicious)
    hooks = source / ".git/hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-push").write_text("#!/bin/sh\n" + malicious)
    (hooks / "pre-push").chmod(0o755)
    git(source, "bundle", "create", str(tmp_path / "agent.bundle"), "HEAD")
    return (tmp_path / "agent.bundle").read_bytes(), head, marker


def test_bundle_import_does_not_import_config_or_execute_checkout(
    malicious_checkout: tuple[bytes, str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, head, marker = malicious_checkout
    monkeypatch.setenv("PRELOOP_GIT_TOKEN_1", "old-agent-token")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!touch " + str(marker))
    clean = tmp_path / "clean"
    clean.mkdir()
    repo = CleanGitRepository(clean)
    repo.import_bundle(bundle, head)
    assert "PRELOOP_GIT_TOKEN_1" not in repo.environment
    assert repo.environment["GIT_CONFIG_COUNT"] == "0"
    assert "credential" not in (clean / "config").read_text()
    assert not (clean / "file").exists()
    assert not marker.exists()
    with pytest.raises(PublicationError):
        repo.import_bundle(bundle, "f" * 40)


def test_publication_rejects_concurrent_remote_and_non_fast_forward(
    tmp_path: Path,
) -> None:
    repo = CleanGitRepository(tmp_path)
    with patch.object(
        repo, "run", return_value=BASE + "\trefs/heads/preloop/issue-1"
    ) as run:
        with pytest.raises(PublicationError, match="concurrently"):
            repo.publish(binding(), lease())
        assert not any(call.args[0] == "push" for call in run.call_args_list)
    with patch.object(
        repo, "run", side_effect=[BASE + "\tref", "", BASE, "c" * 40]
    ) as run:
        with pytest.raises(PublicationError, match="Non-fast-forward"):
            repo.publish(binding(expected_remote_sha=BASE), lease())
        assert not any(call.args[0] == "push" for call in run.call_args_list)


def test_publication_retry_skips_push_and_wrong_lease_rejected(tmp_path: Path) -> None:
    repo = CleanGitRepository(tmp_path)
    with patch.object(repo, "run", return_value=HEAD + "\tref") as run:
        repo.publish(binding(), lease())
        assert run.call_count == 1
    with pytest.raises(PublicationError, match="bound"):
        repo.publish(binding(), lease("https://github.com/example/other.git"))
    with pytest.raises(PublicationError, match="expired"):
        repo.publish(binding(), replace(lease(), expires_at=datetime.now(timezone.utc)))


def provider_row(provider: str, body: str) -> dict[str, Any]:
    if provider == "github":
        return {
            "number": 5,
            "html_url": "https://github.com/example/project/pull/5",
            "body": body,
            "head": {
                "ref": "preloop/issue-1",
                "repo": {"full_name": "example/project"},
            },
            "base": {"ref": "main"},
        }
    return {
        "iid": 5,
        "web_url": "https://gitlab.example.com/example/project/-/merge_requests/5",
        "description": body,
        "source_branch": "preloop/issue-1",
        "target_branch": "main",
        "source_project_id": 10,
        "target_project_id": 10,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_provider_create_retry_metadata_update_preserves_human_edits(
    provider: str,
) -> None:
    url = (
        "https://github.com/example/project.git"
        if provider == "github"
        else "https://gitlab.example.com/example/project.git"
    )
    contract = binding(provider=provider, repository_url=url)
    calls: list[httpx.Request] = []
    row: dict[str, Any] | None = None
    field = "body" if provider == "github" else "description"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal row
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[row] if row else [])
        payload = json.loads(request.content)
        if request.method == "POST":
            assert payload["title"] == "Fix résumé"
            assert (
                payload["head" if provider == "github" else "source_branch"]
                == contract.branch
            )
            row = provider_row(provider, payload[field])
            return httpx.Response(201, json=row)
        assert list(payload) == [field]
        assert row is not None
        row[field] = payload[field]
        return httpx.Response(200, json=row)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = PullRequestPublisher(client)
        first = await publisher.upsert(
            contract, lease(url), "Fix résumé", "## Summary\nInitial"
        )
        assert first["number"] == 5
        assert row is not None
        row[field] = "Human before\n" + row[field] + "\nHuman after"
        repaired = replace(
            contract, records=(*contract.records, PublicationRecord(REPAIR, HEAD))
        )
        await publisher.upsert(
            repaired, lease(url), "Ignored agent rewrite", "Overwrite human body"
        )
        await publisher.upsert(repaired, lease(url), "Retry", "Retry")
        assert row[field].startswith("Human before\n## Summary\nInitial")
        assert row[field].endswith("\nHuman after")
        assert row[field].count(REPAIR) == 1
        assert sum(call.method == "POST" for call in calls) == 1
        assert sum(call.method in {"PATCH", "PUT"} for call in calls) == 1


@pytest.mark.asyncio
async def test_provider_failure_is_observable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, json=[provider_row("github", "Human body")])
            if request.method == "GET"
            else httpx.Response(503)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PublicationError, match="provider request failed"):
            await PullRequestPublisher(client).upsert(
                binding(), lease(), "Title", "Body"
            )


@pytest.mark.asyncio
async def test_invalid_bundle_never_acquires_write_credentials() -> None:
    acquire = AsyncMock()
    async with httpx.AsyncClient() as client:
        with pytest.raises(PublicationError):
            await publish_verified_bundle(
                binding=binding(),
                bundle=b"bad bundle",
                result_json=b"{}",
                acquire_lease=acquire,
                client=client,
            )
    acquire.assert_not_awaited()


def test_repository_template_reader_rejects_symlink_escape(tmp_path: Path) -> None:
    from preloop.utils.pr_metadata import repository_template

    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "private"
    outside.write_text("secret")
    (repository / "template.md").symlink_to(outside)
    with pytest.raises(ValueError, match="missing or outside"):
        repository_template(repository, provider="github", configured="template.md")
