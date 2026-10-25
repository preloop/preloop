"""Preset copies must not subscribe to wildcard events before configuration."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.services.flow_presets_service import clone_preset_for_account


@pytest.mark.parametrize("source_enabled", [True, False])
def test_ordinary_clone_is_disabled_even_if_preset_was_enabled(
    source_enabled: bool,
) -> None:
    """Copy retained event configuration only behind an explicit activation step."""
    preset = SimpleNamespace(
        id=uuid4(),
        name="Reviewer",
        prompt_template="Review pull requests",
        is_preset=True,
        account_id=None,
        is_enabled=source_enabled,
        trigger_event_source=None,
        trigger_project_ids=None,
        trigger_event_types=["pull_request_opened"],
        agent_type="codex",
        agent_config={},
        allowed_mcp_tools=[],
    )
    crud = MagicMock()
    crud.get_by_name_and_account.return_value = None
    clone_preset_for_account(
        MagicMock(), preset, uuid4(), bound_ai_model_id=uuid4(), flow_crud=crud
    )
    submitted = crud.create.call_args.kwargs["flow_in"]
    assert submitted.is_enabled is False
    assert submitted.trigger_event_source is None
    assert submitted.trigger_project_ids is None
    assert submitted.trigger_event_types == ["pull_request_opened"]


def test_explicit_ad_hoc_activation_has_no_event_subscriptions() -> None:
    """The confirmed run-preset path remains runnable without tracker side effects."""
    preset = SimpleNamespace(
        id=uuid4(),
        name="Triage",
        prompt_template="Triage issues",
        is_preset=True,
        account_id=None,
        trigger_event_source="tracker",
        trigger_project_ids=["project"],
        trigger_event_types=["issue_labeled"],
        agent_type="codex",
        agent_config={},
        allowed_mcp_tools=[],
    )
    crud = MagicMock()
    crud.get_by_name_and_account.return_value = None
    clone_preset_for_account(
        MagicMock(),
        preset,
        uuid4(),
        bound_ai_model_id=uuid4(),
        flow_crud=crud,
        clear_event_triggers=True,
        is_enabled=True,
    )
    submitted = crud.create.call_args.kwargs["flow_in"]
    assert submitted.is_enabled is True
    assert submitted.trigger_event_types == []
    assert submitted.trigger_event_source is None
    assert submitted.trigger_project_ids is None
