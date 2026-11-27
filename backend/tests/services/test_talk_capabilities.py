"""Talk must refuse requests a connected older sidecar cannot honor."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services import talk_capabilities as talk


def test_old_connection_cannot_reuse_previous_capabilities() -> None:
    """An older replacement process must not inherit a newer one's features."""
    agent = SimpleNamespace(
        id="agent", account_id="account", control_connection_id="new"
    )
    enrollment = SimpleNamespace(
        validation_result={
            "talk_connection_id": "old",
            "talk_capabilities": {"start_directory": True},
        }
    )
    with patch.object(
        talk.crud_managed_agent_enrollment,
        "get_latest_for_agent_by_type",
        return_value=enrollment,
    ):
        assert talk.sidecar_features(MagicMock(), agent)["capabilities"] == {}


def test_start_directory_is_refused_before_delivery_to_old_sidecar() -> None:
    """A directory must never be silently discarded by an old runtime."""
    with pytest.raises(talk.TalkCapabilityError, match="working directories"):
        talk.require_features(
            {"capabilities": {}}, ["start_directory", "command_session"]
        )


def test_project_choices_match_host_and_repository() -> None:
    """An identical slug on another tracker host is not the same checkout."""
    project = SimpleNamespace(
        id="project",
        name="Example",
        slug="team/example",
        organization=SimpleNamespace(tracker=SimpleNamespace()),
    )
    with (
        patch.object(
            talk.crud_project, "list_active_for_tracker_types", return_value=[project]
        ),
        patch.object(talk, "repository_host", return_value="git.example.com"),
    ):
        assert talk.project_choices(
            MagicMock(),
            "account",
            [
                {"path": "/work/example", "remote": "git.example.com/team/example"},
                {"path": "/other/example", "remote": "other.example.com/team/example"},
            ],
        ) == [{"project_id": "project", "name": "Example", "path": "/work/example"}]


def test_current_checkout_supersedes_stale_remote_at_same_path() -> None:
    """Old hook history cannot relabel a checkout the host has since changed."""
    project = SimpleNamespace(
        id="project",
        name="Example",
        slug="team/example",
        organization=SimpleNamespace(tracker=SimpleNamespace()),
    )
    with (
        patch.object(
            talk.crud_project, "list_active_for_tracker_types", return_value=[project]
        ),
        patch.object(talk, "repository_host", return_value="git.example.com"),
    ):
        assert (
            talk.project_choices(
                MagicMock(),
                "account",
                [
                    {
                        "path": "/work/example",
                        "remote": "git.example.com/team/replacement",
                    },
                    {"path": "/work/example", "remote": "git.example.com/team/example"},
                ],
            )
            == []
        )
