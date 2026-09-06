"""Email sending utility for Preloop."""

import asyncio
import html
import logging
import os
import smtplib

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from typing import Dict, Any
from preloop.config import settings

logger = logging.getLogger(__name__)

# Email configuration
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "hello@preloop.ai")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Preloop")
PRELOOP_URL = os.getenv("PRELOOP_URL", "https://preloop.ai")
APP_NAME = os.getenv("APP_NAME", "Preloop")


class EmailError(Exception):
    """Raised when email sending fails."""

    pass


def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    from_email: Optional[str] = None,
) -> None:
    """Send an email using the configured SMTP server.

    Args:
        to_email: Recipient email address.
        subject: Email subject.
        body_text: Plain text email body.
        body_html: HTML email body (optional).
        from_email: Sender email address (defaults to SMTP_FROM).

    Raises:
        EmailError: If email sending fails.
    """
    if not from_email:
        from_email = SMTP_FROM

    # Create message container
    msg = MIMEMultipart("alternative")
    # Remove protocol from URL
    portal_url = settings.preloop_url.replace("https://", "").replace("http://", "")
    msg["Subject"] = f"[{portal_url}] {subject}"
    msg["From"] = from_email
    msg["To"] = to_email

    # Create the body of the message
    part1 = MIMEText(body_text, "plain")
    msg.attach(part1)

    if body_html:
        part2 = MIMEText(body_html, "html")
        msg.attach(part2)

    # Skip sending if SMTP credentials are not configured (development mode)
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        logger.warning(
            "Email not sent: SMTP credentials not configured. "
            f"Would have sent email to {to_email} with subject '{subject}'"
        )
        # The body is NOT logged. Several senders put single-use credentials in
        # it: send_verification_email and send_password_reset_email embed an
        # auth token in a link, and send_invitation_email does the same. Logging
        # the body would write those tokens to the log in clear text, where they
        # stay valid and readable by anyone with log access. The length is
        # enough to tell an operator that a body was composed.
        logger.debug("Email body suppressed from logs (%d chars)", len(body_text))
        # Don't raise error - just return gracefully to avoid HTTP 500 in dev/CI environments
        return

    try:
        # Send the message via SMTP server
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()  # Secure the connection
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(from_email, to_email, msg.as_string())
            logger.info(f"Email sent to {to_email}: {subject}")
    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}")
        raise EmailError(f"Failed to send email: {str(e)}")


def send_verification_email(user_email: str, token: str) -> None:
    """Send a verification email to a newly registered user.

    Args:
        user_email: The user's email address.
        token: The verification token.
    """
    verification_link = f"{PRELOOP_URL}/verify-email?token={token}"

    subject = f"Verify your {APP_NAME} account"
    text_body = f"""
    Welcome to {APP_NAME}!

    Please verify your email address by clicking the link below:

    {verification_link}

    If you didn't register for {APP_NAME}, please ignore this email.

    Thank you,
    The {APP_NAME} Team
    """

    html_body = f"""
    <html>
    <body>
        <h2>Welcome to {APP_NAME}!</h2>
        <p>Please verify your email address by clicking the link below:</p>
        <p><a href="{verification_link}">Verify your email</a></p>
        <p>If you didn't register for {APP_NAME}, please ignore this email.</p>
        <p>Thank you,<br>The {APP_NAME} Team</p>
    </body>
    </html>
    """

    send_email(user_email, subject, text_body, html_body)


def send_password_reset_email(user_email: str, token: str) -> None:
    """Send a password reset email.

    Args:
        user_email: The user's email address.
        token: The password reset token.
    """
    reset_link = f"{PRELOOP_URL}/reset-password?token={token}"

    subject = f"Reset your {APP_NAME} password"
    text_body = f"""
    You have requested to reset your {APP_NAME} password.

    Please click the link below to set a new password:

    {reset_link}

    If you didn't request a password reset, please ignore this email.

    Thank you,
    The {APP_NAME} Team
    """

    html_body = f"""
    <html>
    <body>
        <h2>Reset your {APP_NAME} password</h2>
        <p>You have requested to reset your {APP_NAME} password.</p>
        <p>Please click the link below to set a new password:</p>
        <p><a href="{reset_link}">Reset your password</a></p>
        <p>If you didn't request a password reset, please ignore this email.</p>
        <p>Thank you,<br>The {APP_NAME} Team</p>
    </body>
    </html>
    """

    send_email(user_email, subject, text_body, html_body)


def send_invitation_email(
    user_email: str,
    token: str,
    organization_name: str,
    invited_by: str,
    role_names: Optional[list[str]] = None,
    team_names: Optional[list[str]] = None,
) -> None:
    """Send an invitation email to join an organization.

    Args:
        user_email: The invitee's email address.
        token: The invitation token.
        organization_name: Name of the organization they're invited to.
        invited_by: Name/email of the person who sent the invitation.
        role_names: Names of roles that will be assigned (optional).
        team_names: Names of teams the user will be added to (optional).
    """
    accept_link = f"{PRELOOP_URL}/invitations/accept?token={token}"

    subject = f"You've been invited to join {organization_name}"

    # Build role and team assignment text
    assignment_text = []
    if role_names:
        roles_str = ", ".join(role_names)
        assignment_text.append(f"Roles: {roles_str}")
    if team_names:
        teams_str = ", ".join(team_names)
        assignment_text.append(f"Teams: {teams_str}")

    assignment_section = ""
    if assignment_text:
        assignment_section = "\n\nYou will be assigned the following:\n" + "\n".join(
            f"• {line}" for line in assignment_text
        )

    text_body = f"""
Hello!

{invited_by} has invited you to join {organization_name} on {APP_NAME}.
{assignment_section}

To accept this invitation and create your account, please click the link below:

{accept_link}

This invitation will expire in 7 days.

If you didn't expect this invitation, you can safely ignore this email.

Best regards,
The {APP_NAME} Team
    """.strip()

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background-color: #4A90E2; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }}
            .content {{ background-color: #f9f9f9; padding: 30px; border-radius: 0 0 5px 5px; }}
            .button {{ display: inline-block; padding: 12px 24px; background-color: #4A90E2; color: white; text-decoration: none; border-radius: 5px; margin: 20px 0; }}
            .button:hover {{ background-color: #357ABD; }}
            .footer {{ margin-top: 20px; padding-top: 20px; border-top: 1px solid #ddd; font-size: 12px; color: #777; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>You're Invited!</h1>
            </div>
            <div class="content">
                <p>Hello!</p>
                <p><strong>{invited_by}</strong> has invited you to join <strong>{organization_name}</strong> on {APP_NAME}.</p>
                {"<div style='background-color: #f0f8ff; padding: 15px; border-left: 4px solid #4A90E2; margin: 20px 0;'><p style='margin: 0 0 10px 0;'><strong>You will be assigned:</strong></p><ul style='margin: 0;'>" + "".join(f"<li>{line}</li>" for line in assignment_text) + "</ul></div>" if assignment_text else ""}
                <p>{APP_NAME} helps teams automate and streamline their development workflows with AI-powered agents.</p>
                <p style="text-align: center;">
                    <a href="{accept_link}" class="button">Accept Invitation</a>
                </p>
                <p style="font-size: 14px; color: #666;">Or copy and paste this link into your browser:</p>
                <p style="font-size: 12px; word-break: break-all; background-color: #f0f0f0; padding: 10px; border-radius: 3px;">{accept_link}</p>
                <div class="footer">
                    <p>This invitation will expire in 7 days.</p>
                    <p>If you didn't expect this invitation, you can safely ignore this email.</p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    send_email(user_email, subject, text_body, html_body)


def send_tracker_registered_email(
    user_email: str, tracker_name: str, tracker_type: str
) -> None:
    """Send a confirmation email when a new tracker is registered.

    Args:
        user_email: The user's email address.
        tracker_name: The name of the registered tracker.
        tracker_type: The type of tracker (e.g., 'github', 'gitlab', 'jira').
    """
    trackers_link = f"{PRELOOP_URL}/console/trackers"

    subject = f"New {tracker_type.title()} tracker registered: {tracker_name}"
    text_body = f"""
    You have successfully registered a new tracker in {APP_NAME}:

    Name: {tracker_name}
    Type: {tracker_type.title()}

    You can view and manage your trackers at:
    {trackers_link}

    Thank you,
    The {APP_NAME} Team
    """

    html_body = f"""
    <html>
    <body>
        <h2>New tracker registered</h2>
        <p>You have successfully registered a new tracker in {APP_NAME}:</p>
        <p>
            <strong>Name:</strong> {tracker_name}<br>
            <strong>Type:</strong> {tracker_type.title()}
        </p>
        <p>You can <a href="{trackers_link}">view and manage your trackers here</a>.</p>
        <p>Thank you,<br>The {APP_NAME} Team</p>
    </body>
    </html>
    """

    send_email(user_email, subject, text_body, html_body)


async def send_product_notification_email(
    user_data: Dict[str, Any],
    tracker_data: Optional[Dict[str, Any]],
    source_ip: str,
) -> None:
    """Send a product notification email with user, tracker, and IP information.

    Args:
        user_data: User account information.
        tracker_data: Tracker information (optional).
        source_ip: Source IP address.

    Raises:
        EmailError: If email sending fails.
    """
    recipient_email = settings.product_team_email
    if not recipient_email:
        logger.warning(
            "Product notification email not sent: PRODUCT_TEAM_EMAIL not configured"
        )
        return
    subject = "Product Notification: User Activity"

    # Filter sensitive fields
    sensitive_fields = {"password", "token", "secret", "api_key"}

    def filter_data(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not data:
            return {}
        return {k: v for k, v in data.items() if k.lower() not in sensitive_fields}

    filtered_user_data = filter_data(user_data)
    filtered_tracker_data = filter_data(tracker_data)

    body_parts = [
        "A product notification event has occurred.",
        "\nUser Data:",
    ]
    for key, value in filtered_user_data.items():
        body_parts.append(f"  {key}: {value}")

    if filtered_tracker_data:
        body_parts.append("\nTracker Data:")
        for key, value in filtered_tracker_data.items():
            body_parts.append(f"  {key}: {value}")

    body_parts.append(f"\nSource IP: {source_ip}")

    body_text = "\n".join(body_parts)

    # HTML body (optional, can be enhanced)
    html_body_parts = [
        "<html><body>",
        "<h2>Product Notification: User Activity</h2>",
        "<p>A product notification event has occurred.</p>",
        "<h3>User Data:</h3><ul>",
    ]
    for key, value in filtered_user_data.items():
        html_body_parts.append(f"<li><strong>{key}:</strong> {value}</li>")
    html_body_parts.append("</ul>")

    if filtered_tracker_data:
        html_body_parts.append("<h3>Tracker Data:</h3><ul>")
        for key, value in filtered_tracker_data.items():
            html_body_parts.append(f"<li><strong>{key}:</strong> {value}</li>")
        html_body_parts.append("</ul>")

    html_body_parts.append(f"<p><strong>Source IP:</strong> {source_ip}</p>")
    html_body_parts.append("</body></html>")
    body_html = "\n".join(html_body_parts)

    try:
        # Run send_email in a separate thread to avoid blocking asyncio event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, send_email, recipient_email, subject, body_text, body_html
        )
        logger.info(f"Product notification email sent to {recipient_email}")
    except EmailError as e:
        # Log error but don't raise to avoid breaking the flow in dev/CI environments
        logger.warning(f"Failed to send product notification email: {str(e)}")
    except Exception as e:
        logger.error(
            f"An unexpected error occurred while sending product notification email: {str(e)}"
        )
        # Log unexpected errors but don't raise


async def send_approval_request_email(
    user_email: str,
    tool_name: str,
    tool_args: Dict[str, Any],
    approval_url: str,
    agent_reasoning: Optional[str] = None,
    summary: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> None:
    """Send an approval request email to an approver.

    Args:
        user_email: The approver's email address.
        tool_name: The name of the tool requiring approval.
        tool_args: The arguments passed to the tool.
        approval_url: The URL to approve or decline the request.
        agent_reasoning: Optional reasoning from the agent for why it wants to use the tool.
        summary: Optional plain-language ask shown first in the email.
        agent_name: The agent that asked, when known. An approver reading an
            inbox decides on the caller as much as on the tool, so the name
            goes in the subject and replaces the generic "An AI agent" line.

    Raises:
        EmailError: If email sending fails.
    """
    ask_text = (summary or "").strip() or None
    asker = (agent_name or "").strip() or None
    if ask_text:
        subject = ask_text[:80]
    elif asker:
        subject = f"{asker} needs approval: {tool_name}"
    else:
        subject = f"Tool Approval Required: {tool_name}"

    # Format tool arguments for display (redact sensitive fields)
    import json

    from preloop.utils.redaction import redact_dict

    tool_args = redact_dict(tool_args)
    tool_args_formatted = json.dumps(tool_args, indent=2)

    # Plain text version
    text_parts = [
        "Hi,",
        "",
    ]
    if ask_text:
        text_parts.extend(
            [
                ask_text,
                "",
                f"Tool: {tool_name}",
            ]
        )
    else:
        text_parts.extend(
            [
                (
                    f"{asker} is requesting approval to execute the following tool:"
                    if asker
                    else "An AI agent is requesting approval to execute the following tool:"
                ),
                "",
                f"Tool: {tool_name}",
            ]
        )

    if asker:
        text_parts.append(f"Agent: {asker}")

    if agent_reasoning:
        text_parts.append("")
        text_parts.append("Agent's Reasoning:")
        text_parts.append(agent_reasoning)

    text_parts.extend(
        [
            "",
            "Arguments:",
            tool_args_formatted,
            "",
            "To approve or decline this request, please visit:",
            approval_url,
            "",
            "This link will expire in 5 minutes.",
            "",
            "Best regards,",
            f"{APP_NAME} Team",
        ]
    )

    body_text = "\n".join(text_parts)

    # HTML version. Every value below is caller-supplied text (an agent's
    # display name, a tool name, the model's own words, the arguments it
    # chose) landing in someone's email client, so it is escaped as text.
    # quote=False: these are text nodes, not attribute values, and escaping
    # quotes would only make the JSON block harder to read.
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=False)

    header_title = "Approval Required" if ask_text else "Tool Approval Required"
    html_parts = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        '  <meta charset="UTF-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        "  <style>",
        "    body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }",
        "    .container { max-width: 600px; margin: 0 auto; padding: 20px; }",
        "    .header { background-color: #4CAF50; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }",
        "    .content { background-color: #f9f9f9; padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px; }",
        "    .tool-name { font-weight: bold; color: #2196F3; font-size: 1.1em; }",
        "    .summary { background-color: #e8f5e9; padding: 15px; border-left: 4px solid #4CAF50; margin: 15px 0; font-size: 1.05em; }",
        "    .reasoning { background-color: #fff3cd; padding: 15px; border-left: 4px solid #ffc107; margin: 15px 0; }",
        "    .arguments { background-color: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; overflow-x: auto; font-family: 'Courier New', monospace; font-size: 13px; }",
        "    .button { display: inline-block; background-color: #4CAF50; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; margin: 10px 5px; }",
        "    .button-decline { background-color: #f44336; }",
        "    .footer { text-align: center; margin-top: 20px; font-size: 0.9em; color: #777; }",
        "    .warning { color: #ff6b6b; font-weight: bold; }",
        "  </style>",
        "</head>",
        "<body>",
        '  <div class="container">',
        '    <div class="header">',
        f"      <h2>{header_title}</h2>",
        "    </div>",
        '    <div class="content">',
        "      <p>Hi,</p>",
    ]

    if ask_text:
        html_parts.extend(
            [
                '      <div class="summary">',
                f"        <p><strong>{esc(ask_text)}</strong></p>",
                "      </div>",
                f'      <p class="tool-name">Tool: {esc(tool_name)}</p>',
            ]
        )
    else:
        lead = (
            f"{asker} is requesting approval to execute the following tool:"
            if asker
            else "An AI agent is requesting approval to execute the following tool:"
        )
        html_parts.extend(
            [
                f"      <p>{esc(lead)}</p>",
                f'      <p class="tool-name">Tool: {esc(tool_name)}</p>',
            ]
        )

    if asker:
        html_parts.append(f"      <p>Agent: {esc(asker)}</p>")

    if agent_reasoning:
        html_parts.extend(
            [
                '      <div class="reasoning">',
                "        <strong>Agent's Reasoning:</strong>",
                f"        <p>{esc(agent_reasoning)}</p>",
                "      </div>",
            ]
        )

    html_parts.extend(
        [
            "      <p><strong>Arguments:</strong></p>",
            f'      <pre class="arguments">{esc(tool_args_formatted)}</pre>',
            "      <p>Please review and decide:</p>",
            '      <div style="text-align: center; margin: 20px 0;">',
            f'        <a href="{approval_url}" class="button">Review Request</a>',
            "      </div>",
            '      <p class="warning">⚠️ This link will expire in 5 minutes.</p>',
            "    </div>",
            '    <div class="footer">',
            f"      <p>Sent by {APP_NAME}</p>",
            "    </div>",
            "  </div>",
            "</body>",
            "</html>",
        ]
    )

    body_html = "\n".join(html_parts)

    try:
        # Run send_email in a separate thread to avoid blocking asyncio event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, send_email, user_email, subject, body_text, body_html
        )
        logger.info(
            f"Approval request email sent to {user_email} for tool '{tool_name}'"
        )
    except EmailError as e:
        # Log error but don't raise to avoid breaking the flow in dev/CI environments
        logger.warning(f"Failed to send approval request email: {str(e)}")
    except Exception as e:
        logger.error(
            f"An unexpected error occurred while sending approval request email: {str(e)}"
        )
        # Log unexpected errors but don't raise


def send_escalation_email(
    user_email: str,
    tool_name: str,
    request_id: str,
    approval_token: str,
    base_url: str,
) -> None:
    """Send an escalation email when original approvers don't respond.

    Args:
        user_email: The escalation recipient's email address.
        tool_name: The name of the tool requiring approval.
        request_id: The approval request ID.
        approval_token: The secure token for the approval link.
        base_url: The base URL for generating approval links.

    Raises:
        EmailError: If email sending fails.
    """
    subject = f"🚨 ESCALATED: Approval Required for {tool_name}"

    # The approval page lives at /console/approval/<id> (SPA route). The
    # token is preserved so the page can fall back to the public token read
    # for recipients who are not signed in (issue #335).
    approval_url = f"{base_url}/console/approval/{request_id}?token={approval_token}"

    # Plain text version
    body_text = f"""Hi,

🚨 ESCALATION NOTICE

An approval request has been escalated to you because the original approvers did not respond in time.

Tool: {tool_name}
Request ID: {request_id}

To approve or decline this request, please visit:
{approval_url}

Please respond promptly as this request is time-sensitive.

Best regards,
{APP_NAME} Team
"""

    # HTML version
    body_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
    .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
    .header {{ background-color: #ff6b6b; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }}
    .content {{ background-color: #f9f9f9; padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px; }}
    .escalation-notice {{ background-color: #fff3cd; padding: 15px; border-left: 4px solid #ff6b6b; margin: 15px 0; font-weight: bold; }}
    .tool-name {{ font-weight: bold; color: #2196F3; font-size: 1.1em; }}
    .button {{ display: inline-block; background-color: #4CAF50; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; margin: 10px 5px; }}
    .footer {{ text-align: center; margin-top: 20px; font-size: 0.9em; color: #777; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🚨 Escalation Notice</h1>
    </div>
    <div class="content">
      <div class="escalation-notice">
        This approval request has been escalated to you because the original approvers did not respond in time.
      </div>

      <p>An AI agent is requesting approval to execute:</p>
      <p class="tool-name">{tool_name}</p>

      <p><strong>Request ID:</strong> {request_id}</p>

      <div style="text-align: center; margin: 25px 0;">
        <a href="{approval_url}" class="button">Review & Respond</a>
      </div>

      <p style="color: #ff6b6b;"><strong>Please respond promptly as this request is time-sensitive.</strong></p>
    </div>
    <div class="footer">
      <p>This is an automated escalation from {APP_NAME}</p>
    </div>
  </div>
</body>
</html>
"""

    try:
        send_email(user_email, subject, body_text, body_html)
        logger.info(f"Escalation email sent to {user_email} for tool '{tool_name}'")
    except EmailError as e:
        logger.warning(f"Failed to send escalation email: {str(e)}")
    except Exception as e:
        logger.error(
            f"An unexpected error occurred while sending escalation email: {str(e)}"
        )
