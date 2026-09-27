"""Guards for the release workflow's Windows signing configuration check.

The release may publish unsigned Windows binaries only while the repository
variable ``SIGNPATH_SIGNING_REQUIRED`` is not ``true``. These tests pin the
workflow wiring: the check step reads the variable and the missing-credential
branch can fail the release, so signing cannot silently regress once it is
required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _signpath_check_step() -> Dict:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("id") == "signpath-check":
                return step
    raise AssertionError("release.yml has no signpath-check step")


def test_signing_required_variable_is_wired() -> None:
    """The check step reads SIGNPATH_SIGNING_REQUIRED from repo variables."""
    step = _signpath_check_step()
    assert step["env"]["REQUIRED"] == "${{ vars.SIGNPATH_SIGNING_REQUIRED }}"


def test_missing_credentials_fail_when_signing_is_required() -> None:
    """The required-but-unconfigured branch exits nonzero, failing release."""
    step = _signpath_check_step()
    script = step["run"]
    required_branch = script.split('[ "$required" = "true" ]', 1)
    assert len(required_branch) == 2, "no required branch in signpath-check"
    assert "exit 1" in required_branch[1].split("else", 1)[0]


def test_signing_required_value_is_case_normalized() -> None:
    """TRUE/True/1 count as required; the check lowercases before matching."""
    script = _signpath_check_step()["run"]
    assert "tr '[:upper:]' '[:lower:]'" in script
    assert '[ "$required" = "1" ]' in script


def test_optional_mode_still_publishes_unsigned() -> None:
    """Without the variable, the step keeps the enabled=false fallback."""
    step = _signpath_check_step()
    assert "enabled=false" in step["run"]
