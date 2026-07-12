"""Tests for approval_workflow_service.py.

Tests for default approval workflow creation service functions.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4


from preloop.services.approval_workflow_service import (
    DEFAULT_APPROVAL_TYPE,
    create_default_approval_workflow_for_account,
    create_default_approval_workflow_background,
    repair_default_approval_workflow_for_account,
)


class TestCreateDefaultApprovalWorkflowForAccount:
    """Test create_default_approval_workflow_for_account function."""

    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_creates_default_policy_when_none_exist(
        self, mock_crud, mock_session_factory
    ):
        """Test that a default policy is created when no policies exist."""
        # Arrange
        account_id = uuid4()
        user_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)

        # No existing default policy
        mock_crud.get_default.return_value = None
        # No existing policies at all
        mock_crud.get_multi_by_account.return_value = []

        # Act
        create_default_approval_workflow_for_account(account_id, user_id)

        # Assert
        mock_crud.create.assert_called_once()
        call_args = mock_crud.create.call_args
        assert call_args.args[0] == mock_db
        assert call_args.kwargs["account_id"] == str(account_id)

        obj_in = call_args.kwargs["obj_in"]
        assert obj_in["name"] == "Default Approval Workflow"
        # The dialog dropdown labels this value as "Standard Human Approval".
        # Storing the dropdown's canonical value (rather than the legacy
        # "manual" synonym) keeps the type field populated when the account
        # owner opens the workflow editor.
        assert obj_in["approval_type"] == DEFAULT_APPROVAL_TYPE == "standard"
        assert obj_in["approval_mode"] == "standard"
        assert obj_in["is_default"] is True
        assert obj_in["approvals_required"] == 1
        assert obj_in["approver_user_ids"] == [str(user_id)]

        mock_db.close.assert_called_once()

    @patch("preloop.services.approval_workflow_service._resolve_account_owner_user_id")
    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_falls_back_to_account_owner_when_user_id_missing(
        self, mock_crud, mock_session_factory, mock_resolve
    ):
        """When ``user_id`` is omitted the service must resolve the account
        owner so the seeded default workflow always has an approver — an
        empty default workflow makes default-routed approvals impossible to
        act on."""
        account_id = uuid4()
        owner_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)

        mock_crud.get_default.return_value = None
        mock_crud.get_multi_by_account.return_value = []
        mock_resolve.return_value = owner_id

        create_default_approval_workflow_for_account(account_id, user_id=None)

        mock_resolve.assert_called_once_with(mock_db, account_id)
        mock_crud.create.assert_called_once()
        obj_in = mock_crud.create.call_args.kwargs["obj_in"]
        assert obj_in["approver_user_ids"] == [str(owner_id)]

    @patch("preloop.services.approval_workflow_service._resolve_account_owner_user_id")
    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_creates_policy_without_approver_when_no_owner_resolvable(
        self, mock_crud, mock_session_factory, mock_resolve
    ):
        """If neither ``user_id`` nor an account-owner can be resolved, the
        workflow is still created (so the account has a default to fall
        back to) but logs a warning instead of failing — and no approver
        is set."""
        account_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)

        mock_crud.get_default.return_value = None
        mock_crud.get_multi_by_account.return_value = []
        mock_resolve.return_value = None

        create_default_approval_workflow_for_account(account_id, user_id=None)

        mock_crud.create.assert_called_once()
        obj_in = mock_crud.create.call_args.kwargs["obj_in"]
        assert "approver_user_ids" not in obj_in

    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_skips_when_default_policy_exists(self, mock_crud, mock_session_factory):
        """Test that creation is skipped when default policy already exists."""
        # Arrange
        account_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)

        # Existing default policy
        mock_crud.get_default.return_value = MagicMock()

        # Act
        create_default_approval_workflow_for_account(account_id)

        # Assert
        mock_crud.create.assert_not_called()
        mock_db.close.assert_called_once()

    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_skips_when_any_policies_exist(self, mock_crud, mock_session_factory):
        """Test that creation is skipped when any policies exist for the account."""
        # Arrange
        account_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)

        # No default policy
        mock_crud.get_default.return_value = None
        # But other policies exist
        mock_crud.get_multi_by_account.return_value = [MagicMock()]

        # Act
        create_default_approval_workflow_for_account(account_id)

        # Assert
        mock_crud.create.assert_not_called()
        mock_db.close.assert_called_once()

    @patch("preloop.services.approval_workflow_service.logger")
    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_handles_exception_gracefully(
        self, mock_crud, mock_session_factory, mock_logger
    ):
        """Test that exceptions are logged but not raised."""
        # Arrange
        account_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)

        mock_crud.get_default.side_effect = Exception("Database error")

        # Act - should not raise
        create_default_approval_workflow_for_account(account_id)

        # Assert - verify logger.error was called with expected message
        mock_logger.error.assert_called_once()
        call_args = mock_logger.error.call_args
        assert "Failed to create default approval workflow" in call_args[0][0]
        mock_db.close.assert_called_once()

    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_closes_session_on_exception(self, mock_crud, mock_session_factory):
        """Test that database session is closed even when exception occurs."""
        # Arrange
        account_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)

        mock_crud.get_default.side_effect = Exception("Database error")

        # Act
        create_default_approval_workflow_for_account(account_id)

        # Assert
        mock_db.close.assert_called_once()


class TestRepairDefaultApprovalWorkflowForAccount:
    """Tests for repair_default_approval_workflow_for_account."""

    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_returns_false_when_no_default_workflow(
        self, mock_crud, mock_session_factory
    ):
        account_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)
        mock_crud.get_default.return_value = None

        assert (
            repair_default_approval_workflow_for_account(account_id, user_id=None)
            is False
        )
        mock_db.commit.assert_not_called()

    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_rewrites_legacy_manual_approval_type(
        self, mock_crud, mock_session_factory
    ):
        """The legacy ``approval_type="manual"`` value must be rewritten
        to the canonical ``standard`` so the dialog renders the type
        correctly."""
        account_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)

        existing = MagicMock()
        existing.approval_type = "manual"
        existing.approver_user_ids = [uuid4()]
        existing.approver_team_ids = None
        mock_crud.get_default.return_value = existing

        modified = repair_default_approval_workflow_for_account(
            account_id, user_id=None
        )

        assert modified is True
        assert existing.approval_type == DEFAULT_APPROVAL_TYPE
        mock_db.commit.assert_called_once()

    @patch("preloop.services.approval_workflow_service._resolve_account_owner_user_id")
    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_seeds_account_owner_when_no_approvers(
        self, mock_crud, mock_session_factory, mock_resolve
    ):
        """A default workflow with no approvers must be patched with the
        account owner so default-routed approvals can be acted on."""
        account_id = uuid4()
        owner_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)

        existing = MagicMock()
        existing.approval_type = "standard"
        existing.approver_user_ids = None
        existing.approver_team_ids = None
        mock_crud.get_default.return_value = existing
        mock_resolve.return_value = owner_id

        modified = repair_default_approval_workflow_for_account(
            account_id, user_id=None
        )

        assert modified is True
        assert existing.approver_user_ids == [owner_id]
        mock_db.commit.assert_called_once()

    @patch("preloop.services.approval_workflow_service.get_session_factory")
    @patch("preloop.services.approval_workflow_service.crud_approval_workflow")
    def test_no_op_when_workflow_is_already_healthy(
        self, mock_crud, mock_session_factory
    ):
        account_id = uuid4()
        mock_db = MagicMock()
        mock_session_factory.return_value = MagicMock(return_value=mock_db)

        existing = MagicMock()
        existing.approval_type = "standard"
        existing.approver_user_ids = [uuid4()]
        existing.approver_team_ids = None
        mock_crud.get_default.return_value = existing

        modified = repair_default_approval_workflow_for_account(
            account_id, user_id=None
        )

        assert modified is False
        mock_db.commit.assert_not_called()


class TestCreateDefaultApprovalWorkflowBackground:
    """Test create_default_approval_workflow_background function."""

    @patch(
        "preloop.services.approval_workflow_service.create_default_approval_workflow_for_account"
    )
    def test_calls_create_function(self, mock_create_func):
        """Test that background task calls the main creation function."""
        # Arrange
        account_id = uuid4()
        user_id = uuid4()

        # Act
        create_default_approval_workflow_background(account_id, user_id)

        # Assert
        mock_create_func.assert_called_once_with(account_id, user_id)

    @patch("preloop.services.approval_workflow_service.logger")
    @patch(
        "preloop.services.approval_workflow_service.create_default_approval_workflow_for_account"
    )
    def test_handles_exception_silently(self, mock_create_func, mock_logger):
        """Test that exceptions in background task are logged but not raised."""
        # Arrange
        account_id = uuid4()
        mock_create_func.side_effect = Exception("Background task error")

        # Act - should not raise
        create_default_approval_workflow_background(account_id)

        # Assert - verify logger.error was called with expected message
        mock_logger.error.assert_called_once()
        call_args = mock_logger.error.call_args
        assert "Background task failed" in call_args[0][0]

    @patch(
        "preloop.services.approval_workflow_service.create_default_approval_workflow_for_account"
    )
    def test_passes_none_user_id_correctly(self, mock_create_func):
        """Test that None user_id is passed correctly."""
        # Arrange
        account_id = uuid4()

        # Act
        create_default_approval_workflow_background(account_id, user_id=None)

        # Assert
        mock_create_func.assert_called_once_with(account_id, None)


class TestResolveAccountOwnerUserId:
    """Tests for the owner-resolution fallback used by workflow seeding."""

    def test_prefers_account_primary_user(self):
        """The account's primary_user_id (the owner set at signup) must win
        over the oldest-user fallback."""
        from preloop.services.approval_workflow_service import (
            _resolve_account_owner_user_id,
        )

        db = MagicMock()
        primary_user_id = uuid4()
        account = MagicMock(primary_user_id=primary_user_id)
        db.query.return_value.filter.return_value.first.return_value = account

        assert _resolve_account_owner_user_id(db, uuid4()) == primary_user_id

    def test_falls_back_to_oldest_user_when_primary_unset(self):
        from preloop.services.approval_workflow_service import (
            _resolve_account_owner_user_id,
        )

        db = MagicMock()
        account = MagicMock(primary_user_id=None)
        first_user = MagicMock(id=uuid4())
        db.query.return_value.filter.return_value.first.return_value = account
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = first_user

        assert _resolve_account_owner_user_id(db, uuid4()) == first_user.id

    def test_returns_none_when_account_and_users_missing(self):
        from preloop.services.approval_workflow_service import (
            _resolve_account_owner_user_id,
        )

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        assert _resolve_account_owner_user_id(db, uuid4()) is None
