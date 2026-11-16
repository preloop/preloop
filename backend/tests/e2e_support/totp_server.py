"""Local browser-test server with a file-controlled MFA clock and mail outbox.

Only proof time and email delivery are replaced. JWT expiry, HTTP authentication,
PostgreSQL transactions, hashing, replay protection and auditing remain real.
No test control routes are added to the application.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"


def test_directory() -> Path:
    """Return the explicitly configured, private harness directory."""
    return Path(os.environ["PRELOOP_E2E_DIR"]).resolve()


def proof_clock() -> float:
    """Read proof time without a wall-clock fallback or mutable HTTP endpoint."""
    return float((test_directory() / "clock").read_text())


def capture_email(
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    **_kwargs: Any,
) -> bool:
    """Capture local email privately instead of contacting an SMTP service."""
    outbox = test_directory() / "outbox"
    outbox.mkdir(mode=0o700, exist_ok=True)
    target = outbox / f"{uuid.uuid4().hex}.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"to": to_email, "subject": subject, "text": body_text}, stream)
    return True


def require_local_database() -> None:
    """Refuse fixture writes outside the explicitly configured local stack."""
    if os.environ.get("TESTING") != "true":
        raise RuntimeError("The browser harness requires TESTING=true")
    host = urlparse(os.environ["DATABASE_URL"]).hostname
    if host not in {"localhost", "127.0.0.1", "::1", "postgres"}:
        raise RuntimeError("The browser harness requires a local test database")


def seed_approval(email: str) -> dict[str, str]:
    """Create an inert approval fixture through the repository's CRUD layer."""
    from preloop.models import schemas
    from preloop.models.crud import (
        crud_approval_request,
        crud_approval_workflow,
        crud_tool_configuration,
        crud_user,
    )
    from preloop.models.db.session import get_db_session

    require_local_database()
    sessions = get_db_session()
    db = next(sessions)
    try:
        user = crud_user.list_by_email(db, email=email)[0]
        policy = crud_approval_workflow.create(
            db,
            account_id=str(user.account_id),
            obj_in={
                "name": "Synthetic browser approval",
                "approval_type": "manual",
                "timeout_seconds": 3600,
            },
        )
        tool = crud_tool_configuration.create(
            db,
            config_in=schemas.ToolConfigurationCreate.model_validate(
                {
                    "tool_name": "synthetic_browser_approval",
                    "account_id": str(user.account_id),
                    "approval_workflow_id": policy.id,
                }
            ),
        )
        approval = crud_approval_request.create(
            db,
            obj_in={
                "account_id": user.account_id,
                "tool_configuration_id": tool.id,
                "approval_workflow_id": policy.id,
                "tool_name": tool.tool_name,
                "tool_args": {},
                "status": "pending",
            },
        )
        return {"id": str(approval.id), "token": approval.approval_token}
    finally:
        sessions.close()


def email_proof(email: str) -> str:
    """Mint a genuine email-link proof for the enrolled-user gate check."""
    from preloop.models.crud import crud_person, crud_user
    from preloop.models.db.session import get_db_session
    from preloop.utils.tokens import create_email_verification_token

    require_local_database()
    sessions = get_db_session()
    db = next(sessions)
    try:
        user = crud_user.list_by_email(db, email=email)[0]
        person = crud_person.resolve_for_membership(db, membership_id=user.id)
        if person.email is None:
            raise RuntimeError("Expected a local email identity")
        return create_email_verification_token(
            person.email,
            person_id=person.id,
            auth_generation=person.auth_generation,
        )
    finally:
        sessions.close()


def main() -> None:
    """Serve the app or perform a private driver operation over stdin/stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve", "totp", "approval", "email"])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "totp":
        import pyotp

        payload = json.load(sys.stdin)
        generator = pyotp.TOTP(payload["secret"])
        now = float(payload["time"])
        if payload.get("invalid"):
            accepted = {generator.at(now + offset) for offset in (-30, 0, 30)}
            print(next(f"{n:06d}" for n in range(4) if f"{n:06d}" not in accepted))
        else:
            print(generator.at(now))
        return
    require_local_database()
    if args.command in {"approval", "email"}:
        payload = json.load(sys.stdin)
        result = (
            seed_approval(payload["email"])
            if args.command == "approval"
            else email_proof(payload["email"])
        )
        print(json.dumps(result))
        return

    # The normal lifespan starts the OAuth provider used by the real CLI.
    # Keep TESTING=true in the driver/fixture processes, but run the server's
    # ordinary local-stack startup, including its real NATS connection.
    os.environ["TESTING"] = "false"

    import uvicorn

    from preloop.api.app import create_app

    # These patches exist only in this standalone test process. Neither the
    # production entry point nor the application's route inventory changes.
    with (
        patch("preloop.services.mfa.clock", proof_clock),
        patch("preloop.services.rate_limit._clock", proof_clock),
        patch("preloop.utils.email.send_email", capture_email),
    ):
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
