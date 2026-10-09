"""Seed the harness Preloop instance. Test-only, idempotent.

Runs inside the ``preloop`` container (``docker compose exec preloop python
/harness-seed/seed.py``) and prints a JSON document with the key values
``verify.sh`` needs. It writes rows through the CRUD layer because the trusted
upstream secret and the per-subject default budget live in
``ApiKey.context_data``, which the public API key endpoint does not set.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any

from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
    crud_role,
    crud_user,
    crud_user_role,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.api_key import ApiKey

TRUSTED_SCOPE = "model_gateway:trusted_upstream"
ORG = "Claude apps gateway harness"
ADMIN = "harness-admin"
# The apps gateway rewrites bare ids to dated ids from its built-in catalog
# before relaying (observed: claude-sonnet-4-5 -> claude-sonnet-4-5-20250929),
# so Preloop serves both spellings.
MODELS = (
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
)


def _gateway_meta(model_id: str) -> dict[str, Any]:
    # Serve the model on the Preloop model gateway under its bare id, which
    # is what Claude Code (and the apps gateway) put in the request body.
    return {"gateway": {"enabled": True, "model_alias": model_id}}


def _user(db: Any, account_id: Any, username: str, email: str) -> Any:
    user = crud_user.get_by_username(db, username=username)
    if user:
        return user
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "email": email,
            "username": username,
            "full_name": username,
            "is_active": True,
            "email_verified": True,
            "hashed_password": "!harness-no-login",
            "user_source": "local",
        },
    )
    owner = crud_role.get_by_name(db, name="owner")
    if owner:
        crud_user_role.create(db, obj_in={"user_id": user.id, "role_id": owner.id})
    db.commit()
    return user


def _key(
    db: Any,
    owner: str,
    name: str,
    value: str,
    scopes: list[str],
    context: dict[str, Any] | None = None,
) -> ApiKey:
    key = crud_api_key.get_by_key(db, key=value) if value else None
    if key is None:
        key = crud_api_key.create_with_owner(
            db,
            obj_in={"name": name, "scopes": scopes},
            owner_username=owner,
            key_value=value,
        )
    key.scopes = scopes
    key.is_active = True
    if context is not None:
        key.context_data = {**(key.context_data or {}), **context}
    db.commit()
    db.refresh(key)
    return key


def set_upstream_key_active(active: bool) -> int:
    """Revoke or restore the trusted upstream key (verify.sh step 6)."""
    db = next(get_db_session())
    key = crud_api_key.get_by_key(db, key=os.environ["PRELOOP_UPSTREAM_KEY"])
    if key is None:
        print("upstream key not found", file=sys.stderr)
        return 1
    key.is_active = active
    db.commit()
    print(json.dumps({"trusted_key_id": str(key.id), "is_active": active}))
    return 0


def main() -> int:
    """Create the account, users, keys and stub-backed models."""
    if len(sys.argv) > 1 and sys.argv[1] in {"--revoke-upstream", "--restore-upstream"}:
        return set_upstream_key_active(sys.argv[1] == "--restore-upstream")
    upstream_key = os.environ["PRELOOP_UPSTREAM_KEY"]
    upstream_secret = os.environ["PRELOOP_UPSTREAM_SECRET"]
    per_subject = float(os.environ.get("HARNESS_PER_SUBJECT_BUDGET_USD", "50"))
    db = next(get_db_session())
    admin = crud_user.get_by_username(db, username=ADMIN)
    if admin is None:
        account = crud_account.create(
            db, obj_in={"organization_name": ORG, "is_active": True}
        )
        db.commit()
        admin = _user(db, account.id, ADMIN, "harness-admin@example.com")
    account_id = admin.account_id
    # alice is a Preloop member (subject links to her); bob is not.
    _user(db, account_id, "alice", "alice@example.com")

    for model_id in MODELS:
        exists = next(
            (
                m
                for m in crud_ai_model.get_by_account(db, account_id=account_id)
                if m.model_identifier == model_id
            ),
            None,
        )
        if exists is None:
            crud_ai_model.create_with_account(
                db,
                obj_in={
                    "name": f"stub {model_id}",
                    "provider_name": "anthropic",
                    "model_identifier": model_id,
                    "api_endpoint": "http://stub-model:9100",
                    "api_key": "stub-model-placeholder-key",
                    "is_default": model_id == MODELS[0],
                    "meta_data": _gateway_meta(model_id),
                },
                account_id=account_id,
            )
        else:
            exists.meta_data = {**(exists.meta_data or {}), **_gateway_meta(model_id)}
    db.commit()

    admin_key = _key(db, ADMIN, "harness admin", "pl-harness-admin-key-00000000", [])
    direct_key = _key(db, ADMIN, "harness direct", "pl-harness-direct-key-0000000", [])
    trusted = _key(
        db,
        ADMIN,
        "harness apps gateway upstream",
        upstream_key,
        [TRUSTED_SCOPE],
        {
            "trusted_upstream_secret_hash": hashlib.sha256(
                upstream_secret.encode()
            ).hexdigest(),
            "per_subject_budget": {
                "hard_limit_usd": per_subject,
                "period": "monthly",
            },
        },
    )
    json.dump(
        {
            "account_id": str(account_id),
            "admin_key": admin_key.key,
            "direct_key": direct_key.key,
            "direct_key_id": str(direct_key.id),
            "trusted_key_id": str(trusted.id),
        },
        sys.stdout,
    )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
