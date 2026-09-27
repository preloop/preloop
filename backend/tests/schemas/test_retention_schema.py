"""Direct tests for the validation logic in ``preloop.schemas.retention``."""

import pytest
from pydantic import ValidationError

from preloop.models.models.legal_hold import HOLD_RESOURCE_TYPES
from preloop.schemas.retention import (
    LegalHoldCreate,
    LegalHoldRelease,
    RetentionSettingsUpdate,
)
from preloop.services.legal_hold import MAX_REASON_CHARS, MIN_REASON_CHARS
from preloop.services.retention_policy import RECORD_CLASSES

VALID_REASON = "x" * MIN_REASON_CHARS


class TestRetentionSettingsUpdate:
    """Only known record classes may be set."""

    def test_empty_update_is_accepted(self) -> None:
        assert RetentionSettingsUpdate().classes == {}

    def test_every_known_class_is_accepted(self) -> None:
        classes = {name: 400 for name in RECORD_CLASSES}
        classes[RECORD_CLASSES[0]] = None

        assert RetentionSettingsUpdate(classes=classes).classes == classes

    def test_unknown_classes_are_listed_sorted(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            RetentionSettingsUpdate(
                classes={"zz_unknown": 30, RECORD_CLASSES[0]: 30, "aa_unknown": 30}
            )

        assert "unknown record classes: aa_unknown, zz_unknown" in str(excinfo.value)


class TestLegalHoldCreate:
    """A hold names a known resource type and a bounded reason."""

    @pytest.mark.parametrize("resource_type", HOLD_RESOURCE_TYPES)
    def test_known_resource_type_is_accepted(self, resource_type: str) -> None:
        hold = LegalHoldCreate(
            resource_type=resource_type, resource_id="r-1", reason=VALID_REASON
        )

        assert hold.resource_type == resource_type

    def test_unknown_resource_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="resource_type must be one of"):
            LegalHoldCreate(
                resource_type="not_a_type", resource_id="r-1", reason=VALID_REASON
            )

    @pytest.mark.parametrize(
        "reason", ["x" * (MIN_REASON_CHARS - 1), "x" * (MAX_REASON_CHARS + 1)]
    )
    def test_reason_outside_bounds_is_rejected(self, reason: str) -> None:
        with pytest.raises(ValidationError):
            LegalHoldCreate(
                resource_type=HOLD_RESOURCE_TYPES[0],
                resource_id="r-1",
                reason=reason,
            )

    def test_resource_id_over_limit_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LegalHoldCreate(
                resource_type=HOLD_RESOURCE_TYPES[0],
                resource_id="r" * 256,
                reason=VALID_REASON,
            )


class TestLegalHoldRelease:
    """A release carries a reason within the same bounds."""

    def test_reason_at_minimum_is_accepted(self) -> None:
        assert LegalHoldRelease(reason=VALID_REASON).reason == VALID_REASON

    def test_short_reason_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LegalHoldRelease(reason="x" * (MIN_REASON_CHARS - 1))
