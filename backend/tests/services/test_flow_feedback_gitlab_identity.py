"""GitLab feedback binds on the project id, not the id-less ``repository`` (#2599)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from preloop.services.flow_feedback import (
    ingest_feedback,
    payload_repository,
    register_thread,
)

# GitLab issue, note and merge request hooks carry both objects; only
# ``project`` has the numeric id.
GITLAB_PROJECT = {"id": 42, "path_with_namespace": "workshop/shop-api"}
GITLAB_LEGACY_REPOSITORY = {
    "name": "shop-api",
    "url": "http://gitlab.example.com:8929/workshop/shop-api.git",
    "homepage": "http://gitlab.example.com:8929/workshop/shop-api",
}
MR_URL = "http://gitlab.example.com:8929/workshop/shop-api/-/merge_requests/1"


def _flow() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        trigger_event_source="webhook",
        git_clone_config=None,
        agent_config={"feedback": {"enabled": True, "debounce_seconds": 0}},
    )


def _execution(flow: SimpleNamespace, details: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        flow_id=flow.id,
        trigger_event_details={
            "source": "gitlab",
            "tracker_id": str(uuid.uuid4()),
            "_session_thread_id": str(uuid.uuid4()),
            **details,
        },
    )


def test_payload_repository_prefers_gitlab_project() -> None:
    payload = {"repository": GITLAB_LEGACY_REPOSITORY, "project": GITLAB_PROJECT}
    assert payload_repository("gitlab", payload) is GITLAB_PROJECT
    github = {"repository": {"id": 7}}
    assert payload_repository("github", github) == {"id": 7}
    assert payload_repository("gitlab", {}) == {}


def test_register_thread_binds_gitlab_issue_hook_on_project_id() -> None:
    flow = _flow()
    execution = _execution(
        flow,
        {
            "payload": {
                "object_kind": "issue",
                "repository": GITLAB_LEGACY_REPOSITORY,
                "project": GITLAB_PROJECT,
                "object_attributes": {"iid": 3},
            }
        },
    )
    with (
        patch("preloop.services.flow_feedback.crud_flow.get", return_value=flow),
        patch("preloop.services.flow_feedback.crud_flow_feedback.register") as reg,
    ):
        register_thread(MagicMock(), execution, MR_URL, "preloop/fix")
    reg.assert_called_once()
    values = reg.call_args.kwargs["values"]
    assert values["repository_id"] == "42"
    assert values["pr_number"] == "1"
    assert values["provider"] == "gitlab"


def test_register_thread_falls_back_to_synced_project_identifier() -> None:
    flow = _flow()
    project_id = str(uuid.uuid4())
    execution = _execution(flow, {"project_id": project_id, "payload": {}})
    synced = SimpleNamespace(identifier="42", slug="workshop/shop-api")
    with (
        patch("preloop.services.flow_feedback.crud_flow.get", return_value=flow),
        patch("preloop.models.crud.crud_project.get", return_value=synced),
        patch("preloop.services.flow_feedback.crud_flow_feedback.register") as reg,
    ):
        register_thread(MagicMock(), execution, MR_URL, "preloop/fix")
    reg.assert_called_once()
    assert reg.call_args.kwargs["values"]["repository_id"] == "42"


def test_ingest_gitlab_note_finds_thread_by_project_id() -> None:
    event = {
        "type": "comment_created",
        "source": "gitlab",
        "account_id": str(uuid.uuid4()),
        "tracker_id": str(uuid.uuid4()),
        "payload": {
            "object_kind": "note",
            "repository": GITLAB_LEGACY_REPOSITORY,
            "project": GITLAB_PROJECT,
            "merge_request": {"iid": 1},
        },
    }
    with patch("preloop.services.flow_feedback.crud_flow_feedback.find") as find:
        find.return_value = []
        ingest_feedback(MagicMock(), event)
    find.assert_called_once()
    assert find.call_args.kwargs["repository_id"] == "42"
    assert find.call_args.kwargs["pr_number"] == "1"
