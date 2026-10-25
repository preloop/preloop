"""Guard offline discovery checks on hosted and self-hosted Linux runners."""

from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github/workflows/discovery-parity.yml"
)


def test_linux_discovery_uses_narrow_self_hosted_helper() -> None:
    """Self-hosted runners cannot invoke unrestricted sudo or direct unshare."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["discovery-parity"]["steps"]
    hosted = next(
        step
        for step in steps
        if step.get("name") == "Test fixtures without external network (hosted Linux)"
    )
    self_hosted = next(
        step
        for step in steps
        if step.get("name")
        == "Test fixtures without external network (self-hosted Linux)"
    )
    assert (
        hosted["if"] == "runner.os == 'Linux' && runner.environment == 'github-hosted'"
    )
    assert (
        "sudo --preserve-env=PRELOOP_DISABLE_TELEMETRY,PRELOOP_DISCOVERY_OFFLINE unshare --net"
        in hosted["run"]
    )
    assert (
        self_hosted["if"]
        == "runner.os == 'Linux' && runner.environment == 'self-hosted'"
    )
    assert self_hosted["run"].strip() == (
        'sudo -n /usr/local/sbin/preloop-ci-offline "$RUNNER_TEMP/discovery.test.exe" '
        '-test.run "$DISCOVERY_TESTS" -test.v -test.count=1 -test.timeout=10m'
    )
