"""Publisher wiring tests for the publication gate (issue #428).

The publisher is the post-execution block in ``container.py`` that pushes
the branch and opens the pull request. These tests pin its contract:

* flows without a verification policy are published exactly as before,
* a gate flow runs the verifier *after* the recovery artifacts and *before*
  the push, and the push/PR creation only happen on an ALLOW verdict,
* a denial exits non-zero (failing the execution) instead of publishing.
"""

import uuid

import pytest

from preloop.agents.container import ContainerAgentExecutor
from preloop.services.verification import resolve_verification_policy

pytestmark = pytest.mark.asyncio


PROFILE = {
    "version": "v1",
    "profile_id": "publisher-test",
    "description": "profile used by publisher wiring tests",
    "always": [
        {"id": "lint", "command": "echo lint-ok", "reason": "hook", "scope": "backend"}
    ],
    "rules": [],
    "unknown_default": [
        {
            "id": "fast-tests",
            "command": "echo fast-ok",
            "reason": "conservative default",
            "scope": "unknown",
        }
    ],
}


def _context(**git_overrides) -> dict:
    git_config = {
        "enabled": True,
        "create_pull_request": True,
        "repositories": [
            {
                "repository_url": "https://github.com/acme/widgets.git",
                "clone_path": "/workspace",
                "tracker_id": "tracker-1",
            }
        ],
    }
    git_config.update(git_overrides)
    return {
        "flow_id": "flow-1",
        "execution_id": str(uuid.uuid4()),
        "flow_name": "Implementation",
        "_git_target_branch": "preloop/issue-428",
        "_git_source_branch": "main",
        "git_clone_config": git_config,
        "git_credentials_map": {
            "tracker-1": {"token": "tok", "tracker_type": "github"}
        },
        "trigger_event_data": {
            "repository": {"clone_url": "https://github.com/acme/widgets.git"},
        },
    }


@pytest.fixture
def executor():
    return ContainerAgentExecutor(
        agent_type="codex",
        config={"test": True},
        image="test-image:latest",
        use_kubernetes=False,
    )


class TestPublisherWithoutPolicy:
    def test_existing_flows_publish_ungated(self, executor):
        """No verification key: the publisher behaves exactly as before."""
        commands = executor._prepare_git_post_execution_commands(_context())
        assert "preloop_gate_verify.py" not in commands
        assert "git push origin" in commands

    def test_verification_mode_off_publishes_ungated(self, executor):
        commands = executor._prepare_git_post_execution_commands(
            _context(verification={"mode": "off", "profile": PROFILE})
        )
        assert "preloop_gate_verify.py" not in commands


class TestPublisherWithGate:
    def _context_gate(self):
        return _context(
            verification={"mode": "gate", "profile": PROFILE},
        )

    def test_gate_runs_before_the_push(self, executor):
        commands = executor._prepare_git_post_execution_commands(self._context_gate())
        gate_at = commands.index("preloop_gate_verify.py")
        recovery_at = commands.index("branch.bundle")
        push_at = commands.index("git push origin")
        assert recovery_at < gate_at < push_at

    def test_gate_embeds_the_trusted_profile(self, executor):
        commands = executor._prepare_git_post_execution_commands(self._context_gate())
        assert "PRELOOP_PROFILE_EOF" in commands
        assert "publisher-test" in commands
        # The selection module is embedded verbatim, so the container runs
        # the same selection implementation as the runner contract.
        assert "select_from_raw" in commands

    def test_push_only_after_an_allow_verdict(self, executor):
        commands = executor._prepare_git_post_execution_commands(self._context_gate())
        verdict_at = commands.index("PRELOOP_VERDICT_TEXT=")
        push_at = commands.index("git push origin")
        assert verdict_at < push_at

    def test_denial_marker_and_nonzero_exit_are_present(self, executor):
        commands = executor._prepare_git_post_execution_commands(self._context_gate())
        assert "PRELOOP_VERIFICATION_DENIED" in commands
        assert "exit 3" in commands

    def test_denial_fails_before_pr_creation(self, executor):
        commands = executor._prepare_git_post_execution_commands(self._context_gate())
        assert "api.github.com/repos/acme/widgets/pulls" in commands
        denied_at = commands.index("PRELOOP_VERIFICATION_DENIED")
        pr_at = commands.index("api.github.com/repos/acme/widgets/pulls")
        assert denied_at < pr_at

    def test_policy_is_resolved_from_the_flow_config(self, executor):
        policy = resolve_verification_policy(self._context_gate()["git_clone_config"])
        assert policy.mode == "gate"
        assert policy.profile is not None
        assert policy.profile.profile_id == "publisher-test"

    def test_malformed_configured_profile_blocks_before_publication(self, executor):
        context = _context(verification={"mode": "gate", "profile": {"nope": True}})
        with pytest.raises(ValueError):
            resolve_verification_policy(context["git_clone_config"])
        with pytest.raises(ValueError):
            executor._prepare_git_post_execution_commands(context)
