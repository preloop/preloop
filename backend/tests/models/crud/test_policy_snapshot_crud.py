"""Tests for PolicySnapshot CRUD operations."""

from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud import crud_policy_snapshot


class TestPolicySnapshotCRUD:
    """Test CRUD operations for PolicySnapshot."""

    def test_create_and_get(self, db_session: Session, create_account):
        """Test creating a policy snapshot and retrieving by ID."""
        account = create_account()
        snapshot_data = {"mcp_servers": [], "policies": [], "tools": []}

        obj_in = {
            "account_id": account.id,
            "version_number": 1,
            "snapshot_data": snapshot_data,
            "is_active": True,
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        snapshot = crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        assert snapshot.id is not None
        assert snapshot.account_id == account.id
        assert snapshot.version_number == 1
        assert snapshot.snapshot_data == snapshot_data
        assert snapshot.is_active is True

        found = crud_policy_snapshot.get(
            db_session, id=snapshot.id, account_id=str(account.id)
        )
        assert found is not None
        assert found.id == snapshot.id

    def test_get_without_account_id(self, db_session: Session, create_account):
        """Test get without account_id filter returns snapshot."""
        account = create_account()
        obj_in = {
            "account_id": account.id,
            "version_number": 1,
            "snapshot_data": {},
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        snapshot = crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        found = crud_policy_snapshot.get(db_session, id=snapshot.id)
        assert found is not None
        assert found.id == snapshot.id

    def test_get_not_found(self, db_session: Session, create_account):
        """Test get returns None for non-existent snapshot."""
        account = create_account()
        result = crud_policy_snapshot.get(
            db_session, id=uuid4(), account_id=str(account.id)
        )
        assert result is None

    def test_get_by_version_number(self, db_session: Session, create_account):
        """Test retrieving snapshot by version number."""
        account = create_account()
        obj_in = {
            "account_id": account.id,
            "version_number": 42,
            "snapshot_data": {},
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        found = crud_policy_snapshot.get_by_version_number(
            db_session, account_id=str(account.id), version_number=42
        )
        assert found is not None
        assert found.version_number == 42

    def test_get_by_version_number_not_found(self, db_session: Session, create_account):
        """Test get_by_version_number returns None when not found."""
        account = create_account()
        result = crud_policy_snapshot.get_by_version_number(
            db_session, account_id=str(account.id), version_number=999
        )
        assert result is None

    def test_get_by_tag(self, db_session: Session, create_account):
        """Test retrieving snapshot by tag."""
        account = create_account()
        obj_in = {
            "account_id": account.id,
            "version_number": 1,
            "tag": "production",
            "snapshot_data": {},
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        found = crud_policy_snapshot.get_by_tag(
            db_session, account_id=str(account.id), tag="production"
        )
        assert found is not None
        assert found.tag == "production"

    def test_get_active(self, db_session: Session, create_account):
        """Test retrieving active snapshot."""
        account = create_account()
        obj_in = {
            "account_id": account.id,
            "version_number": 1,
            "snapshot_data": {},
            "is_active": True,
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        found = crud_policy_snapshot.get_active(db_session, account_id=str(account.id))
        assert found is not None
        assert found.is_active is True

    def test_get_active_not_found(self, db_session: Session, create_account):
        """Test get_active returns None when no active snapshot."""
        account = create_account()
        obj_in = {
            "account_id": account.id,
            "version_number": 1,
            "snapshot_data": {},
            "is_active": False,
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        result = crud_policy_snapshot.get_active(db_session, account_id=str(account.id))
        assert result is None

    def test_get_multi_by_account(self, db_session: Session, create_account):
        """Test retrieving snapshots for an account."""
        account = create_account()
        for i in range(3):
            obj_in = {
                "account_id": account.id,
                "version_number": i + 1,
                "snapshot_data": {},
                "mcp_servers_count": 0,
                "policies_count": 0,
                "tools_count": 0,
            }
            crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        snapshots = crud_policy_snapshot.get_multi_by_account(
            db_session, account_id=str(account.id)
        )
        assert len(snapshots) == 3
        # Ordered by version_number desc
        assert snapshots[0].version_number == 3
        assert snapshots[2].version_number == 1

    def test_get_multi_by_account_with_pagination(
        self, db_session: Session, create_account
    ):
        """Test get_multi_by_account with skip and limit."""
        account = create_account()
        for i in range(5):
            obj_in = {
                "account_id": account.id,
                "version_number": i + 1,
                "snapshot_data": {},
                "mcp_servers_count": 0,
                "policies_count": 0,
                "tools_count": 0,
            }
            crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        snapshots = crud_policy_snapshot.get_multi_by_account(
            db_session, account_id=str(account.id), skip=1, limit=2
        )
        assert len(snapshots) == 2

    def test_get_next_version_number_empty(self, db_session: Session, create_account):
        """Test get_next_version_number when no snapshots exist."""
        account = create_account()
        next_ver = crud_policy_snapshot.get_next_version_number(
            db_session, account_id=str(account.id)
        )
        assert next_ver == 1

    def test_get_next_version_number_existing(
        self, db_session: Session, create_account
    ):
        """Test get_next_version_number when snapshots exist."""
        account = create_account()
        obj_in = {
            "account_id": account.id,
            "version_number": 5,
            "snapshot_data": {},
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        next_ver = crud_policy_snapshot.get_next_version_number(
            db_session, account_id=str(account.id)
        )
        assert next_ver == 6

    def test_set_active(self, db_session: Session, create_account):
        """Test setting a snapshot as active."""
        account = create_account()
        obj_in1 = {
            "account_id": account.id,
            "version_number": 1,
            "snapshot_data": {},
            "is_active": True,
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        obj_in2 = {
            "account_id": account.id,
            "version_number": 2,
            "snapshot_data": {},
            "is_active": False,
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        snap1 = crud_policy_snapshot.create(db_session, obj_in=obj_in1)
        snap2 = crud_policy_snapshot.create(db_session, obj_in=obj_in2)
        db_session.flush()

        result = crud_policy_snapshot.set_active(
            db_session, account_id=str(account.id), snapshot_id=snap2.id
        )
        assert result is not None
        assert result.id == snap2.id
        assert result.is_active is True

        # First snapshot should be deactivated
        db_session.refresh(snap1)
        assert snap1.is_active is False

    def test_clear_tag(self, db_session: Session, create_account):
        """Test clearing a tag from a snapshot."""
        account = create_account()
        obj_in = {
            "account_id": account.id,
            "version_number": 1,
            "tag": "staging",
            "snapshot_data": {},
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        snapshot = crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        result = crud_policy_snapshot.clear_tag(
            db_session, account_id=str(account.id), tag="staging"
        )
        assert result is not None
        db_session.flush()
        db_session.refresh(snapshot)
        assert snapshot.tag is None

    def test_update_tag(self, db_session: Session, create_account):
        """Test updating tag on a snapshot."""
        account = create_account()
        obj_in = {
            "account_id": account.id,
            "version_number": 1,
            "snapshot_data": {},
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        snapshot = crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        result = crud_policy_snapshot.update_tag(
            db_session,
            snapshot_id=snapshot.id,
            account_id=str(account.id),
            tag="v1.0",
        )
        assert result is not None
        db_session.flush()
        db_session.refresh(snapshot)
        assert snapshot.tag == "v1.0"

    def test_remove(self, db_session: Session, create_account):
        """Test removing a policy snapshot."""
        account = create_account()
        obj_in = {
            "account_id": account.id,
            "version_number": 1,
            "snapshot_data": {},
            "mcp_servers_count": 0,
            "policies_count": 0,
            "tools_count": 0,
        }
        snapshot = crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()
        snapshot_id = snapshot.id

        result = crud_policy_snapshot.remove(
            db_session, id=snapshot_id, account_id=str(account.id)
        )
        assert result is not None
        assert result.id == snapshot_id
        db_session.flush()

        found = crud_policy_snapshot.get(db_session, id=snapshot_id)
        assert found is None

    def test_remove_not_found(self, db_session: Session, create_account):
        """Test remove returns None for non-existent snapshot."""
        account = create_account()
        result = crud_policy_snapshot.remove(
            db_session, id=uuid4(), account_id=str(account.id)
        )
        assert result is None

    def test_count_by_account(self, db_session: Session, create_account):
        """Test counting snapshots by account."""
        account = create_account()
        for i in range(3):
            obj_in = {
                "account_id": account.id,
                "version_number": i + 1,
                "snapshot_data": {},
                "mcp_servers_count": 0,
                "policies_count": 0,
                "tools_count": 0,
            }
            crud_policy_snapshot.create(db_session, obj_in=obj_in)
        db_session.flush()

        count = crud_policy_snapshot.count_by_account(
            db_session, account_id=str(account.id)
        )
        assert count == 3
