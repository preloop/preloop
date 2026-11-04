"""Guard image-independent tools used by the documentation workflow."""

import fnmatch
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/docs.yml"


def test_docs_install_pinned_shellcheck_before_mdm_recipes() -> None:
    """The MDM fixture check has ShellCheck even on a self-hosted runner."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["build"]["steps"]
    setup_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Set up ShellCheck"
    )
    setup = steps[setup_index]["run"]
    assert "shellcheck-v0.11.0.linux.x86_64.tar.xz" in setup
    assert "8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198" in setup
    assert "sha256sum --check" in setup
    assert '>> "$GITHUB_PATH"' in setup
    assert setup_index < next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Check MDM templates against the CLI"
    )


def test_docs_install_pinned_powershell_before_inventory_parse() -> None:
    """The PowerShell parser runs even when a self-hosted image omits pwsh."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["build"]["steps"]
    setup_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Set up PowerShell"
    )
    setup = steps[setup_index]["run"]
    assert "powershell-7.5.3-linux-x64.tar.gz" in setup
    assert "5c74e0bbafd8be59e72267f05700fd6615344d9cf4d30460d9b6d12cd4a88a8c" in setup
    assert "sha256sum --check" in setup
    assert '>> "$GITHUB_PATH"' in setup
    assert setup_index < next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Parse Windows inventory template (no execution)"
    )


def test_docs_runs_architecture_guard_for_documentation_changes() -> None:
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    guard = "backend/tests/test_architecture_docs_map.py"
    for event in ("push", "pull_request"):
        paths = workflow["on"][event]["paths"]
        for changed in (
            "ARCHITECTURE.md",
            "mkdocs.yml",
            "docs/architecture/example.md",
            guard,
        ):
            assert any(fnmatch.fnmatchcase(changed, path) for path in paths), (
                f"{event} does not run the architecture guard for {changed}"
            )
    job = workflow["jobs"]["build"]
    assert "if" not in job
    assert job["env"]["PRELOOP_DISABLE_TELEMETRY"] == "true"
    step = next(step for step in job["steps"] if step.get("run") == f"python {guard}")
    assert "if" not in step
    assert "continue-on-error" not in step


def test_standalone_architecture_guard_rejects_oversized_map(tmp_path: Path) -> None:
    root = WORKFLOW.parents[2]
    guard = Path("backend/tests/test_architecture_docs_map.py")
    (tmp_path / guard.parent).mkdir(parents=True)
    shutil.copyfile(root / guard, tmp_path / guard)
    shutil.copyfile(root / "mkdocs.yml", tmp_path / "mkdocs.yml")
    shutil.copytree(root / "docs/architecture", tmp_path / "docs/architecture")
    architecture = tmp_path / "ARCHITECTURE.md"
    shutil.copyfile(root / "ARCHITECTURE.md", architecture)
    command = [sys.executable, str(tmp_path / guard)]
    valid = subprocess.run(command, capture_output=True, text=True, check=False)
    assert valid.returncode == 0, valid.stderr
    architecture.write_text(architecture.read_text() + "\n" * 151)
    invalid = subprocess.run(command, capture_output=True, text=True, check=False)
    assert invalid.returncode != 0, "standalone guard accepted an oversized map"
    assert "move subsystem detail" in invalid.stderr
