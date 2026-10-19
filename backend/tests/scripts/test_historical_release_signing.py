"""Exact-byte historical endorsements cannot rebuild or replace release assets."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "historical_release_signing", ROOT / "scripts/historical_release_signing.py"
)
assert SPEC is not None and SPEC.loader is not None
signing = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = signing
SPEC.loader.exec_module(signing)


@pytest.fixture
def release_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Synthetic release bytes and metadata; never contact a service."""
    payload = b"historical binary bytes\x00\xff"
    digest = hashlib.sha256(payload).hexdigest()
    checksums = f"{digest}  payload.bin\n".encode()
    contents = {11: payload, 12: checksums}
    snapshot = {
        "id": 101,
        "tag": "v1.0.0",
        "published_at": "2025-01-01T00:00:00Z",
        "tag_commit": "a" * 40,
        "immutable": False,
        "assets": [
            {
                "id": asset_id,
                "name": name,
                "size": len(contents[asset_id]),
                "api_digest": "sha256:"
                + hashlib.sha256(contents[asset_id]).hexdigest(),
                "created_at": "2025-01-01T00:00:00Z",
                "updated_at": "2025-01-01T00:00:00Z",
            }
            for asset_id, name in [(11, "payload.bin"), (12, "SHA256SUMS")]
        ],
    }
    monkeypatch.setattr(signing, "SELECTED_RELEASES", {"v1.0.0": 101})
    monkeypatch.setattr(signing, "snapshot_release", lambda *_: copy.deepcopy(snapshot))

    def download(asset_id: int, path: Path) -> None:
        path.write_bytes(contents[asset_id])

    monkeypatch.setattr(signing, "download_asset", download)
    return {
        "snapshot": snapshot,
        "contents": contents,
        "directory": tmp_path / "assets",
        "manifest": tmp_path / "inventory.json",
    }


def prepare(fixture: dict) -> dict:
    """Prepare an inventory with verified synthetic downloads."""
    return signing.prepare_inventory(fixture["directory"])


def test_prepare_downloads_and_hashes_every_original_asset(
    release_fixture: dict,
) -> None:
    inventory = prepare(release_fixture)
    assets = inventory["releases"][0]["assets"]
    assert {asset["name"] for asset in assets} == {"payload.bin", "SHA256SUMS"}
    for asset in assets:
        assert (
            asset["sha256"]
            == hashlib.sha256(release_fixture["contents"][asset["id"]]).hexdigest()
        )
    assert inventory["releases"][0]["checksum_coverage"]["covered"] == ["payload.bin"]


def test_older_release_without_manifest_still_covers_every_byte(
    release_fixture: dict,
) -> None:
    release_fixture["snapshot"]["assets"].pop()
    inventory = prepare(release_fixture)
    assert inventory["releases"][0]["checksum_coverage"] == {
        "covered": [],
        "uncovered": ["payload.bin"],
    }
    assert inventory["releases"][0]["assets"][0]["sha256"]


@pytest.mark.parametrize(
    "field,value", [("size", 999), ("api_digest", "sha256:" + "0" * 64)]
)
def test_download_hash_or_size_mismatch_fails(
    release_fixture: dict, field: str, value: object
) -> None:
    release_fixture["snapshot"]["assets"][0][field] = value
    with pytest.raises(ValueError, match="mismatch"):
        prepare(release_fixture)


def test_checksum_payload_mismatch_fails_even_without_api_digests(
    release_fixture: dict,
) -> None:
    release_fixture["contents"][11] = b"changed historical bytes"
    asset = release_fixture["snapshot"]["assets"][0]
    asset["size"] = len(release_fixture["contents"][11])
    asset["api_digest"] = None
    with pytest.raises(ValueError, match="Checksum mismatch"):
        prepare(release_fixture)


@pytest.mark.parametrize("name", ["../payload", "a/b", "a\\b", "a\nb", ".", "-option"])
def test_untrusted_asset_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="asset name"):
        signing.validate_asset_name(name)


def test_symlink_is_not_hashed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="regular"):
        signing.sha256_file(link)


def test_changed_release_or_tag_aborts_before_signing(
    release_fixture: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = prepare(release_fixture)
    release_fixture["snapshot"]["tag_commit"] = "b" * 40
    run = Mock()
    monkeypatch.setattr(signing.subprocess, "run", run)
    with pytest.raises(ValueError, match="changed"):
        signing.publish_endorsements(
            inventory,
            release_fixture["directory"],
            "c" * 40,
            "https://github.com/example/project/actions/runs/1",
        )
    run.assert_not_called()


def test_tampered_download_aborts_before_signing(
    release_fixture: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = prepare(release_fixture)
    (release_fixture["directory"] / "v1.0.0" / "payload.bin").write_bytes(b"tampered")
    run = Mock()
    monkeypatch.setattr(signing.subprocess, "run", run)
    with pytest.raises(ValueError, match="mismatch"):
        signing.publish_endorsements(
            inventory,
            release_fixture["directory"],
            "c" * 40,
            "https://github.com/example/project/actions/runs/1",
        )
    run.assert_not_called()


def test_inventory_approval_digest_is_checked_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "inventory.json"
    manifest.write_text("{}\n")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "signing.py",
            "publish",
            "--directory",
            str(tmp_path / "assets"),
            "--manifest",
            str(manifest),
            "--manifest-sha256",
            "0" * 64,
        ],
    )
    prepare_mock = Mock()
    monkeypatch.setattr(signing, "prepare_inventory", prepare_mock)
    with pytest.raises(ValueError, match="approved inventory"):
        signing.main()
    prepare_mock.assert_not_called()


def test_dry_run_never_signs_or_uploads(
    release_fixture: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "signing.py",
            "prepare",
            "--directory",
            str(release_fixture["directory"]),
            "--manifest",
            str(release_fixture["manifest"]),
        ],
    )
    run = Mock(side_effect=AssertionError("unexpected mutation"))
    monkeypatch.setattr(signing.subprocess, "run", run)
    assert signing.main() == 0
    assert (
        json.loads(release_fixture["manifest"].read_text())["releases"][0]["tag"]
        == "v1.0.0"
    )
    run.assert_not_called()


def test_publish_uses_detached_bundle_strict_identity_and_no_clobber(
    release_fixture: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = prepare(release_fixture)
    commands: list[list[str]] = []
    attached: dict[str, bytes] = {}
    monkeypatch.setattr(signing, "existing_endorsements", lambda *_: attached)

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args[:2] == ["cosign", "sign-blob"]:
            Path(args[args.index("--bundle") + 1]).write_bytes(b"synthetic bundle")
        if args[:3] == ["gh", "release", "upload"]:
            for path in args[args.index("--repo") + 2 :]:
                attached[Path(path).name] = Path(path).read_bytes()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(signing.subprocess, "run", run)
    monkeypatch.setattr(
        signing,
        "download_endorsement",
        lambda _tag, name, path: path.write_bytes(attached[name]),
    )
    signing.publish_endorsements(
        inventory,
        release_fixture["directory"],
        "c" * 40,
        "https://github.com/example/project/actions/runs/1",
    )
    assert len(attached) == 2
    assert any(name.endswith(".sigstore") for name in attached)
    document = json.loads(
        next(content for name, content in attached.items() if name.endswith(".json"))
    )
    assert document["kind"] == "historical-artifact-endorsement"
    assert "not original build provenance" in document["statement"]
    assert {asset["name"] for asset in document["release"]["assets"]} == {
        "payload.bin",
        "SHA256SUMS",
    }
    verify = [
        command for command in commands if command[:2] == ["cosign", "verify-blob"]
    ]
    assert len(verify) == 2
    for command in verify:
        assert (
            command[command.index("--certificate-identity") + 1]
            == signing.CERTIFICATE_IDENTITY
        )
        assert (
            command[command.index("--certificate-oidc-issuer") + 1]
            == "https://token.actions.githubusercontent.com"
        )
        assert (
            command[command.index("--certificate-github-workflow-sha") + 1] == "c" * 40
        )
    assert all("--clobber" not in command for command in commands)


def test_signature_verification_failure_never_uploads(
    release_fixture: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = prepare(release_fixture)
    monkeypatch.setattr(signing, "existing_endorsements", lambda *_: {})
    commands: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args[:2] == ["cosign", "verify-blob"]:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(signing.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        signing.publish_endorsements(
            inventory,
            release_fixture["directory"],
            "c" * 40,
            "https://github.com/example/project/actions/runs/1",
        )
    assert not any(command[:3] == ["gh", "release", "upload"] for command in commands)


def test_partial_old_manifest_does_not_omit_installer_from_endorsement(
    release_fixture: dict,
) -> None:
    contents = b"historical installer"
    release_fixture["contents"][13] = contents
    release_fixture["snapshot"]["assets"].append(
        {
            "id": 13,
            "name": "installer.sh",
            "size": len(contents),
            "api_digest": None,
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
        }
    )
    release = prepare(release_fixture)["releases"][0]
    assert release["checksum_coverage"] == {
        "covered": ["payload.bin"],
        "uncovered": ["installer.sh"],
    }
    assert (
        next(asset for asset in release["assets"] if asset["name"] == "installer.sh")[
            "sha256"
        ]
        == hashlib.sha256(contents).hexdigest()
    )


@pytest.mark.parametrize(
    "change", ["added", "removed", "replaced", "renamed", "retagged"]
)
def test_metadata_changes_are_rejected(release_fixture: dict, change: str) -> None:
    reviewed = prepare(release_fixture)["releases"][0]
    live = release_fixture["snapshot"]
    if change == "added":
        live["assets"].append({**live["assets"][0], "id": 99, "name": "extra.bin"})
    elif change == "removed":
        live["assets"].pop()
    elif change == "replaced":
        live["assets"][0]["id"] = 99
    elif change == "renamed":
        live["assets"][0]["name"] = "renamed.bin"
    else:
        live["tag_commit"] = "d" * 40
    with pytest.raises(ValueError, match="changed"):
        signing.assert_unchanged(reviewed)


def test_partial_existing_attachments_fail_without_replacement(
    release_fixture: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = prepare(release_fixture)
    monkeypatch.setattr(
        signing,
        "existing_endorsements",
        lambda tag: {signing.attachment_names(tag)[0]: {"id": 999}},
    )
    run = Mock()
    monkeypatch.setattr(signing.subprocess, "run", run)
    with pytest.raises(ValueError, match="already exists"):
        signing.publish_endorsements(
            inventory,
            release_fixture["directory"],
            "c" * 40,
            "https://github.com/example/project/actions/runs/1",
        )
    run.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_REPOSITORY", "example/fork"),
        ("GITHUB_WORKFLOW_SHA", "d" * 40),
    ],
)
def test_publication_requires_matching_trusted_main_context(
    release_fixture: dict, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    raw = signing.canonical_json(prepare(release_fixture))
    release_fixture["manifest"].write_bytes(raw)
    for name, val in {
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": signing.REPOSITORY,
        "GITHUB_SHA": "c" * 40,
        "GITHUB_WORKFLOW_SHA": "c" * 40,
    }.items():
        monkeypatch.setenv(name, val)
    monkeypatch.setenv(field, value)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "signing.py",
            "publish",
            "--directory",
            str(release_fixture["directory"]),
            "--manifest",
            str(release_fixture["manifest"]),
            "--manifest-sha256",
            hashlib.sha256(raw).hexdigest(),
        ],
    )
    with pytest.raises(ValueError, match="trusted repository|commits must match"):
        signing.main()


def test_verification_binds_signed_json_to_reviewed_inventory(
    release_fixture: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = prepare(release_fixture)
    names = signing.attachment_names("v1.0.0")
    monkeypatch.setattr(
        signing, "existing_endorsements", lambda _: dict.fromkeys(names)
    )
    verify = Mock()
    monkeypatch.setattr(signing, "verify_signature", verify)
    body = {
        "kind": "historical-artifact-endorsement",
        "statement": signing.STATEMENT,
        "repository": signing.REPOSITORY,
        "release": inventory["releases"][0],
        "signing_workflow_commit": "c" * 40,
        "reviewed_inventory_sha256": hashlib.sha256(
            signing.canonical_json(inventory)
        ).hexdigest(),
    }

    def download(_tag: str, name: str, path: Path) -> None:
        path.write_bytes(
            signing.canonical_json(body) if name.endswith(".json") else b"bundle"
        )

    monkeypatch.setattr(signing, "download_endorsement", download)
    signing.verify_published(inventory, release_fixture["directory"], "c" * 40)
    assert verify.call_args.args[2] == "c" * 40
    body["reviewed_inventory_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="reviewed inventory"):
        signing.verify_published(inventory, release_fixture["directory"], "c" * 40)


def test_workflow_dry_run_has_no_signing_permissions() -> None:
    import yaml

    workflow = yaml.load(
        (ROOT / ".github/workflows/historical-release-signing.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert (
        workflow["on"]["workflow_dispatch"]["inputs"]["publish"]["default"] == "false"
    )
    assert workflow["jobs"]["inventory"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["sign-and-attach"]["permissions"] == {
        "contents": "write",
        "id-token": "write",
    }
    for job in workflow["jobs"].values():
        assert "github.repository == 'preloop/preloop'" in job["if"]
        assert "github.ref == 'refs/heads/main'" in job["if"]
        for step in job["steps"]:
            if "uses" in step:
                assert len(step["uses"].split("@", 1)[1]) == 40
