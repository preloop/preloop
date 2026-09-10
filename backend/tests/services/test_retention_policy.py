"""The six month floor, and what a stored value under it resolves to."""

import pytest

from preloop.config import settings
from preloop.services import retention_policy as policy


def test_default_is_twelve_months_when_the_account_says_nothing():
    resolved = policy.resolve_retention({}, record_class=policy.CLASS_AUDIT)

    assert resolved.days == 365
    assert resolved.source == "default"
    assert resolved.floored is False


def test_every_record_class_resolves():
    resolved = policy.resolve_all(None)

    assert set(resolved) == set(policy.RECORD_CLASSES)
    assert all(
        setting.days >= policy.ABSOLUTE_FLOOR_DAYS for setting in resolved.values()
    )


def test_account_value_above_the_floor_is_used_as_written():
    meta = {"retention": {"audit": 800}}

    resolved = policy.resolve_retention(meta, record_class=policy.CLASS_AUDIT)

    assert resolved.days == 800
    assert resolved.source == "account"
    assert resolved.floored is False


def test_a_write_below_the_floor_is_refused():
    with pytest.raises(policy.RetentionFloorError) as excinfo:
        policy.validate_retention_request({"audit": 30})

    assert excinfo.value.floor_days == 183
    assert excinfo.value.requested_days == 30
    assert "183" in str(excinfo.value)


def test_the_floor_is_exactly_six_months_and_inclusive():
    assert policy.validate_retention_request({"audit": 183}) == {"audit": 183}
    with pytest.raises(policy.RetentionFloorError):
        policy.validate_retention_request({"audit": 182})


def test_a_stored_value_under_the_floor_still_resolves_to_the_floor():
    """A hand-edited row, or a deployment that raised its floor afterwards.

    The floor is the reason this module exists, so it wins at read time too,
    not only at write time.
    """
    meta = {"retention": {"audit": 10}}

    resolved = policy.resolve_retention(meta, record_class=policy.CLASS_AUDIT)

    assert resolved.days == 183
    assert resolved.floored is True


def test_a_deployment_may_raise_the_floor_but_not_lower_it(monkeypatch):
    monkeypatch.setattr(settings, "retention_floor_days", 400, raising=False)
    assert policy.floor_days() == 400
    with pytest.raises(policy.RetentionFloorError):
        policy.validate_retention_request({"audit": 200})

    monkeypatch.setattr(settings, "retention_floor_days", 10, raising=False)
    assert policy.floor_days() == policy.ABSOLUTE_FLOOR_DAYS


def test_a_default_below_the_floor_is_raised_to_it(monkeypatch):
    monkeypatch.setattr(settings, "retention_default_days", 30, raising=False)

    assert policy.default_days() == policy.ABSOLUTE_FLOOR_DAYS


def test_an_unknown_record_class_is_refused_not_ignored():
    with pytest.raises(ValueError, match="unknown record class"):
        policy.validate_retention_request({"logs": 400})
    with pytest.raises(ValueError, match="unknown record class"):
        policy.resolve_retention({}, record_class="logs")


def test_garbage_in_the_bucket_does_not_decide_retention():
    meta = {"retention": {"audit": "soon", "usage": -5, "approvals": True}}

    assert policy.normalize_retention_store(meta) == {}
    assert policy.resolve_retention(meta, record_class=policy.CLASS_AUDIT).days == 365


def test_set_retention_leaves_the_rest_of_account_metadata_alone():
    meta = {"approval_window_max_seconds": 900, "retention": {"audit": 400}}

    updated = policy.set_retention(meta, values={"usage": 200})

    assert updated["approval_window_max_seconds"] == 900
    assert updated["retention"] == {"usage": 200}
    # The input is not mutated: callers hold the account's live dict.
    assert meta["retention"] == {"audit": 400}


def test_clearing_every_class_removes_the_bucket():
    meta = {"retention": {"audit": 400}, "other": 1}

    updated = policy.set_retention(meta, values={"audit": None})

    assert "retention" not in updated
    assert updated["other"] == 1


def test_retention_is_capped_so_a_table_is_not_kept_forever():
    cleaned = policy.validate_retention_request({"audit": 99999})

    assert cleaned["audit"] == policy.MAX_RETENTION_DAYS
