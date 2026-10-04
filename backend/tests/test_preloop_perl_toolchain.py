"""The environment image installs a distro Perl toolchain."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "environments" / "preloop" / "Dockerfile"
SMOKE = REPO / "environments" / "perl" / "perl-toolchain-smoke.sh"

# Distro package names. Exact `name=version` pins are rejected: noble-updates
# drops superseded point releases, so a pin fails the next security update.
PACKAGES = (
    "perl",
    "cpanminus",
    "libperl-minimumversion-perl",
    "libperl-critic-perl",
    "libtest-harness-perl",
    "libtest-simple-perl",
)


def _apt_install_packages(text: str) -> list[str]:
    """Return package tokens in the first ``apt-get install`` instruction."""
    packages: list[str] = []
    in_install = False
    for line in text.splitlines():
        if "apt-get install" in line:
            in_install = True
            continue
        if not in_install:
            continue
        stripped = line.strip().rstrip("\\").strip()
        if stripped.startswith("&&") or not stripped:
            break
        packages.append(stripped)
    return packages


def test_dockerfile_installs_distro_perl_packages() -> None:
    """The image layer names perl, cpanminus, and the linter packages.

    Packages are unpinned. An exact version pin against noble-updates
    breaks the next point release, and ``Dockerfile.dev`` does not pin
    apt packages either. The smoke script is the build gate.
    """
    text = DOCKERFILE.read_text()
    packages = _apt_install_packages(text)
    assert packages == list(PACKAGES)
    assert all("=" not in package for package in packages)
    assert "perl-toolchain-smoke.sh" in text
    assert "rm -rf /var/lib/apt/lists/*" in text


def test_smoke_script_checks_the_three_tools() -> None:
    """The build smoke covers perlver, perlcritic, and prove.

    ``perlver`` 1.40 rejects ``--version`` (unknown option, non-zero exit).
    The script runs ``perlver`` on a one-line program and prints the module
    version. ``perlcritic`` and ``prove`` do support ``--version``.
    """
    text = SMOKE.read_text()
    assert "perlver --version" not in text
    assert "perlver " in text
    assert "Perl::MinimumVersion" in text
    assert "perlcritic --version" in text
    assert "prove --version" in text
    assert "Test::More" in text
    assert "Test::Harness" in text
    assert "cpanm" in text


def test_smoke_script_passes_bash_n() -> None:
    """The smoke script is valid bash."""
    result = subprocess.run(
        ["bash", "-n", str(SMOKE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
