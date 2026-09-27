"""Service for handling post-account and post-user creation tasks.

This service centralizes all the tasks that need to happen after:
1. A new account is created (standard registration, Stripe checkout)
2. A new user is created (registration, Stripe, invitation acceptance)
3. A user logs in after inactivity

This ensures consistent behavior across all signup/login paths.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from preloop.services.approval_workflow_service import (
    create_default_approval_workflow_for_account,
)
from preloop.services.flow_presets_service import ensure_global_presets_exist_background
from preloop.sync.tasks import notify_admins
from preloop.utils.email import send_verification_email
from preloop.utils.tokens import create_email_verification_token

logger = logging.getLogger(__name__)

TESTING_DOMAINS = [
    "preloop.ai",
    "spacecode.ai",
    "spacebridge.io",
    "example.com",
    "test.com",
    "dmo.ai",
]


def notify_admins_new_user_signup(
    username: str,
    email: str,
    signup_source: str,
    full_name: Optional[str] = None,
    source_ip: Optional[str] = None,
    account_id: Optional[UUID] = None,
    organization_name: Optional[str] = None,
) -> None:
    """
    Notify admins about a new user signup.

    This sends notifications via email, Slack, and Mattermost (if configured).

    Args:
        username: The new user's username.
        email: The new user's email.
        signup_source: How the user signed up (e.g., 'standard', 'stripe', 'invitation').
        full_name: The user's full name (optional).
        source_ip: The IP address from which the signup occurred (optional).
        account_id: The account UUID (optional).
        organization_name: The organization name (optional).
    """
    if source_ip == "testclient":
        logger.info(
            f"Skipping admin notification for new user signup for test client: {source_ip}"
        )
        return

    if email.split("@")[1] in TESTING_DOMAINS:
        logger.info(
            f"Skipping admin notification for new user signup for email: {email}"
        )
        return

    subject = f"New User Signup: {username}"

    message_parts = [
        f"A new user has signed up via {signup_source}.",
        "",
        f"Username: {username}",
        f"Email: {email}",
    ]

    if full_name:
        message_parts.append(f"Full Name: {full_name}")

    if organization_name:
        message_parts.append(f"Organization: {organization_name}")

    if account_id:
        message_parts.append(f"Account ID: {account_id}")

    if source_ip:
        message_parts.append(f"Source IP: {source_ip}")

    message = "\n".join(message_parts)

    # Build HTML version
    html_parts = [
        f"<h2>New User Signup via {signup_source}</h2>",
        f"<p><strong>Username:</strong> {username}</p>",
        f"<p><strong>Email:</strong> {email}</p>",
    ]

    if full_name:
        html_parts.append(f"<p><strong>Full Name:</strong> {full_name}</p>")

    if organization_name:
        html_parts.append(f"<p><strong>Organization:</strong> {organization_name}</p>")

    if account_id:
        html_parts.append(f"<p><strong>Account ID:</strong> {account_id}</p>")

    if source_ip:
        html_parts.append(f"<p><strong>Source IP:</strong> {source_ip}</p>")

    message_html = "\n".join(html_parts)

    try:
        notify_admins(subject, message, message_html)
        logger.info(f"Admin notification sent for new user signup: {username}")
    except Exception as e:
        logger.error(f"Failed to notify admins about new user signup: {e}")


def notify_admins_user_login_after_inactivity(
    username: str,
    email: str,
    last_login: Optional[datetime],
    source_ip: Optional[str] = None,
) -> None:
    """
    Notify admins when a user logs in after being inactive for 7+ days.

    Args:
        username: The user's username.
        email: The user's email.
        last_login: When the user last logged in.
        source_ip: The IP address from which the login occurred (optional).
    """
    if source_ip == "testclient" or username == "spacetester":
        logger.info(
            f"Skipping admin notification for user login after inactivity for test client: {source_ip} or username: {username}"
        )
        return

    if email.split("@")[1] in TESTING_DOMAINS:
        logger.info(f"Skipping admin notification for login for email: {email}")
        return

    if last_login:
        days_inactive = (datetime.now(timezone.utc) - last_login).days
        subject = f"User Login After {days_inactive} Days: {username}"
        last_login_str = last_login.isoformat()
    else:
        subject = f"First Login: {username}"
        last_login_str = "Never"
        days_inactive = None

    message_parts = [
        "A user has logged in after a period of inactivity.",
        "",
        f"Username: {username}",
        f"Email: {email}",
        f"Last Login: {last_login_str}",
    ]

    if days_inactive is not None:
        message_parts.append(f"Days Inactive: {days_inactive}")

    if source_ip:
        message_parts.append(f"Source IP: {source_ip}")

    message = "\n".join(message_parts)

    # Build HTML version
    html_parts = [
        "<h2>User Login After Inactivity</h2>",
        f"<p><strong>Username:</strong> {username}</p>",
        f"<p><strong>Email:</strong> {email}</p>",
        f"<p><strong>Last Login:</strong> {last_login_str}</p>",
    ]

    if days_inactive is not None:
        html_parts.append(f"<p><strong>Days Inactive:</strong> {days_inactive}</p>")

    if source_ip:
        html_parts.append(f"<p><strong>Source IP:</strong> {source_ip}</p>")

    message_html = "\n".join(html_parts)

    try:
        notify_admins(subject, message, message_html)
        logger.info(
            f"Admin notification sent for user login after inactivity: {username}"
        )
    except Exception as e:
        logger.error(f"Failed to notify admins about user login: {e}")


def notify_admins_user_joined_organization(
    username: str,
    email: str,
    organization_name: Optional[str] = None,
    account_id: Optional[UUID] = None,
    invited_by: Optional[str] = None,
) -> None:
    """
    Notify admins when a new user joins an organization via invitation.

    Args:
        username: The new user's username.
        email: The new user's email.
        organization_name: The organization name (optional).
        account_id: The account UUID (optional).
        invited_by: Who invited the user (optional).
    """
    subject = f"User Joined Organization: {username}"

    message_parts = [
        "A new user has joined an organization via invitation.",
        "",
        f"Username: {username}",
        f"Email: {email}",
    ]

    if organization_name:
        message_parts.append(f"Organization: {organization_name}")

    if account_id:
        message_parts.append(f"Account ID: {account_id}")

    if invited_by:
        message_parts.append(f"Invited By: {invited_by}")

    message = "\n".join(message_parts)

    # Build HTML version
    html_parts = [
        "<h2>User Joined Organization</h2>",
        f"<p><strong>Username:</strong> {username}</p>",
        f"<p><strong>Email:</strong> {email}</p>",
    ]

    if organization_name:
        html_parts.append(f"<p><strong>Organization:</strong> {organization_name}</p>")

    if account_id:
        html_parts.append(f"<p><strong>Account ID:</strong> {account_id}</p>")

    if invited_by:
        html_parts.append(f"<p><strong>Invited By:</strong> {invited_by}</p>")

    message_html = "\n".join(html_parts)

    try:
        notify_admins(subject, message, message_html)
        logger.info(f"Admin notification sent for user joined organization: {username}")
    except Exception as e:
        logger.error(f"Failed to notify admins about user joining organization: {e}")


def should_notify_on_login(
    last_login: Optional[datetime], days_threshold: int = 7
) -> bool:
    """
    Check if we should notify admins about a user login.

    Returns True if:
    - User has never logged in before (last_login is None)
    - User hasn't logged in for more than `days_threshold` days

    Args:
        last_login: When the user last logged in.
        days_threshold: Number of days of inactivity to trigger notification.

    Returns:
        True if notification should be sent, False otherwise.
    """
    if last_login is None:
        # First login ever
        return True

    days_since_login = (datetime.now(timezone.utc) - last_login).days
    return days_since_login >= days_threshold


def complete_new_account_setup(
    account_id: UUID,
    user_id: UUID,
    user_email: str,
    username: str,
    full_name: Optional[str] = None,
    organization_name: Optional[str] = None,
    signup_source: str = "standard",
    source_ip: Optional[str] = None,
    send_verification: bool = True,
) -> None:
    """
    Complete all post-account-creation setup tasks.

    This should be called after creating a new account and its primary user.
    It handles:
    1. Sending verification email (if enabled)
    2. Ensuring global flow presets are available
    3. Creating default approval workflow for the account
    4. Notifying admins about the new signup

    Args:
        account_id: The new account's UUID.
        user_id: The new user's UUID.
        user_email: The new user's email.
        username: The new user's username.
        full_name: The user's full name (optional).
        organization_name: The organization name (optional).
        signup_source: How the user signed up (e.g., 'standard', 'stripe').
        source_ip: The IP address from which the signup occurred (optional).
        send_verification: Whether to send verification email (default True).
    """
    logger.info(
        f"Completing account setup for account {account_id}, user {username} "
        f"(source: {signup_source})"
    )

    # 1. Send verification email
    if send_verification:
        try:
            token = create_email_verification_token(user_email, user_id=user_id)
            send_verification_email(
                user_email=user_email, token=token, username=username
            )
            logger.info(f"Verification email sent to {user_email}")
        except Exception as e:
            logger.error(f"Failed to send verification email to {user_email}: {e}")

    # 2. Ensure global flow presets exist
    try:
        ensure_global_presets_exist_background()
        logger.info("Global flow presets ensured for new account setup")
    except Exception as e:
        logger.error(f"Failed to ensure global flow presets: {e}")

    # 3. Create default approval workflow
    try:
        create_default_approval_workflow_for_account(account_id, user_id)
        logger.info(f"Default approval workflow created for account {account_id}")
    except Exception as e:
        logger.error(
            f"Failed to create default approval workflow for account {account_id}: {e}"
        )

    # 4. Notify admins about new signup
    try:
        notify_admins_new_user_signup(
            username=username,
            email=user_email,
            signup_source=signup_source,
            full_name=full_name,
            source_ip=source_ip,
            account_id=account_id,
            organization_name=organization_name,
        )
    except Exception as e:
        logger.error(f"Failed to notify admins about new signup: {e}")

    logger.info(f"Account setup completed for account {account_id}")


def complete_new_account_setup_background(
    account_id: UUID,
    user_id: UUID,
    user_email: str,
    username: str,
    full_name: Optional[str] = None,
    organization_name: Optional[str] = None,
    signup_source: str = "standard",
    source_ip: Optional[str] = None,
    send_verification: bool = True,
) -> None:
    """
    Background task wrapper for complete_new_account_setup.

    This function is designed to be called via FastAPI's BackgroundTasks
    to avoid blocking the response.

    Args:
        Same as complete_new_account_setup.
    """
    try:
        complete_new_account_setup(
            account_id=account_id,
            user_id=user_id,
            user_email=user_email,
            username=username,
            full_name=full_name,
            organization_name=organization_name,
            signup_source=signup_source,
            source_ip=source_ip,
            send_verification=send_verification,
        )
    except Exception as e:
        logger.error(
            f"Background task failed to complete account setup for {account_id}: {e}",
            exc_info=True,
        )
