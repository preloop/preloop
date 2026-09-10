"""Release payload coverage and fail-closed provenance verification."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "release_assets", REPO_ROOT / "scripts" / "release_assets.py"
)
assert SPEC is not None and SPEC.loader is not None
release_assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_assets)


class ReleaseAssetsTest(unittest.TestCase):
    """Exercise all shipped asset types and verification failures locally."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "assets"
        self.directory.mkdir()
        self.bundle = Path(self.temporary.name) / "bundle.jsonl"
        self.bundle.write_text("synthetic mocked bundle", encoding="utf-8")
        self.payloads = [
            "preloop-linux-amd64",
            "preloop-windows-amd64.exe",
            "preloop-1.2.3.tgz",
            "preloop-1.2.3.tar.gz",
            "preloop-1.2.3-py3-none-any.whl",
            "preloop-sbom-cli-1.2.3.cdx.json",
            "docker-compose.release.yaml",
            "install-cli.sh",
            "install-cli.ps1",
            "install-oss.sh",
        ]
        for name in self.payloads:
            (self.directory / name).write_bytes(f"payload {name}".encode())

    def verify(self) -> None:
        release_assets.verify_assets(
            self.directory, self.bundle, "example/project", "v1.2.3", "a" * 40
        )

    def test_checksums_cover_all_payloads_and_are_repeatable(self) -> None:
        release_assets.write_checksums(self.directory)
        manifest = self.directory / "SHA256SUMS"
        first = manifest.read_bytes()
        entries = dict(line.split("  ")[::-1] for line in first.decode().splitlines())
        self.assertEqual(set(entries), set(self.payloads))
        for name, digest in entries.items():
            self.assertEqual(
                digest, hashlib.sha256((self.directory / name).read_bytes()).hexdigest()
            )
        release_assets.write_checksums(self.directory)
        self.assertEqual(first, manifest.read_bytes())

    def test_verify_all_payloads_and_manifest_with_strict_identity(self) -> None:
        release_assets.write_checksums(self.directory)
        with patch.object(release_assets.subprocess, "run") as run:
            self.verify()
        verified = set()
        for call in run.call_args_list:
            command = call.args[0]
            verified.add(Path(command[3]).name)
            for flag, value in (
                ("--bundle", str(self.bundle)),
                ("--repo", "example/project"),
                ("--signer-workflow", "example/project/.github/workflows/release.yml"),
                ("--source-ref", "refs/tags/v1.2.3"),
                ("--source-digest", "a" * 40),
            ):
                self.assertEqual(command[command.index(flag) + 1], value)
            self.assertIn("--deny-self-hosted-runners", command)
            self.assertTrue(call.kwargs["check"])
        self.assertEqual(verified, {*self.payloads, "SHA256SUMS"})

    def test_rejected_signature_aborts_verification(self) -> None:
        release_assets.write_checksums(self.directory)
        with patch.object(
            release_assets.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, ["gh"]),
        ) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                self.verify()
        self.assertEqual(run.call_count, 1)

    def test_missing_manifest_cannot_pass(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing SHA256SUMS"):
            self.verify()

    def test_missing_bundle_cannot_pass(self) -> None:
        release_assets.write_checksums(self.directory)
        self.bundle.unlink()
        with self.assertRaisesRegex(ValueError, "bundle is missing"):
            self.verify()

    def test_bundle_must_not_be_a_subject_of_itself(self) -> None:
        release_assets.write_checksums(self.directory)
        self.bundle = self.bundle.rename(self.directory / self.bundle.name)
        with self.assertRaisesRegex(ValueError, "before copying"):
            self.verify()

    def test_empty_release_is_rejected(self) -> None:
        for path in self.directory.iterdir():
            path.unlink()
        with self.assertRaisesRegex(ValueError, "empty"):
            release_assets.write_checksums(self.directory)

    def test_symlink_and_multiline_names_are_rejected(self) -> None:
        (self.directory / "indirect").symlink_to(self.bundle)
        with self.assertRaisesRegex(ValueError, "regular file"):
            release_assets.write_checksums(self.directory)
        (self.directory / "indirect").unlink()
        (self.directory / "ambiguous\nname").touch()
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            release_assets.write_checksums(self.directory)


if __name__ == "__main__":
    unittest.main()
