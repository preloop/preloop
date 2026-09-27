"""Direct tests for the validation logic in ``preloop.schemas.operator_notes``."""

import uuid
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from preloop.schemas.operator_notes import (
    OperatorNoteCreate,
    OperatorNotePendingRequest,
)
from preloop.services.operator_notes import MAX_NOTE_BODY_CHARS

TARGETS = ("agent_id", "runtime_session_id", "execution_id")


class TestOperatorNoteCreateTarget:
    """A note names exactly one target."""

    @pytest.mark.parametrize("target", TARGETS)
    def test_single_target_is_accepted(self, target: str) -> None:
        target_id = uuid.uuid4()
        note = OperatorNoteCreate(text="Check the tests", **{target: target_id})

        assert getattr(note, target) == target_id

    def test_no_target_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Name exactly one of"):
            OperatorNoteCreate(text="Check the tests")

    @pytest.mark.parametrize(
        "targets",
        [
            ("agent_id", "runtime_session_id"),
            ("runtime_session_id", "execution_id"),
            ("agent_id", "execution_id"),
            TARGETS,
        ],
    )
    def test_several_targets_are_rejected(self, targets: tuple[str, ...]) -> None:
        fields: Dict[str, Any] = {name: uuid.uuid4() for name in targets}

        with pytest.raises(ValidationError, match="Name exactly one of"):
            OperatorNoteCreate(text="Check the tests", **fields)


class TestOperatorNoteCreateBounds:
    """Text length and expiry are bounded."""

    def test_empty_text_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperatorNoteCreate(text="", agent_id=uuid.uuid4())

    def test_text_at_the_limit_is_accepted(self) -> None:
        note = OperatorNoteCreate(text="x" * MAX_NOTE_BODY_CHARS, agent_id=uuid.uuid4())

        assert len(note.text) == MAX_NOTE_BODY_CHARS

    def test_text_over_the_limit_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperatorNoteCreate(
                text="x" * (MAX_NOTE_BODY_CHARS + 1), agent_id=uuid.uuid4()
            )

    @pytest.mark.parametrize("seconds", [60, 7 * 24 * 60 * 60])
    def test_expiry_bounds_are_inclusive(self, seconds: int) -> None:
        note = OperatorNoteCreate(
            text="Check", agent_id=uuid.uuid4(), expires_in_seconds=seconds
        )

        assert note.expires_in_seconds == seconds

    @pytest.mark.parametrize("seconds", [59, 7 * 24 * 60 * 60 + 1])
    def test_expiry_outside_bounds_is_rejected(self, seconds: int) -> None:
        with pytest.raises(ValidationError):
            OperatorNoteCreate(
                text="Check", agent_id=uuid.uuid4(), expires_in_seconds=seconds
            )


class TestOperatorNotePendingRequestChannel:
    """A harness pull cannot claim gateway delivery."""

    def test_default_channel_is_hook(self) -> None:
        assert OperatorNotePendingRequest().channel == "hook"

    def test_gateway_channel_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OperatorNotePendingRequest(channel="gateway")
