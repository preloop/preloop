"""Tests for the flow review_instructions field and prompt placeholder."""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from preloop.models.schemas.flow import FlowUpdate
from preloop.services.prompt_resolvers.base import ResolverContext
from preloop.services.prompt_resolvers.flow import (
    FlowResolver,
    review_instructions_text,
)


def test_blank_instructions_store_as_null() -> None:
    parsed = FlowUpdate(review_instructions="  \n")
    assert parsed.review_instructions is None


def test_instructions_are_stripped() -> None:
    parsed = FlowUpdate(review_instructions="  keep the declared runtime  ")
    assert parsed.review_instructions == "keep the declared runtime"


def test_oversized_instructions_are_rejected() -> None:
    with pytest.raises(ValidationError):
        FlowUpdate(review_instructions="x" * 32769)


def test_unset_instructions_render_as_empty() -> None:
    assert review_instructions_text(None) == ""
    assert review_instructions_text("   ") == ""
    assert review_instructions_text("stay compatible") == "stay compatible"


@pytest.mark.asyncio
async def test_resolver_reads_the_flow_column(monkeypatch: pytest.MonkeyPatch) -> None:
    flow = MagicMock(review_instructions="  minimum runtime stays  ")
    monkeypatch.setattr(
        "preloop.services.prompt_resolvers.flow.crud_flow.get",
        lambda _db, id: flow,
    )
    context = ResolverContext(
        db=MagicMock(),
        trigger_event_data={},
        flow_id="flow-1",
        execution_id="exec-1",
    )
    assert FlowResolver().prefix == "flow"
    text = await FlowResolver().resolve("review_instructions", context)
    assert text == "minimum runtime stays"


@pytest.mark.asyncio
async def test_resolver_missing_flow_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "preloop.services.prompt_resolvers.flow.crud_flow.get",
        lambda _db, id: None,
    )
    context = ResolverContext(
        db=MagicMock(),
        trigger_event_data={},
        flow_id="missing",
        execution_id="exec-1",
    )
    assert await FlowResolver().resolve("review_instructions", context) == ""


@pytest.mark.asyncio
async def test_resolver_unknown_field_is_none() -> None:
    context = ResolverContext(
        db=MagicMock(),
        trigger_event_data={},
        flow_id="flow-1",
        execution_id="exec-1",
    )
    assert await FlowResolver().resolve("name", context) is None
