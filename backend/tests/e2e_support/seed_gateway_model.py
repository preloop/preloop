"""Seed a gateway-enabled ``AIModel`` for the T10 E2E harness.

The OpenAI gateway resolves which ``AIModel`` to use for an incoming
``/openai/v1/chat/completions`` request by matching the request's ``model``
field against each model's ``meta_data.gateway.model_alias`` (see
``OpenAIGatewayService._resolve_requested_model`` and
``resolve_ai_model_runtime``). The *upstream* call litellm makes is built from
``AIModel.api_endpoint`` (used as litellm ``api_base``) plus
``provider_name`` / ``model_identifier``.

This script seeds ONE gateway-enabled model under the ``INIT_TEST_DATA`` admin
account:

* ``meta_data.gateway.enabled = True`` and ``model_alias = "preloop-fake"`` so a
  client request with ``"model": "preloop-fake"`` resolves to this row.
* ``provider_name = "openai"`` so litellm speaks the OpenAI chat-completions
  wire format.
* ``api_endpoint`` = the fake upstream base URL (``$PRELOOP_E2E_FAKE_UPSTREAM``,
  default ``http://127.0.0.1:8081/v1``) so litellm POSTs to the fake server
  instead of a real provider. The ``api_key`` is a throwaway value (the fake
  upstream ignores auth); it only needs to be present so the gateway's
  "credentials configured" guard passes.

Run it standalone after the backend DB is migrated and INIT_TEST_DATA has run:

    source .venv/bin/activate  # or ../.venv/bin/activate
    PRELOOP_E2E_FAKE_UPSTREAM=http://127.0.0.1:8081/v1 \
        python -m tests.e2e_support.seed_gateway_model

It is idempotent: re-running updates the existing seeded model's endpoint
rather than creating duplicates. It prints the resolved model alias on success.
"""

from __future__ import annotations

import logging
import os
import sys

from preloop.models.crud.account import CRUDAccount
from preloop.models.crud.ai_model import ai_model as crud_ai_model
from preloop.models.crud.user import CRUDUser
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed_gateway_model")

# The client-facing alias the E2E sends as the request ``model`` field. It must
# match ``preloop-fake`` in the Playwright spec.
GATEWAY_MODEL_ALIAS = "preloop-fake"
ADMIN_EMAIL = os.getenv("PRELOOP_E2E_ADMIN_EMAIL", "admin@preloop.ai")
ADMIN_USERNAME = os.getenv("PRELOOP_E2E_USERNAME", "admin")


def _resolve_account_id(db) -> str:
    """Resolve the account id the logged-in admin uses for gateway resolution.

    The gateway resolves the request's ``AIModel`` via the authenticated user's
    ``account_id`` (``get_account_for_user`` -> ``current_user.account_id``).
    The credential-mint endpoint anchors the agent to the same account. So the
    seeded model must live under the SAME account the admin login resolves to.

    Prefer the ``User`` row's ``account_id`` (matches the login principal); fall
    back to the ``Account`` looked up by email for INIT_TEST_DATA setups where
    the admin is provisioned as an Account.

    Raises:
        RuntimeError: If no admin principal can be found.
    """
    crud_user = CRUDUser(User)
    user = crud_user.get_by_username(db, username=ADMIN_USERNAME) or (
        crud_user.get_by_email(db, email=ADMIN_EMAIL)
    )
    if user is not None and getattr(user, "account_id", None):
        return str(user.account_id)

    crud_account = CRUDAccount(Account)
    account = crud_account.get_by_email(db, email=ADMIN_EMAIL)
    if account is not None:
        return str(account.id)

    raise RuntimeError(
        f"No admin principal found (username={ADMIN_USERNAME!r}, "
        f"email={ADMIN_EMAIL!r}). Start the backend with INIT_TEST_DATA=true "
        "first, or set PRELOOP_E2E_USERNAME/PRELOOP_E2E_ADMIN_EMAIL."
    )


def seed() -> str:
    """Create or update the gateway-enabled fake AIModel.

    Returns:
        The model alias clients send as the request ``model`` field.

    Raises:
        RuntimeError: If the admin account cannot be found.
    """
    fake_upstream = os.getenv("PRELOOP_E2E_FAKE_UPSTREAM", "http://127.0.0.1:8081/v1")

    session_generator = get_db_session()
    db = next(session_generator)
    try:
        account_id = _resolve_account_id(db)

        gateway_meta = {
            "gateway": {
                "enabled": True,
                "model_alias": GATEWAY_MODEL_ALIAS,
                "provider_adapter": "preloop",
            }
        }

        # Idempotency: reuse an existing seeded model with our alias.
        existing = None
        for model in crud_ai_model.get_by_account(db, account_id=account_id):
            meta = model.meta_data or {}
            if (meta.get("gateway") or {}).get("model_alias") == GATEWAY_MODEL_ALIAS:
                existing = model
                break

        if existing is not None:
            crud_ai_model.update(
                db,
                db_obj=existing,
                obj_in={
                    "api_endpoint": fake_upstream,
                    "api_key": "e2e-fake-upstream-key",
                    "is_default": True,
                    "meta_data": gateway_meta,
                },
            )
            logger.info(
                "Updated gateway model %s (alias=%s, endpoint=%s)",
                existing.id,
                GATEWAY_MODEL_ALIAS,
                fake_upstream,
            )
        else:
            created = crud_ai_model.create_with_account(
                db,
                obj_in={
                    "name": "E2E Fake Gateway Model",
                    "provider_name": "openai",
                    "model_identifier": "gpt-4o-mini",
                    "api_endpoint": fake_upstream,
                    "api_key": "e2e-fake-upstream-key",
                    "is_default": True,
                    "meta_data": gateway_meta,
                },
                account_id=account_id,
            )
            logger.info(
                "Created gateway model %s (alias=%s, endpoint=%s)",
                created.id,
                GATEWAY_MODEL_ALIAS,
                fake_upstream,
            )

        return GATEWAY_MODEL_ALIAS
    finally:
        db.close()
        try:
            next(session_generator, None)
        except StopIteration:
            pass


def main() -> None:
    try:
        alias = seed()
    except Exception as exc:  # noqa: BLE001 - surface a clean CI error
        logger.error("Failed to seed gateway model: %s", exc)
        sys.exit(1)
    print(alias)


if __name__ == "__main__":
    main()
