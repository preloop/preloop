"""Mint a runtime API key for the codex+MCP canary integration test.

Run as a subprocess AFTER the backend has booted with ``INIT_TEST_DATA=true``
(the seeded admin principal must exist). Prints exactly one line to stdout:
the bearer token. The token authenticates BOTH surfaces the canary exercises:

* the model gateway (``/openai/v1/responses`` via ``authenticate_bearer_token``)
* the MCP server (``/mcp/v1`` via ``PreloopBearerAuthBackend``)

Mirrors ``tests/e2e_support/seed_gateway_model.py`` conventions: standalone,
idempotent to re-run (each run mints a fresh key), resolves the admin via
``PRELOOP_E2E_USERNAME`` / ``PRELOOP_E2E_ADMIN_EMAIL``.

    python -m tests.e2e_support.mint_canary_credentials
"""

from __future__ import annotations

import logging
import os
import sys

from preloop.models.crud import crud_api_key
from preloop.models.crud.user import CRUDUser
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger("mint_canary_credentials")

ADMIN_EMAIL = os.getenv("PRELOOP_E2E_ADMIN_EMAIL", "admin@preloop.ai")
ADMIN_USERNAME = os.getenv("PRELOOP_E2E_USERNAME", "admin")


def mint() -> str:
    """Mint a runtime API key for the seeded admin; return the bearer token."""
    session_generator = get_db_session()
    db = next(session_generator)
    try:
        crud_user = CRUDUser(User)
        user = crud_user.get_by_username(db, username=ADMIN_USERNAME) or (
            crud_user.get_by_email(db, email=ADMIN_EMAIL)
        )
        if user is None or not getattr(user, "account_id", None):
            raise RuntimeError(
                f"No admin principal found (username={ADMIN_USERNAME!r}, "
                f"email={ADMIN_EMAIL!r}). Start the backend with "
                "INIT_TEST_DATA=true first."
            )
        _, token = crud_api_key.create_runtime_key(
            db,
            name="Codex MCP Canary Token",
            account_id=user.account_id,
            user_id=user.id,
            context_data={},
        )
        return token
    finally:
        db.close()
        try:
            next(session_generator, None)
        except StopIteration:
            pass


def main() -> None:
    try:
        token = mint()
    except Exception as exc:  # noqa: BLE001 - surface a clean CI error
        logger.error("Failed to mint canary credentials: %s", exc)
        sys.exit(1)
    print(token)


if __name__ == "__main__":
    main()
