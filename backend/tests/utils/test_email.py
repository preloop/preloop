"""Tests for email utility."""

import pytest
from unittest.mock import MagicMock, patch
from preloop.utils.email import (
    EmailError,
    send_email,
    send_verification_email,
    send_password_reset_email,
    send_invitation_email,
    send_tracker_registered_email,
    send_product_notification_email,
)


class TestSendEmail:
    """Test send_email function."""

    @patch("preloop.utils.email.smtplib.SMTP")
    @patch("preloop.utils.email.SMTP_USERNAME", "test@example.com")
    @patch("preloop.utils.email.SMTP_PASSWORD", "password")
    @patch("preloop.utils.email.SMTP_HOST", "smtp.example.com")
    @patch("preloop.utils.email.SMTP_PORT", 587)
    def test_send_email_text_only(self, mock_smtp):
        """Test sending email with text body only."""
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        send_email(
            to_email="recipient@example.com",
            subject="Test Subject",
            body_text="Test body",
        )

        mock_smtp.assert_called_once_with("smtp.example.com", 587)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("test@example.com", "password")
        mock_server.sendmail.assert_called_once()

    @patch("preloop.utils.email.smtplib.SMTP")
    @patch("preloop.utils.email.SMTP_USERNAME", "test@example.com")
    @patch("preloop.utils.email.SMTP_PASSWORD", "password")
    def test_send_email_with_html(self, mock_smtp):
        """Test sending email with both text and HTML bodies."""
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        send_email(
            to_email="recipient@example.com",
            subject="Test Subject",
            body_text="Test body",
            body_html="<html><body>Test body</body></html>",
        )

        mock_server.sendmail.assert_called_once()
        # Verify HTML was included
        call_args = mock_server.sendmail.call_args
        message_str = call_args[0][2]
        assert "text/html" in message_str
        assert "<html>" in message_str

    @patch("preloop.utils.email.smtplib.SMTP")
    def test_send_email_no_credentials_does_not_send(self, mock_smtp, monkeypatch):
        """Test that email logs warning and returns gracefully when credentials are missing."""
        # Use monkeypatch to modify module-level variables at runtime
        monkeypatch.setattr("preloop.utils.email.SMTP_USERNAME", "")
        monkeypatch.setattr("preloop.utils.email.SMTP_PASSWORD", "")

        # Call should succeed without raising
        send_email(
            to_email="recipient@example.com",
            subject="Test Subject",
            body_text="Test body",
        )

        # Verify SMTP connection was never attempted - this is the key behavior
        mock_smtp.assert_not_called()

    @patch("preloop.utils.email.smtplib.SMTP")
    @patch("preloop.utils.email.SMTP_USERNAME", "test@example.com")
    @patch("preloop.utils.email.SMTP_PASSWORD", "password")
    def test_send_email_smtp_error_raises_email_error(self, mock_smtp):
        """Test that SMTP errors raise EmailError."""
        mock_server = MagicMock()
        mock_server.sendmail.side_effect = Exception("SMTP connection failed")
        mock_smtp.return_value.__enter__.return_value = mock_server

        with pytest.raises(EmailError) as exc_info:
            send_email(
                to_email="recipient@example.com",
                subject="Test Subject",
                body_text="Test body",
            )

        assert "Failed to send email" in str(exc_info.value)
        assert "SMTP connection failed" in str(exc_info.value)

    @patch("preloop.utils.email.smtplib.SMTP")
    @patch("preloop.utils.email.SMTP_USERNAME", "test@example.com")
    @patch("preloop.utils.email.SMTP_PASSWORD", "password")
    def test_send_email_custom_from_address(self, mock_smtp):
        """Test sending email with custom from address."""
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        send_email(
            to_email="recipient@example.com",
            subject="Test Subject",
            body_text="Test body",
            from_email="custom@example.com",
        )

        call_args = mock_server.sendmail.call_args
        from_addr = call_args[0][0]
        assert from_addr == "custom@example.com"

    @patch("preloop.utils.email.smtplib.SMTP")
    @patch("preloop.utils.email.SMTP_USERNAME", "test@example.com")
    @patch("preloop.utils.email.SMTP_PASSWORD", "password")
    @patch("preloop.utils.email.settings")
    def test_send_email_includes_portal_url_in_subject(self, mock_settings, mock_smtp):
        """Test that portal URL is included in subject."""
        mock_settings.preloop_url = "https://preloop.ai"
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        send_email(
            to_email="recipient@example.com",
            subject="Test Subject",
            body_text="Test body",
        )

        call_args = mock_server.sendmail.call_args
        message_str = call_args[0][2]
        assert "preloop.ai" in message_str
        assert "Test Subject" in message_str


class TestSendVerificationEmail:
    """Test send_verification_email function."""

    @patch("preloop.utils.email.send_email")
    @patch("preloop.utils.email.PRELOOP_URL", "https://app.test.com")
    def test_send_verification_email_calls_send_email(self, mock_send_email):
        """Test that verification email calls send_email with correct params."""
        send_verification_email("user@example.com", "test_token_123")

        mock_send_email.assert_called_once()
        call_args = mock_send_email.call_args

        # Check recipient
        assert call_args[0][0] == "user@example.com"

        # Check subject
        subject = call_args[0][1]
        assert "Verify" in subject

        # Check body contains verification link
        text_body = call_args[0][2]
        assert "https://app.test.com/verify-email?token=test_token_123" in text_body

        # Check HTML body
        html_body = call_args[0][3]
        assert "test_token_123" in html_body
        assert "verify-email" in html_body

    @patch("preloop.utils.email.send_email")
    @patch("preloop.utils.email.APP_NAME", "Preloop")
    def test_send_verification_email_includes_welcome_message(self, mock_send_email):
        """Test that verification email includes welcome message."""
        send_verification_email("user@example.com", "token")

        call_args = mock_send_email.call_args
        text_body = call_args[0][2]
        assert "Welcome to Preloop" in text_body


class TestSendPasswordResetEmail:
    """Test send_password_reset_email function."""

    @patch("preloop.utils.email.send_email")
    @patch("preloop.utils.email.PRELOOP_URL", "https://app.test.com")
    def test_send_password_reset_email_calls_send_email(self, mock_send_email):
        """Test that password reset email calls send_email with correct params."""
        send_password_reset_email("user@example.com", "reset_token_456")

        mock_send_email.assert_called_once()
        call_args = mock_send_email.call_args

        # Check recipient
        assert call_args[0][0] == "user@example.com"

        # Check subject
        subject = call_args[0][1]
        assert "Reset" in subject
        assert "password" in subject.lower()

        # Check body contains reset link
        text_body = call_args[0][2]
        assert "https://app.test.com/reset-password?token=reset_token_456" in text_body

        # Check HTML body
        html_body = call_args[0][3]
        assert "reset_token_456" in html_body
        assert "reset-password" in html_body

    @patch("preloop.utils.email.send_email")
    def test_send_password_reset_email_includes_instructions(self, mock_send_email):
        """Test that password reset email includes instructions."""
        send_password_reset_email("user@example.com", "token")

        call_args = mock_send_email.call_args
        text_body = call_args[0][2]
        assert "set a new password" in text_body.lower()


class TestSendInvitationEmail:
    """Test send_invitation_email function."""

    @patch("preloop.utils.email.send_email")
    @patch("preloop.utils.email.PRELOOP_URL", "https://app.test.com")
    def test_send_invitation_email_calls_send_email(self, mock_send_email):
        """Test that invitation email calls send_email with correct params."""
        send_invitation_email(
            user_email="invitee@example.com",
            token="invite_token_123",
            organization_name="Test Org",
            invited_by="admin@example.com",
        )

        mock_send_email.assert_called_once()
        call_args = mock_send_email.call_args

        # Check recipient
        assert call_args[0][0] == "invitee@example.com"

        # Check subject includes organization name
        subject = call_args[0][1]
        assert "invited" in subject.lower()
        assert "Test Org" in subject

        # Check body contains invitation link
        text_body = call_args[0][2]
        assert (
            "https://app.test.com/invitations/accept?token=invite_token_123"
            in text_body
        )
        assert "Test Org" in text_body
        assert "admin@example.com" in text_body

        # Check HTML body
        html_body = call_args[0][3]
        assert "invite_token_123" in html_body
        assert "invitations/accept" in html_body
        assert "Test Org" in html_body

    @patch("preloop.utils.email.send_email")
    def test_send_invitation_email_includes_welcome_message(self, mock_send_email):
        """Test that invitation email includes welcome/greeting."""
        send_invitation_email(
            user_email="invitee@example.com",
            token="token",
            organization_name="Acme Corp",
            invited_by="boss@example.com",
        )

        call_args = mock_send_email.call_args
        text_body = call_args[0][2]
        assert "Hello" in text_body
        assert "invited" in text_body.lower()

    @patch("preloop.utils.email.send_email")
    def test_send_invitation_email_includes_expiration_notice(self, mock_send_email):
        """Test that invitation email includes expiration information."""
        send_invitation_email(
            user_email="invitee@example.com",
            token="token",
            organization_name="Test Org",
            invited_by="admin@example.com",
        )

        call_args = mock_send_email.call_args
        text_body = call_args[0][2]
        assert "7 days" in text_body or "expire" in text_body.lower()

    @patch("preloop.utils.email.send_email")
    def test_send_invitation_email_html_has_button(self, mock_send_email):
        """Test that HTML invitation email includes a styled button."""
        send_invitation_email(
            user_email="invitee@example.com",
            token="token",
            organization_name="Test Org",
            invited_by="admin@example.com",
        )

        call_args = mock_send_email.call_args
        html_body = call_args[0][3]
        # Check for button-like styling
        assert "button" in html_body.lower() or "class=" in html_body

    @patch("preloop.utils.email.send_email")
    def test_send_invitation_email_includes_inviter_name(self, mock_send_email):
        """Test that invitation email mentions who invited them."""
        send_invitation_email(
            user_email="invitee@example.com",
            token="token",
            organization_name="Test Org",
            invited_by="John Doe",
        )

        call_args = mock_send_email.call_args
        text_body = call_args[0][2]
        assert "John Doe" in text_body

    @patch("preloop.utils.email.send_email")
    @patch("preloop.utils.email.PRELOOP_URL", "https://custom.domain.com")
    def test_send_invitation_email_uses_custom_url(self, mock_send_email):
        """Test that invitation email uses configured PRELOOP_URL."""
        send_invitation_email(
            user_email="invitee@example.com",
            token="token",
            organization_name="Test Org",
            invited_by="admin@example.com",
        )

        call_args = mock_send_email.call_args
        text_body = call_args[0][2]
        assert "https://custom.domain.com/invitations/accept" in text_body


class TestSendTrackerRegisteredEmail:
    """Test send_tracker_registered_email function."""

    @patch("preloop.utils.email.send_email")
    @patch("preloop.utils.email.PRELOOP_URL", "https://app.test.com")
    def test_send_tracker_registered_email_github(self, mock_send_email):
        """Test tracker registered email for GitHub tracker."""
        send_tracker_registered_email(
            user_email="user@example.com",
            tracker_name="My GitHub Repo",
            tracker_type="github",
        )

        mock_send_email.assert_called_once()
        call_args = mock_send_email.call_args

        # Check recipient
        assert call_args[0][0] == "user@example.com"

        # Check subject includes tracker name and type
        subject = call_args[0][1]
        assert "My GitHub Repo" in subject
        assert "Github" in subject

        # Check body includes tracker details
        text_body = call_args[0][2]
        assert "My GitHub Repo" in text_body
        assert "Github" in text_body
        assert "https://app.test.com/console/trackers" in text_body

    @patch("preloop.utils.email.send_email")
    def test_send_tracker_registered_email_gitlab(self, mock_send_email):
        """Test tracker registered email for GitLab tracker."""
        send_tracker_registered_email(
            user_email="user@example.com",
            tracker_name="My GitLab Project",
            tracker_type="gitlab",
        )

        call_args = mock_send_email.call_args
        subject = call_args[0][1]
        assert "Gitlab" in subject

    @patch("preloop.utils.email.send_email")
    def test_send_tracker_registered_email_jira(self, mock_send_email):
        """Test tracker registered email for Jira tracker."""
        send_tracker_registered_email(
            user_email="user@example.com",
            tracker_name="My Jira Board",
            tracker_type="jira",
        )

        call_args = mock_send_email.call_args
        subject = call_args[0][1]
        assert "Jira" in subject


class TestSendProductNotificationEmail:
    """Test send_product_notification_email function."""

    @pytest.mark.asyncio
    @patch("preloop.utils.email.send_email")
    @patch("preloop.utils.email.settings")
    async def test_send_product_notification_email_with_user_data(
        self, mock_settings, mock_send_email
    ):
        """Test sending product notification with user data."""
        mock_settings.product_team_email = "team@example.com"

        user_data = {"username": "testuser", "email": "test@example.com"}
        await send_product_notification_email(
            user_data=user_data, tracker_data=None, source_ip="192.168.1.1"
        )

        mock_send_email.assert_called_once()
        call_args = mock_send_email.call_args

        # Check recipient
        assert call_args[0][0] == "team@example.com"

        # Check body includes user data
        text_body = call_args[0][2]
        assert "testuser" in text_body
        assert "test@example.com" in text_body
        assert "192.168.1.1" in text_body

    @pytest.mark.asyncio
    @patch("preloop.utils.email.send_email")
    @patch("preloop.utils.email.settings")
    async def test_send_product_notification_email_with_tracker_data(
        self, mock_settings, mock_send_email
    ):
        """Test sending product notification with tracker data."""
        mock_settings.product_team_email = "team@example.com"

        user_data = {"username": "testuser"}
        tracker_data = {"name": "My Tracker", "type": "github"}

        await send_product_notification_email(
            user_data=user_data, tracker_data=tracker_data, source_ip="192.168.1.1"
        )

        call_args = mock_send_email.call_args
        text_body = call_args[0][2]
        assert "My Tracker" in text_body
        assert "github" in text_body

    @pytest.mark.asyncio
    @patch("preloop.utils.email.send_email")
    @patch("preloop.utils.email.settings")
    async def test_send_product_notification_email_filters_sensitive_data(
        self, mock_settings, mock_send_email
    ):
        """Test that sensitive fields are filtered out."""
        mock_settings.product_team_email = "team@example.com"

        user_data = {
            "username": "testuser",
            "password": "secret123",
            "token": "abc123",
            "api_key": "key123",
        }

        await send_product_notification_email(
            user_data=user_data, tracker_data=None, source_ip="192.168.1.1"
        )

        call_args = mock_send_email.call_args
        text_body = call_args[0][2]

        # Should include username
        assert "testuser" in text_body

        # Should NOT include sensitive fields
        assert "secret123" not in text_body
        assert "abc123" not in text_body
        assert "key123" not in text_body

    @pytest.mark.asyncio
    @patch("preloop.utils.email.send_email")
    @patch("preloop.utils.email.settings")
    async def test_send_product_notification_email_html_body(
        self, mock_settings, mock_send_email
    ):
        """Test that HTML body is generated correctly."""
        mock_settings.product_team_email = "team@example.com"

        user_data = {"username": "testuser"}

        await send_product_notification_email(
            user_data=user_data, tracker_data=None, source_ip="192.168.1.1"
        )

        call_args = mock_send_email.call_args
        html_body = call_args[0][3]

        # Check HTML structure
        assert "<html>" in html_body
        assert "<body>" in html_body
        assert "<h2>Product Notification" in html_body
        assert "testuser" in html_body

    @pytest.mark.asyncio
    @patch("preloop.utils.email.settings")
    async def test_send_product_notification_email_raises_on_error(
        self, mock_settings, monkeypatch
    ):
        """Test that EmailError is logged but not raised when send_email fails."""
        mock_settings.product_team_email = "team@example.com"

        # Patch send_email to raise EmailError
        def mock_send_email(*args, **kwargs):
            raise EmailError("SMTP failed")

        monkeypatch.setattr("preloop.utils.email.send_email", mock_send_email)

        user_data = {"username": "testuser"}

        # Should not raise - errors are logged but not raised to avoid breaking flows
        # The key behavior is that no exception propagates
        await send_product_notification_email(
            user_data=user_data, tracker_data=None, source_ip="192.168.1.1"
        )
        # If we get here without exception, the test passes

    @pytest.mark.asyncio
    @patch("preloop.utils.email.settings")
    async def test_send_product_notification_email_wraps_unexpected_errors(
        self, mock_settings, monkeypatch
    ):
        """Test that unexpected errors are logged but not raised."""
        mock_settings.product_team_email = "team@example.com"

        # Patch send_email to raise RuntimeError
        def mock_send_email(*args, **kwargs):
            raise RuntimeError("Unexpected error")

        monkeypatch.setattr("preloop.utils.email.send_email", mock_send_email)

        user_data = {"username": "testuser"}

        # Should not raise - errors are logged but not raised to avoid breaking flows
        # The key behavior is that no exception propagates
        await send_product_notification_email(
            user_data=user_data, tracker_data=None, source_ip="192.168.1.1"
        )
        # If we get here without exception, the test passes


class TestEmailError:
    """Test EmailError exception."""

    def test_email_error_can_be_raised(self):
        """Test that EmailError can be raised with a message."""
        error = EmailError("Test error message")
        assert "Test error message" in str(error)
        with pytest.raises(EmailError, match="Test error message"):
            raise error

    def test_email_error_inherits_from_exception(self):
        """Test that EmailError inherits from Exception."""
        assert issubclass(EmailError, Exception)
