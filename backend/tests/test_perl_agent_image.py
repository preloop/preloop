"""The opt-in Perl agent image recipe and its shared smoke (issue #1058).

The image itself is built and smoked by environments/perl/run-smoke.sh,
which needs Docker. These tests pin the recipe's contract and run the smoke
script against stub toolchains, so they need only bash (plus a real Perl
toolchain for the end-to-end case, which is skipped when it is absent).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_preloop_perl_toolchain import PACKAGES, _apt_install_packages

REPO = Path(__file__).resolve().parents[2]
PERL_DIR = REPO / "environments" / "perl"
DOCKERFILE = PERL_DIR / "Dockerfile"
SMOKE = PERL_DIR / "perl-toolchain-smoke.sh"
RUN_SMOKE = PERL_DIR / "run-smoke.sh"
FIXTURES = PERL_DIR / "fixtures"
README = PERL_DIR / "README.md"
FIXTURE_DOCKERFILE = REPO / "environments" / "preloop" / "Dockerfile"
TOOLS = ("perl", "cpanm", "perlver", "perlcritic", "prove")
BASH = shutil.which("bash") or "/bin/bash"


def _instructions(text: str) -> list[str]:
    return [
        line.split()[0].upper()
        for line in text.splitlines()
        if line.strip() and not line.startswith((" ", "#"))
    ]


def test_recipe_extends_a_build_time_base_and_installs_only_perl() -> None:
    text = DOCKERFILE.read_text()
    assert "ARG CODEX_BASE_IMAGE\nFROM ${CODEX_BASE_IMAGE}" in text
    assert "*@sha256:*" in text, "a mutable base tag must be refused"
    assert _apt_install_packages(text) == list(PACKAGES)
    lowered = "\n".join(
        line for line in text.lower().splitlines() if not line.startswith("#")
    )
    for unrelated in ("postgres", "playwright", "chromium", "app-dev.txt", "npm"):
        assert unrelated not in lowered


def test_recipe_preserves_base_entrypoint_workdir_and_user() -> None:
    """The executor, not the image, chooses the user and command."""
    instructions = _instructions(DOCKERFILE.read_text())
    for forbidden in ("ENTRYPOINT", "CMD", "WORKDIR"):
        assert forbidden not in instructions
    users = [
        line for line in DOCKERFILE.read_text().splitlines() if line.startswith("USER")
    ]
    assert users == ["USER root", "USER ${BASE_USER:-root}"], (
        "restore the base user last"
    )
    assert "ARG BASE_USER=root" in DOCKERFILE.read_text()


def test_both_images_run_the_one_shared_smoke() -> None:
    shared = "COPY environments/perl/perl-toolchain-smoke.sh"
    assert shared in DOCKERFILE.read_text()
    assert shared in FIXTURE_DOCKERFILE.read_text()
    assert "COPY environments/perl/fixtures" in FIXTURE_DOCKERFILE.read_text()
    assert not (REPO / "environments" / "preloop" / "perl-toolchain-smoke.sh").exists()


def test_fixtures_are_checked_in() -> None:
    compatible = (FIXTURES / "compatible-5_10.pl").read_text()
    newer = (FIXTURES / "postfix-dereference.pl").read_text()
    assert "//" in compatible and "->@*" not in compatible
    assert "->@*" in newer and "use feature 'postderef'" in newer
    assert "Test::More" in (FIXTURES / "t" / "pass.t").read_text()
    assert "Test::More" in (FIXTURES / "failing" / "fail.t").read_text()


def test_smoke_never_calls_perlver_version_or_trusts_its_exit() -> None:
    text = SMOKE.read_text()
    assert "perlver --version" not in text
    assert "->minimum_version" in text


@pytest.mark.parametrize("script", [SMOKE, RUN_SMOKE])
def test_scripts_pass_bash_n(script: Path) -> None:
    result = subprocess.run(
        [BASH, "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def _stub_toolchain(tmp_path: Path, missing: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in TOOLS:
        if tool == missing:
            continue
        stub = bin_dir / tool
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    return bin_dir


@pytest.mark.parametrize("missing", TOOLS)
def test_smoke_fails_and_names_a_missing_tool(tmp_path: Path, missing: str) -> None:
    bin_dir = _stub_toolchain(tmp_path, missing)
    result = subprocess.run(
        [BASH, str(SMOKE), str(FIXTURES)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
    )
    assert result.returncode != 0
    assert f"missing required tool: {missing}" in result.stderr


def _real_toolchain() -> bool:
    if not all(shutil.which(tool) for tool in TOOLS):
        return False
    probe = subprocess.run(
        ["perl", "-MPerl::MinimumVersion", "-MPerl::Critic", "-e1"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


needs_perl = pytest.mark.skipif(
    not _real_toolchain(), reason="distro Perl toolchain not installed"
)


@needs_perl
def test_smoke_passes_on_a_real_toolchain(tmp_path: Path) -> None:
    result = subprocess.run(
        [BASH, str(SMOKE), str(FIXTURES)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert "compatible-5_10.pl=5.010 " in result.stdout
    assert "perl-toolchain-smoke: OK" in result.stdout


@needs_perl
def test_failing_tap_fixture_fails_prove() -> None:
    result = subprocess.run(
        ["prove", str(FIXTURES / "failing" / "fail.t")],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_readme_documents_the_evidence_and_boundaries() -> None:
    text = README.read_text()
    for needle in (
        "CODEX_IMAGE",
        "docker_image",
        "host_exec_profile",
        "run-smoke.sh",
        "--negative-tool",
        "--network none",
        "digest",
        "not execution on Perl 5.10",
    ):
        assert needle in text, needle
