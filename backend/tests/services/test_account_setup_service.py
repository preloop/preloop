"""Tests for account setup service."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


from preloop.services.account_setup_service import (
    complete_new_account_setup,
    complete_new_account_setup_background,
    notify_admins_new_user_signup,
    notify_admins_user_joined_organization,
    notify_admins_user_login_after_inactivity,
    should_notify_on_login,
)


class TestShouldNotifyOnLogin:
    """Tests for should_notify_on_login function."""

    def test_returns_true_for_first_login(self):
        """Test that first login (None last_login) triggers notification."""
        result = should_notify_on_login(None)
        assert result is True

    def test_returns_true_for_inactive_user(self):
        """Test that user inactive for threshold days triggers notification."""
        last_login = datetime.now(timezone.utc) - timedelta(days=8)
        result = should_notify_on_login(last_login, days_threshold=7)
        assert result is True

    def test_returns_false_for_recent_login(self):
        """Test that recent login doesn't trigger notification."""
        last_login = datetime.now(timezone.utc) - timedelta(days=3)
        result = should_notify_on_login(last_login, days_threshold=7)
        assert result is False

    def test_returns_false_for_exactly_threshold(self):
        """Test behavior at exactly threshold days."""
        last_login = datetime.now(timezone.utc) - timedelta(days=7)
        result = should_notify_on_login(last_login, days_threshold=7)
        # At exactly threshold, should return True
        assert result is True

    def test_custom_threshold(self):
        """Test with custom threshold."""
        last_login = datetime.now(timezone.utc) - timedelta(days=15)
        result = should_notify_on_login(last_login, days_threshold=30)
        assert result is False

        result = should_notify_on_login(last_login, days_threshold=10)
        assert result is True


@patch("preloop.services.account_setup_service.TESTING_DOMAINS", [])
class TestNotifyAdminsNewUserSignup:
    """Tests for notify_admins_new_user_signup function."""

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_sends_notification_for_standard_signup(self, mock_notify):
        """Test that notification is sent for standard signup."""
        notify_admins_new_user_signup(
            username="testuser",
            email="test@test.com",
            signup_source="standard",
        )

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        subject = call_args[0][0]
        message = call_args[0][1]

        assert "testuser" in subject
        assert "testuser" in message
        assert "test@test.com" in message
        assert "standard" in message

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_includes_optional_fields(self, mock_notify):
        """Test that optional fields are included when provided."""
        account_id = uuid.uuid4()
        notify_admins_new_user_signup(
            username="testuser",
            email="test@test.com",
            signup_source="stripe",
            full_name="Test User",
            source_ip="192.168.1.1",
            account_id=account_id,
            organization_name="Test Org",
        )

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        message = call_args[0][1]
        message_html = call_args[0][2]

        assert "Test User" in message
        assert "192.168.1.1" in message
        assert str(account_id) in message
        assert "Test Org" in message
        assert "Test User" in message_html

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_skips_notification_for_testclient(self, mock_notify):
        """Test that notification is skipped for testclient IP."""
        notify_admins_new_user_signup(
            username="testuser",
            email="test@test.com",
            signup_source="standard",
            source_ip="testclient",
        )

        mock_notify.assert_not_called()

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_handles_notification_error(self, mock_notify):
        """Test that notification errors are handled gracefully."""
        mock_notify.side_effect = Exception("Notification failed")

        # Should not raise exception
        notify_admins_new_user_signup(
            username="testuser",
            email="test@test.com",
            signup_source="standard",
        )


@patch("preloop.services.account_setup_service.TESTING_DOMAINS", [])
class TestNotifyAdminsUserLoginAfterInactivity:
    """Tests for notify_admins_user_login_after_inactivity function."""

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_sends_notification_with_last_login(self, mock_notify):
        """Test notification with last login date."""
        last_login = datetime.now(timezone.utc) - timedelta(days=10)

        notify_admins_user_login_after_inactivity(
            username="testuser",
            email="test@test.com",
            last_login=last_login,
        )

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        subject = call_args[0][0]
        message = call_args[0][1]

        assert "10 Days" in subject
        assert "testuser" in message
        assert "test@test.com" in message
        assert "Days Inactive: 10" in message

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_handles_first_login(self, mock_notify):
        """Test notification for first ever login."""
        notify_admins_user_login_after_inactivity(
            username="testuser",
            email="test@test.com",
            last_login=None,
        )

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        subject = call_args[0][0]
        message = call_args[0][1]

        assert "First Login" in subject
        assert "Never" in message

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_includes_source_ip(self, mock_notify):
        """Test that source IP is included when provided."""
        notify_admins_user_login_after_inactivity(
            username="testuser",
            email="test@test.com",
            last_login=None,
            source_ip="10.0.0.1",
        )

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        message = call_args[0][1]

        assert "10.0.0.1" in message

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_skips_notification_for_testclient(self, mock_notify):
        """Test that notification is skipped for testclient IP."""
        notify_admins_user_login_after_inactivity(
            username="testuser",
            email="test@test.com",
            last_login=None,
            source_ip="testclient",
        )

        mock_notify.assert_not_called()

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_handles_notification_error(self, mock_notify):
        """Test that notification errors are handled gracefully."""
        mock_notify.side_effect = Exception("Notification failed")

        # Should not raise exception
        notify_admins_user_login_after_inactivity(
            username="testuser",
            email="test@test.com",
            last_login=None,
        )


@patch("preloop.services.account_setup_service.TESTING_DOMAINS", [])
class TestNotifyAdminsUserJoinedOrganization:
    """Tests for notify_admins_user_joined_organization function."""

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_sends_basic_notification(self, mock_notify):
        """Test sending basic notification."""
        notify_admins_user_joined_organization(
            username="newuser",
            email="new@test.com",
        )

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        subject = call_args[0][0]
        message = call_args[0][1]

        assert "Joined Organization" in subject
        assert "newuser" in message
        assert "new@test.com" in message

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_includes_optional_fields(self, mock_notify):
        """Test that optional fields are included."""
        account_id = uuid.uuid4()

        notify_admins_user_joined_organization(
            username="newuser",
            email="new@test.com",
            organization_name="Test Org",
            account_id=account_id,
            invited_by="admin@test.com",
        )

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        message = call_args[0][1]
        message_html = call_args[0][2]

        assert "Test Org" in message
        assert str(account_id) in message
        assert "admin@test.com" in message
        assert "Test Org" in message_html

    @patch("preloop.services.account_setup_service.notify_admins")
    def test_handles_notification_error(self, mock_notify):
        """Test that notification errors are handled gracefully."""
        mock_notify.side_effect = Exception("Notification failed")

        # Should not raise exception
        notify_admins_user_joined_organization(
            username="newuser",
            email="new@test.com",
        )


class TestCompleteNewAccountSetup:
    """Tests for complete_new_account_setup function."""

    @patch("preloop.services.account_setup_service.notify_admins_new_user_signup")
    @patch(
        "preloop.services.account_setup_service.create_default_approval_workflow_for_account"
    )
    @patch(
        "preloop.services.account_setup_service.ensure_global_presets_exist_background"
    )
    @patch("preloop.services.account_setup_service.send_verification_email")
    @patch("preloop.services.account_setup_service.create_email_verification_token")
    def test_completes_all_setup_tasks(
        self,
        mock_create_token,
        mock_send_email,
        mock_ensure_presets,
        mock_create_policy,
        mock_notify,
    ):
        """Test that all setup tasks are completed."""
        account_id = uuid.uuid4()
        user_id = uuid.uuid4()
        mock_create_token.return_value = "test_token"

        complete_new_account_setup(
            account_id=account_id,
            user_id=user_id,
            user_email="test@test.com",
            username="testuser",
        )

        # Verify all tasks were called
        # The link is bound to the new row, not only to its address.
        mock_create_token.assert_called_once_with("test@test.com", user_id=user_id)
        mock_send_email.assert_called_once_with(
            user_email="test@test.com",
            token="test_token",
            username="testuser",
        )
        mock_ensure_presets.assert_called_once_with()
        mock_create_policy.assert_called_once_with(account_id, user_id)
        mock_notify.assert_called_once()

    @patch("preloop.services.account_setup_service.notify_admins_new_user_signup")
    @patch(
        "preloop.services.account_setup_service.create_default_approval_workflow_for_account"
    )
    @patch(
        "preloop.services.account_setup_service.ensure_global_presets_exist_background"
    )
    @patch("preloop.services.account_setup_service.send_verification_email")
    @patch("preloop.services.account_setup_service.create_email_verification_token")
    def test_skips_verification_email_when_disabled(
        self,
        mock_create_token,
        mock_send_email,
        mock_ensure_presets,
        mock_create_policy,
        mock_notify,
    ):
        """Test that verification email is skipped when disabled."""
        account_id = uuid.uuid4()
        user_id = uuid.uuid4()

        complete_new_account_setup(
            account_id=account_id,
            user_id=user_id,
            user_email="test@test.com",
            username="testuser",
            send_verification=False,
        )

        mock_create_token.assert_not_called()
        mock_send_email.assert_not_called()
        mock_ensure_presets.assert_called_once_with()
        mock_create_policy.assert_called_once()
        mock_notify.assert_called_once()

    @patch("preloop.services.account_setup_service.notify_admins_new_user_signup")
    @patch(
        "preloop.services.account_setup_service.create_default_approval_workflow_for_account"
    )
    @patch(
        "preloop.services.account_setup_service.ensure_global_presets_exist_background"
    )
    @patch("preloop.services.account_setup_service.send_verification_email")
    @patch("preloop.services.account_setup_service.create_email_verification_token")
    def test_passes_optional_params_to_notify(
        self,
        mock_create_token,
        mock_send_email,
        mock_ensure_presets,
        mock_create_policy,
        mock_notify,
    ):
        """Test that optional parameters are passed to notification."""
        account_id = uuid.uuid4()
        user_id = uuid.uuid4()
        mock_create_token.return_value = "token"

        complete_new_account_setup(
            account_id=account_id,
            user_id=user_id,
            user_email="test@test.com",
            username="testuser",
            full_name="Test User",
            organization_name="Test Org",
            signup_source="stripe",
            source_ip="192.168.1.1",
        )

        mock_notify.assert_called_once_with(
            username="testuser",
            email="test@test.com",
            signup_source="stripe",
            full_name="Test User",
            source_ip="192.168.1.1",
            account_id=account_id,
            organization_name="Test Org",
        )

    @patch("preloop.services.account_setup_service.notify_admins_new_user_signup")
    @patch(
        "preloop.services.account_setup_service.create_default_approval_workflow_for_account"
    )
    @patch(
        "preloop.services.account_setup_service.ensure_global_presets_exist_background"
    )
    @patch("preloop.services.account_setup_service.send_verification_email")
    @patch("preloop.services.account_setup_service.create_email_verification_token")
    def test_handles_verification_email_error(
        self,
        mock_create_token,
        mock_send_email,
        mock_ensure_presets,
        mock_create_policy,
        mock_notify,
    ):
        """Test that verification email errors are handled gracefully."""
        account_id = uuid.uuid4()
        user_id = uuid.uuid4()
        mock_create_token.return_value = "token"
        mock_send_email.side_effect = Exception("Email failed")

        # Should not raise exception, should continue with other tasks
        complete_new_account_setup(
            account_id=account_id,
            user_id=user_id,
            user_email="test@test.com",
            username="testuser",
        )

        # Other tasks should still be called
        mock_ensure_presets.assert_called_once_with()
        mock_create_policy.assert_called_once()
        mock_notify.assert_called_once()

    @patch("preloop.services.account_setup_service.notify_admins_new_user_signup")
    @patch(
        "preloop.services.account_setup_service.create_default_approval_workflow_for_account"
    )
    @patch(
        "preloop.services.account_setup_service.ensure_global_presets_exist_background"
    )
    @patch("preloop.services.account_setup_service.send_verification_email")
    @patch("preloop.services.account_setup_service.create_email_verification_token")
    def test_handles_policy_creation_error(
        self,
        mock_create_token,
        mock_send_email,
        mock_ensure_presets,
        mock_create_policy,
        mock_notify,
    ):
        """Test that policy creation errors are handled gracefully."""
        account_id = uuid.uuid4()
        user_id = uuid.uuid4()
        mock_create_token.return_value = "token"
        mock_create_policy.side_effect = Exception("Policy creation failed")

        # Should not raise exception, should continue with other tasks
        complete_new_account_setup(
            account_id=account_id,
            user_id=user_id,
            user_email="test@test.com",
            username="testuser",
        )

        # Notification should still be called
        mock_notify.assert_called_once()

    @patch("preloop.services.account_setup_service.notify_admins_new_user_signup")
    @patch(
        "preloop.services.account_setup_service.create_default_approval_workflow_for_account"
    )
    @patch(
        "preloop.services.account_setup_service.ensure_global_presets_exist_background"
    )
    @patch("preloop.services.account_setup_service.send_verification_email")
    @patch("preloop.services.account_setup_service.create_email_verification_token")
    def test_handles_preset_ensure_error(
        self,
        mock_create_token,
        mock_send_email,
        mock_ensure_presets,
        mock_create_policy,
        mock_notify,
    ):
        """Test that preset setup errors are handled gracefully."""
        account_id = uuid.uuid4()
        user_id = uuid.uuid4()
        mock_create_token.return_value = "token"
        mock_ensure_presets.side_effect = Exception("Preset setup failed")

        complete_new_account_setup(
            account_id=account_id,
            user_id=user_id,
            user_email="test@test.com",
            username="testuser",
        )

        mock_create_policy.assert_called_once()
        mock_notify.assert_called_once()

    @patch("preloop.services.account_setup_service.notify_admins_new_user_signup")
    @patch(
        "preloop.services.account_setup_service.create_default_approval_workflow_for_account"
    )
    @patch(
        "preloop.services.account_setup_service.ensure_global_presets_exist_background"
    )
    @patch("preloop.services.account_setup_service.send_verification_email")
    @patch("preloop.services.account_setup_service.create_email_verification_token")
    def test_handles_notification_error(
        self,
        mock_create_token,
        mock_send_email,
        mock_ensure_presets,
        mock_create_policy,
        mock_notify,
    ):
        """Test that notification errors are handled gracefully."""
        account_id = uuid.uuid4()
        user_id = uuid.uuid4()
        mock_create_token.return_value = "token"
        mock_notify.side_effect = Exception("Notification failed")

        # Should not raise exception
        complete_new_account_setup(
            account_id=account_id,
            user_id=user_id,
            user_email="test@test.com",
            username="testuser",
        )


class TestCompleteNewAccountSetupBackground:
    """Tests for complete_new_account_setup_background function."""

    @patch("preloop.services.account_setup_service.complete_new_account_setup")
    def test_calls_complete_new_account_setup(self, mock_complete):
        """Test that it calls complete_new_account_setup with correct params."""
        account_id = uuid.uuid4()
        user_id = uuid.uuid4()

        complete_new_account_setup_background(
            account_id=account_id,
            user_id=user_id,
            user_email="test@test.com",
            username="testuser",
            full_name="Test User",
            organization_name="Test Org",
            signup_source="stripe",
            source_ip="192.168.1.1",
            send_verification=False,
        )

        mock_complete.assert_called_once_with(
            account_id=account_id,
            user_id=user_id,
            user_email="test@test.com",
            username="testuser",
            full_name="Test User",
            organization_name="Test Org",
            signup_source="stripe",
            source_ip="192.168.1.1",
            send_verification=False,
        )

    @patch("preloop.services.account_setup_service.complete_new_account_setup")
    def test_handles_errors_gracefully(self, mock_complete):
        """Test that errors are handled gracefully."""
        account_id = uuid.uuid4()
        user_id = uuid.uuid4()
        mock_complete.side_effect = Exception("Setup failed")

        # Should not raise exception
        complete_new_account_setup_background(
            account_id=account_id,
            user_id=user_id,
            user_email="test@test.com",
            username="testuser",
        )
