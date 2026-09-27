"""The environment image installs a distro Perl toolchain."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "environments" / "preloop" / "Dockerfile"
SMOKE = REPO / "environments" / "preloop" / "perl-toolchain-smoke.sh"

# Ubuntu 24.04 package versions apt installs on the pinned base image.
PINNED_PACKAGES = (
    "perl=5.38.2-3.2ubuntu0.6",
    "cpanminus=1.7047-1",
    "libperl-minimumversion-perl=1.40-1",
    "libperl-critic-perl=1.152-1",
    "libtest-harness-perl=3.48-1",
    "libtest-simple-perl=1.302198-1",
)


def test_dockerfile_installs_pinned_perl_packages() -> None:
    """The image layer names perl, cpanminus, and the linter packages."""
    text = DOCKERFILE.read_text()
    for package in PINNED_PACKAGES:
        assert package in text, package
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
