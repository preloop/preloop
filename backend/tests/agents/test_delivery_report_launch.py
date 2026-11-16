"""Delivery reports read repositories without generating publication scripts."""

from unittest.mock import patch
from uuid import uuid4

import pytest

from preloop.agents.codex import CodexAgent
from preloop.agents.container import K8S_INNER_SCRIPT_ENV_PREFIX
from preloop.utils.execve_limits import check_launch_payload, chunked_env


@pytest.mark.parametrize("repository_count", [1, 80])
def test_report_launch_has_no_publication_capabilities(repository_count: int) -> None:
    """Existing presets use legacy clone config but must never prepare a push."""
    agent = CodexAgent({})
    tracker_id = str(uuid4())
    context = {
        "account_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "prompt": "Read the repository history and write a delivery report.",
        "agent_config": {"report_kind": "delivery_report", "enable_auto_lint": False},
        "git_clone_config": {
            "enabled": True,
            "publication_mode": "legacy",
            "create_pull_request": False,
            "repositories": [
                {
                    "repository_url": f"https://github.com/example/project-{i}.git",
                    "tracker_id": tracker_id,
                    "branch": "main",
                }
                for i in range(repository_count)
            ],
        },
        "git_credentials_map": {
            tracker_id: {
                "token": "synthetic-installation-token",
                "tracker_type": "github",
                "auth_type": "github_app",
            }
        },
    }
    with patch(
        "preloop.api.endpoints.publication_credentials.mint_publication_capability",
        return_value="synthetic-capability",
    ) as mint:
        script = agent._build_codex_script(context)
        assert "git clone" in script
        assert "git push" not in script
        assert "PRELOOP_PUBLICATION_REFRESH_URL" not in script
        mint.assert_not_called()
    check_launch_payload(
        command=["/bin/bash"],
        args=["-c", "exec bash /tmp/preloop/agent-script.sh"],
        env=chunked_env(K8S_INNER_SCRIPT_ENV_PREFIX, script),
    )
