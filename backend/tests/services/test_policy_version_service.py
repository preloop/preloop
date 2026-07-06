"""Tests for PolicyVersionService.

The heavy policy export/apply dependencies are patched so these tests exercise
the service's own version-management logic (numbering, active flag, tag
handling, delete guards, error paths) against the real database.
"""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from preloop.services.policy import PolicyDocument, PolicyMetadata
from preloop.services.policy_version_service import PolicyVersionService


def empty_policy(name="Test Policy"):
    """Return a minimal valid PolicyDocument."""
    return PolicyDocument(
        metadata=PolicyMetadata(name=name),
        mcp_servers=[],
        approval_workflows=[],
        tools=[],
    )


@pytest.fixture
def account_id(test_user):
    return str(test_user.account_id)


@pytest.fixture
def service(db_session, account_id):
    return PolicyVersionService(db_session, account_id)


@pytest.fixture(autouse=True)
def patch_export(db_session):
    """Patch export_current_policy to avoid heavy DB introspection."""
    with patch(
        "preloop.services.policy_version_service.export_current_policy",
        return_value=empty_policy(),
    ):
        yield


class TestCreateSnapshot:
    def test_creates_with_incrementing_versions(self, service, db_session):
        s1 = service.create_snapshot(description="first")
        s2 = service.create_snapshot(description="second")
        assert s2.version_number == s1.version_number + 1
        assert s1.description == "first"

    def test_counts_recorded(self, service):
        with patch(
            "preloop.services.policy_version_service.export_current_policy",
            return_value=PolicyDocument(
                metadata=PolicyMetadata(name="p"),
                mcp_servers=[],
                approval_workflows=[],
                tools=[],
            ),
        ):
            snap = service.create_snapshot()
        assert snap.mcp_servers_count == 0
        assert snap.policies_count == 0
        assert snap.tools_count == 0

    def test_set_active_true_makes_active(self, service):
        snap = service.create_snapshot(set_active=True)
        assert snap.is_active is True

    def test_set_active_deactivates_previous(self, service):
        s1 = service.create_snapshot(set_active=True)
        s2 = service.create_snapshot(set_active=True)
        # Re-fetch s1 from DB
        refetched = service.get_snapshot(s1.id)
        assert refetched.is_active is False
        assert s2.is_active is True

    def test_snapshot_data_serialized(self, service):
        snap = service.create_snapshot()
        assert isinstance(snap.snapshot_data, dict)
        assert snap.snapshot_data["metadata"]["name"]


class TestGetAndList:
    def test_get_snapshot(self, service):
        snap = service.create_snapshot()
        assert service.get_snapshot(snap.id).id == snap.id

    def test_get_missing_returns_none(self, service):
        assert service.get_snapshot(uuid4()) is None

    def test_list_snapshots(self, service):
        service.create_snapshot()
        service.create_snapshot()
        snaps = service.list_snapshots()
        assert len(snaps) >= 2

    def test_get_active_snapshot(self, service):
        snap = service.create_snapshot(set_active=True)
        assert service.get_active_snapshot().id == snap.id


class TestTags:
    def test_update_tag(self, service):
        snap = service.create_snapshot()
        updated, err = service.update_tag(snap.id, "production")
        assert err is None
        assert updated.tag == "production"

    def test_update_tag_missing(self, service):
        updated, err = service.update_tag(uuid4(), "x")
        assert updated is None
        assert err == "Snapshot not found"

    def test_remove_tag(self, service):
        snap = service.create_snapshot(tag="staging")
        updated, err = service.remove_tag(snap.id)
        assert err is None
        assert updated.tag is None

    def test_get_snapshot_by_tag(self, service):
        service.create_snapshot(tag="release")
        found = service.get_snapshot_by_tag("release")
        assert found is not None
        assert found.tag == "release"

    def test_tag_moves_to_newest(self, service):
        s1 = service.create_snapshot(tag="prod")
        s2 = service.create_snapshot(tag="prod")
        # clear_tag should have removed the tag from s1
        refetched = service.get_snapshot(s1.id)
        assert refetched.tag is None
        assert s2.tag == "prod"


class TestDelete:
    def test_cannot_delete_active(self, service):
        snap = service.create_snapshot(set_active=True)
        ok, err = service.delete_snapshot(snap.id)
        assert ok is False
        assert "active" in err.lower()

    def test_delete_inactive(self, service):
        s1 = service.create_snapshot(set_active=True)
        s2 = service.create_snapshot(set_active=True)
        # s1 is now inactive
        ok, err = service.delete_snapshot(s1.id)
        assert ok is True
        assert err is None
        assert service.get_snapshot(s1.id) is None
        assert s2.is_active is True

    def test_delete_missing(self, service):
        ok, err = service.delete_snapshot(uuid4())
        assert ok is False
        assert err == "Snapshot not found"


class TestRollbackAndDiff:
    def test_compute_rollback_diff_missing(self, service):
        diff, err = service.compute_rollback_diff(uuid4())
        assert diff is None
        assert err == "Snapshot not found"

    def test_compute_rollback_diff_success(self, service):
        snap = service.create_snapshot()
        diff, err = service.compute_rollback_diff(snap.id)
        assert err is None
        assert diff is not None

    def test_rollback_missing_snapshot(self, service):
        diff, ok, err = service.rollback_to_snapshot(uuid4())
        assert ok is False
        assert err == "Snapshot not found"

    def test_rollback_preview_only_does_not_apply(self, service):
        snap = service.create_snapshot()
        with patch(
            "preloop.services.policy_version_service.PolicyApplier"
        ) as mock_applier:
            diff, ok, err = service.rollback_to_snapshot(snap.id, preview_only=True)
        assert ok is True
        assert err is None
        # PolicyApplier must not be used in preview mode
        mock_applier.assert_not_called()

    def test_rollback_applies_snapshot(self, service):
        snap = service.create_snapshot(set_active=False)
        with patch(
            "preloop.services.policy_version_service.PolicyApplier"
        ) as mock_applier:
            mock_applier.return_value.apply.return_value = SimpleNamespace(
                success=True, errors=[]
            )
            diff, ok, err = service.rollback_to_snapshot(snap.id)
        assert ok is True
        assert err is None
        # Snapshot should now be active
        assert service.get_snapshot(snap.id).is_active is True

    def test_rollback_apply_failure(self, service):
        snap = service.create_snapshot(set_active=False)
        with patch(
            "preloop.services.policy_version_service.PolicyApplier"
        ) as mock_applier:
            mock_applier.return_value.apply.return_value = SimpleNamespace(
                success=False, errors=["bad rule"]
            )
            diff, ok, err = service.rollback_to_snapshot(snap.id)
        assert ok is False
        assert "Failed to apply" in err


class TestPrune:
    def test_prune_returns_count(self, service):
        # Nothing old enough to prune; should return 0 without error.
        count = service.prune_snapshots(older_than_days=3650, keep_count=1)
        assert isinstance(count, int)
        assert count >= 0
