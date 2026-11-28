"""Saving a read-only review its executor cannot run is refused (#2602)."""

import pytest
from fastapi import HTTPException

from preloop.api.endpoints.flows import _reject_unrunnable_read_only_review

READ_ONLY = {
    "enabled": True,
    "publication_mode": "read_only",
    "repositories": [{"tracker_id": "t1", "identifier": "acme/api"}],
}


def test_claude_read_only_review_on_private_runner_is_refused_on_save() -> None:
    """A Claude host profile cannot run read-only review, so save says so."""
    with pytest.raises(HTTPException) as caught:
        _reject_unrunnable_read_only_review(
            git_clone_config=READ_ONLY,
            agent_type="claude_code",
            agent_config={"host_exec_profile": "claude"},
            runner_pool="local",
        )
    assert caught.value.status_code == 422
    assert "Codex" in str(caught.value.detail)


def test_hosted_codex_read_only_review_saves() -> None:
    """The hosted Codex executor on the server pool is admitted."""
    _reject_unrunnable_read_only_review(
        git_clone_config=READ_ONLY,
        agent_type="codex",
        agent_config={},
        runner_pool="server",
    )


def test_non_review_flow_is_not_checked() -> None:
    """Flows that are not read-only reviews keep every executor."""
    _reject_unrunnable_read_only_review(
        git_clone_config={"enabled": True},
        agent_type="claude_code",
        agent_config={"host_exec_profile": "claude"},
        runner_pool="local",
    )
