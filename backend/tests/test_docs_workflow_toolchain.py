"""Guard image-independent tools used by the documentation workflow."""

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
