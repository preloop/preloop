"""Prefer a matching system interpreter, else fall back to setup-python.

``actions/setup-python`` only ships Ubuntu/RHEL builds. A Debian 12 runner
already has ``python3.11`` but the action still errors with ``not found for
debian 12``. Distro Python is also PEP 668 managed, so CI cannot ``pip
install`` into it: the system interpreter is used only through a venv.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from tests.ci_workflow import REPO_ROOT, load_ci_jobs

SCRIPT = (
    REPO_ROOT / ".github" / "actions" / "setup-ci-python" / "prefer-system-python.sh"
)
COMPOSITE = REPO_ROOT / ".github" / "actions" / "setup-ci-python" / "action.yml"
SETUP_PYTHON_PIN = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
COMPOSITE_USES = "./.github/actions/setup-ci-python"
PYTHON_JOBS = (
    "requirements-resolve",
    "lint",
    "test-backend",
    "test-backend-coverage",
    "test-runtime-plugins",
)


def _output_value(path: Path, name: str) -> str:
    """Return the last assignment of ``name`` in a GITHUB_OUTPUT file."""
    found = ""
    prefix = f"{name}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            found = line[len(prefix) :]
    return found


def _run_script(
    tmp_path: Path,
    *,
    python_version: str,
    path: str,
) -> SimpleNamespace:
    github_output = tmp_path / "github_output"
    github_path = tmp_path / "github_path"
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir()
    github_output.write_text("", encoding="utf-8")
    github_path.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHON_VERSION"] = python_version
    env["GITHUB_OUTPUT"] = str(github_output)
    env["GITHUB_PATH"] = str(github_path)
    env["RUNNER_TEMP"] = str(runner_temp)
    # A sandbox bin so a host python3 cannot leak in: on Debian /bin is
    # /usr/bin, so "PATH=...:/bin" still exposes python3.11. The script only
    # needs rm from outside the fake dir (echo/printf/command/[ are builtins).
    sandbox = tmp_path / "sandbox-bin"
    sandbox.mkdir()
    (sandbox / "rm").symlink_to("/bin/rm")
    env["PATH"] = os.pathsep.join([path, str(sandbox)])
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    return SimpleNamespace(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        github_output=github_output,
        github_path=github_path,
        runner_temp=runner_temp,
    )


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_missing_system_python_falls_back_to_setup_python(tmp_path: Path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = _run_script(tmp_path, python_version="3.11", path=str(empty))
    assert result.returncode == 0, result.stderr
    assert _output_value(result.github_output, "use_setup") == "true"
    assert result.github_path.read_text(encoding="utf-8") == ""
    assert "No system Python 3.11 on PATH" in result.stdout


def test_wrong_minor_falls_back_to_setup_python(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Probe order is python3.11, then python3. Fake both so this is a
    # version mismatch, not "candidate missing".
    _write_executable(
        bin_dir / "python3.11",
        "#!/bin/sh\nprintf '3.12\\n'\n",
    )
    _write_executable(
        bin_dir / "python3",
        "#!/bin/sh\nprintf '3.12\\n'\n",
    )
    result = _run_script(tmp_path, python_version="3.11", path=str(bin_dir))
    assert result.returncode == 0, result.stderr
    assert _output_value(result.github_output, "use_setup") == "true"
    assert "No system Python 3.11 on PATH" in result.stdout


def test_matching_python_without_venv_falls_back(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "python3.11",
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then printf "3.11\\n"; exit 0; fi\n'
        "echo ensurepip is not available >&2\n"
        "exit 1\n",
    )
    result = _run_script(tmp_path, python_version="3.11", path=str(bin_dir))
    assert result.returncode == 0, result.stderr
    assert _output_value(result.github_output, "use_setup") == "true"
    assert not (result.runner_temp / "preloop-python").exists()
    assert "::warning::" in result.stdout
    assert "python3.11-venv" in result.stdout


def test_matching_system_python_uses_a_venv(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real = Path(sys.executable).resolve()
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    (bin_dir / f"python{minor}").symlink_to(real)
    (bin_dir / "python3").symlink_to(real)
    result = _run_script(tmp_path, python_version=minor, path=str(bin_dir))
    assert result.returncode == 0, result.stderr + result.stdout
    assert _output_value(result.github_output, "use_setup") == "false"
    venv_python = result.runner_temp / "preloop-python" / "bin" / "python"
    assert venv_python.is_file()
    github_path = result.github_path.read_text(encoding="utf-8")
    assert str(result.runner_temp / "preloop-python" / "bin") in github_path
    pip = result.runner_temp / "preloop-python" / "bin" / "pip"
    assert pip.is_file()


def test_patch_version_input_matches_minor(tmp_path: Path) -> None:
    """``3.11.16`` must probe ``python3.11``, not ``python3.11.16``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "python3.11",
        '#!/bin/sh\nif [ "$1" = "-c" ]; then printf "3.11\\n"; exit 0; fi\nexit 1\n',
    )
    result = _run_script(tmp_path, python_version="3.11.16", path=str(bin_dir))
    assert result.returncode == 0, result.stderr
    assert "No system Python" not in result.stdout
    assert "python3.11-venv" in result.stdout
    assert _output_value(result.github_output, "use_setup") == "true"


def test_broken_interpreter_is_skipped(tmp_path: Path) -> None:
    """A candidate that exists but exits 1 is not a match."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "python3.11", "#!/bin/sh\nexit 1\n")
    result = _run_script(tmp_path, python_version="3.11", path=str(bin_dir))
    assert result.returncode == 0, result.stderr
    assert _output_value(result.github_output, "use_setup") == "true"
    assert "No system Python 3.11 on PATH" in result.stdout


def test_ci_python_jobs_use_the_composite() -> None:
    jobs = load_ci_jobs()
    for name in PYTHON_JOBS:
        step = next(s for s in jobs[name]["steps"] if s.get("name") == "Set up Python")
        assert step["uses"] == COMPOSITE_USES, name
        assert step["with"]["python-version"] == "${{ env.PYTHON_VERSION }}"


def test_composite_falls_back_to_the_pinned_setup_python() -> None:
    import yaml  # type: ignore[import-untyped]

    with COMPOSITE.open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    steps = doc["runs"]["steps"]
    prefer = steps[0]
    fallback = steps[1]
    assert "prefer-system-python.sh" in prefer["run"]
    # github.action_path is the host path. A job that runs in a
    # container mounts the workspace at /__w, where that host path does
    # not exist. Backend shards run on the runner. GITHUB_WORKSPACE is
    # valid in both layouts.
    assert "github.action_path" not in prefer["run"]
    assert "GITHUB_WORKSPACE" in prefer["run"]
    assert fallback["uses"] == SETUP_PYTHON_PIN
    assert fallback["if"] == "steps.system.outputs.use_setup == 'true'"
