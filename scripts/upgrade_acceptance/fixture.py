"""Synthetic upgrade corpus and local-only probes, run inside either app image.

All application data goes through the release's own models and CRUD. The base
CRUD deliberately writes legacy columns on the old release to exercise the
credential backfill, rather than silently seeding only already-migrated rows.
Never run this against an existing installation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

PASSWORD = "synthetic-upgrade-password-42!"
USER_KEY = "synthetic-upgrade-user-token-do-not-use-42"
AGENT_KEY = "synthetic-upgrade-agent-token-do-not-use-42"
REVOKED_KEY = "synthetic-upgrade-revoked-token-do-not-use-42"
MODEL_KEY = "synthetic-upgrade-model-token-do-not-use-42"
TRACKER_KEY = "synthetic-upgrade-tracker-token-do-not-use-42"
WEBHOOK_KEY = "synthetic-upgrade-webhook-token-do-not-use-42"
STAMP = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


class FixtureError(RuntimeError):
    """A fixed, safe assertion message that contains no credential values."""


def identifier(name: str) -> UUID:
    """Give every synthetic row a stable, independent identity."""
    return uuid5(NAMESPACE_URL, f"https://example.com/upgrade-acceptance/{name}")


def require(condition: Any, message: str) -> None:
    """Fail without interpolating secret-bearing values into an exception."""
    if not condition:
        raise FixtureError(message)


def seed() -> None:
    """Create a small multi-tenant corpus through the old release's CRUD."""
    from preloop.api.auth.jwt import get_password_hash
    from preloop.models import crud, models
    from preloop.models.crud.base import CRUDBase
    from preloop.models.db.session import get_db_session

    require(hasattr(models.AIModel, "api_key"), "Baseline lacks legacy AI field")
    require(hasattr(models.ApiKey, "key"), "Baseline lacks legacy API key field")
    with contextmanager(get_db_session)() as db:
        for tenant in ("alpha", "beta"):
            account = crud.crud_account.create(
                db,
                obj_in={
                    "id": identifier(f"{tenant}/account"),
                    "organization_name": f"Example {tenant.title()}",
                    "is_active": True,
                    "meta_data": {"upgrade_fixture": 1, "region": "example"},
                },
            )
            user = crud.crud_user.create(
                db,
                obj_in={
                    "id": identifier(f"{tenant}/user"),
                    "account_id": account.id,
                    "username": f"upgrade-{tenant}",
                    "email": f"upgrade-{tenant}@example.com",
                    "full_name": "Jane Doe",
                    "hashed_password": get_password_hash(PASSWORD),
                    "is_active": True,
                    "email_verified": True,
                    "user_source": "local",
                },
            )
            role = crud.crud_role.get_by_name(db, name="owner")
            require(role is not None, "Initializer did not seed owner role")
            crud.crud_user_role.create(
                db, obj_in={"user_id": user.id, "role_id": role.id}
            )
            crud.crud_account.update(
                db, db_obj=account, obj_in={"primary_user_id": user.id}
            )
            tracker = crud.crud_tracker.create(
                db,
                obj_in={
                    "id": identifier(f"{tenant}/tracker"),
                    "account_id": account.id,
                    "name": "Example tracker",
                    "tracker_type": "jira",
                    "url": "https://example.com/issues",
                    "api_key": TRACKER_KEY,
                    "is_active": False,
                },
            )
            # The normal writer encrypts new webhooks. Old installations can
            # still have this legacy column: exercise that separate backfill.
            CRUDBase(models.Tracker).update(
                db, db_obj=tracker, obj_in={"jira_webhook_secret": WEBHOOK_KEY}
            )
            organization = crud.crud_organization.create(
                db,
                obj_in={
                    "id": identifier(f"{tenant}/organization"),
                    "tracker_id": tracker.id,
                    "name": "Example organization",
                    "identifier": f"example-{tenant}",
                },
            )
            project = crud.crud_project.create(
                db,
                obj_in={
                    "id": identifier(f"{tenant}/project"),
                    "organization_id": organization.id,
                    "name": "Example project",
                    "identifier": f"example-{tenant}",
                    "settings": {"labels": ["upgrade", "synthetic"]},
                },
            )
            for index, status in enumerate(("open", "in_progress", "closed")):
                crud.crud_issue.create(
                    db,
                    obj_in={
                        "id": identifier(f"{tenant}/issue/{index}"),
                        "project_id": project.id,
                        "tracker_id": tracker.id,
                        "external_id": str(100 + index),
                        "key": f"EXAMPLE-{100 + index}",
                        "title": f"Synthetic {status} issue",
                        "description": "Preserve Unicode: café.\nSecond paragraph.",
                        "status": status,
                        "priority": "high",
                        "meta_data": {"labels": ["upgrade"], "estimate": index},
                    },
                )
            for mode in ("legacy", "encrypted"):
                writer = (
                    CRUDBase(models.AIModel) if mode == "legacy" else crud.crud_ai_model
                )
                writer.create(
                    db,
                    obj_in={
                        "id": identifier(f"{tenant}/model/{mode}"),
                        "account_id": account.id,
                        "name": f"Synthetic {mode} model",
                        "provider_name": "openai",
                        "model_identifier": "synthetic-model",
                        "api_endpoint": "http://127.0.0.1:9/v1",
                        "api_key": MODEL_KEY,
                        "is_default": False,
                        "meta_data": {"gateway": {"enabled": False}},
                    },
                )
            for state in ("active", "suspended"):
                crud.crud_managed_agent.create(
                    db,
                    obj_in={
                        "id": identifier(f"{tenant}/agent/{state}"),
                        "account_id": account.id,
                        "owner_user_id": user.id,
                        "agent_kind": "claude-code",
                        "session_source_type": "claude-code",
                        "session_source_id": f"example-{tenant}-{state}",
                        "display_name": f"Example {state} agent",
                        "tags": {"purpose": "upgrade-acceptance"},
                        "lifecycle_state": state,
                        "lifecycle_updated_at": STAMP,
                        "last_seen_at": STAMP,
                    },
                )
            if tenant == "alpha":
                for name, value, active in (
                    ("user", USER_KEY, True),
                    ("revoked", REVOKED_KEY, False),
                    ("agent", AGENT_KEY, True),
                ):
                    key = CRUDBase(models.ApiKey).create(
                        db,
                        obj_in={
                            "id": identifier(f"alpha/key/{name}"),
                            "account_id": account.id,
                            "user_id": user.id,
                            "name": f"Synthetic {name} key",
                            "key": value,
                            "scopes": [],
                            "is_active": active,
                        },
                    )
                    if name == "agent":
                        crud.crud_managed_agent_credential.create_for_agent(
                            db,
                            account_id=account.id,
                            agent_id=identifier("alpha/agent/active"),
                            api_key_id=key.id,
                            created_by_user_id=user.id,
                            name="Synthetic durable credential",
                            issued_at=STAMP,
                        )


def snapshot() -> dict[str, Any]:
    """Check credential resolution and fingerprint stable data/relationships."""
    from preloop.models import crud, models
    from preloop.models.crud.base import CRUDBase
    from preloop.models.db.session import get_db_session
    from preloop.services.secret_service import get_secret_service

    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    def record(model: Any, name: str, fields: tuple[str, ...]) -> Any:
        row = CRUDBase(model).get(db, id=identifier(name))
        require(row is not None, f"Missing fixture row: {name}")
        rows.append({"fixture": name, **{f: getattr(row, f) for f in fields}})
        counts[model.__name__] = counts.get(model.__name__, 0) + 1
        return row

    encrypted_models = 0
    encrypted_webhooks = 0
    with contextmanager(get_db_session)() as db:
        for tenant in ("alpha", "beta"):
            record(
                models.Account,
                f"{tenant}/account",
                ("id", "organization_name", "primary_user_id", "meta_data"),
            )
            record(
                models.User,
                f"{tenant}/user",
                ("id", "account_id", "username", "email", "is_active"),
            )
            tracker = record(
                models.Tracker,
                f"{tenant}/tracker",
                ("id", "account_id", "name", "url", "is_active"),
            )
            require(tracker.resolved_api_key == TRACKER_KEY, "Tracker token changed")
            require(
                tracker.resolved_webhook_secret == WEBHOOK_KEY,
                "Webhook credential changed",
            )
            encrypted_webhooks += bool(tracker.webhook_secret_id)
            record(
                models.Organization,
                f"{tenant}/organization",
                ("id", "tracker_id", "identifier"),
            )
            record(
                models.Project,
                f"{tenant}/project",
                ("id", "organization_id", "identifier", "settings"),
            )
            for index in range(3):
                record(
                    models.Issue,
                    f"{tenant}/issue/{index}",
                    (
                        "id",
                        "project_id",
                        "tracker_id",
                        "external_id",
                        "key",
                        "title",
                        "description",
                        "status",
                        "priority",
                        "meta_data",
                    ),
                )
            for mode in ("legacy", "encrypted"):
                model = record(
                    models.AIModel,
                    f"{tenant}/model/{mode}",
                    ("id", "account_id", "name", "model_identifier", "meta_data"),
                )
                resolved = get_secret_service().resolve_ai_model_api_key(model)
                require(resolved and resolved.value == MODEL_KEY, "Model token changed")
                encrypted_models += bool(model.credentials_secret_id)
            for state in ("active", "suspended"):
                record(
                    models.ManagedAgent,
                    f"{tenant}/agent/{state}",
                    (
                        "id",
                        "account_id",
                        "owner_user_id",
                        "display_name",
                        "session_source_id",
                        "lifecycle_state",
                        "tags",
                    ),
                )
        for name in ("user", "revoked", "agent"):
            record(
                models.ApiKey,
                f"alpha/key/{name}",
                ("id", "account_id", "user_id", "name", "scopes", "is_active"),
            )
        credentials = crud.crud_managed_agent_credential.list_for_agent(
            db,
            account_id=identifier("alpha/account"),
            agent_id=identifier("alpha/agent/active"),
        )
        require(len(credentials) == 1, "Agent credential inventory changed")
        credential = credentials[0]
        require(
            credential["api_key_id"] == str(identifier("alpha/key/agent"))
            and credential["status"] == "active",
            "Agent credential binding changed",
        )
        rows.append({"fixture": "agent-credential", "id": credential["id"]})
        counts["ManagedAgentCredential"] = 1
        migrated = "key" not in models.ApiKey.__table__.columns
        if migrated:
            require(encrypted_models == 4, "Model backfill incomplete")
            require(encrypted_webhooks == 2, "Webhook backfill incomplete")
            for name, value in (("user", USER_KEY), ("agent", AGENT_KEY)):
                key = crud.crud_api_key.get(db, id=identifier(f"alpha/key/{name}"))
                require(
                    key.key_hash == hashlib.sha256(value.encode()).hexdigest(),
                    "Legacy key hash backfill incomplete",
                )
    digest = hashlib.sha256(
        json.dumps(rows, default=str, sort_keys=True).encode()
    ).hexdigest()
    return {"digest": digest, "counts": counts, "credential_backfill": migrated}


def check(previous: dict[str, Any]) -> dict[str, Any]:
    """Verify fresh login, existing tokens, revoked denial and schema head."""
    import httpx
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    state = snapshot()
    if previous:
        require(state["digest"] == previous["digest"], "Fixture data changed")
        require(state["counts"] == previous["counts"], "Fixture inventory changed")
    with httpx.Client(
        base_url="http://127.0.0.1:8000", timeout=20, trust_env=False
    ) as client:
        for tenant in ("alpha", "beta"):
            login = client.post(
                "/api/v1/auth/token/json",
                json={"username": f"upgrade-{tenant}", "password": PASSWORD},
            )
            require(login.status_code == 200, f"{tenant} password login failed")
            token = login.json()["access_token"]
            profile = client.get(
                "/api/v1/auth/users/me", headers={"Authorization": f"Bearer {token}"}
            )
            require(profile.status_code == 200, "Password session rejected")
            require(
                profile.json()["id"] == str(identifier(f"{tenant}/user")),
                "Login resolved wrong identity",
            )
            if tenant == "alpha":
                state["access_token"] = token
        for name, token in (
            ("user", USER_KEY),
            ("agent", AGENT_KEY),
            ("prior-session", previous.get("access_token", state["access_token"])),
        ):
            profile = client.get(
                "/api/v1/auth/users/me", headers={"Authorization": f"Bearer {token}"}
            )
            require(profile.status_code == 200, f"Existing {name} token rejected")
            require(
                profile.json()["id"] == str(identifier("alpha/user")),
                f"{name} token resolved wrong user",
            )
        denied = client.get(
            "/api/v1/auth/users/me",
            headers={"Authorization": f"Bearer {REVOKED_KEY}"},
        )
        require(denied.status_code in (401, 403), "Revoked credential accepted")
    models_dir = "/app/backend/preloop/models"
    config = Config(f"{models_dir}/alembic.ini")
    config.set_main_option("script_location", f"{models_dir}/alembic")
    heads = ScriptDirectory.from_config(config).get_heads()
    require(len(heads) == 1, "Candidate migration graph has multiple heads")
    current = subprocess.run(
        [sys.executable, "-m", "alembic", "current", "--check-heads"],
        cwd=models_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    require(current.returncode == 0, "Database is not at the image migration head")
    require(heads[0] in current.stdout, "Expected migration head was not observed")
    state["migration_head"] = heads[0]
    state["checks"] = [
        "stable_data",
        "password_logins",
        "existing_user_key",
        "agent_key",
        "existing_session",
        "revoked_key_denied",
        "local_model_secret",
        "local_tracker_secret",
        "local_webhook_secret",
        "single_migration_head",
    ]
    return state


def main() -> None:
    """Return only allowlisted evidence; never print exceptions or tokens."""
    try:
        if sys.argv[1] == "seed":
            seed()
        result = check(json.loads(sys.stdin.read() or "{}"))
    except Exception as exc:
        # SQLAlchemy exceptions may include bound secrets. The orchestrator
        # receives the exception class and our explicitly safe assertion only.
        detail = str(exc) if isinstance(exc, FixtureError) else type(exc).__name__
        frames = traceback.extract_tb(exc.__traceback__)
        location = [f"{frame.name}:{frame.lineno}" for frame in frames[-5:]]
        print(json.dumps({"error": detail, "location": location}), flush=True)
        raise SystemExit(1) from None
    print("UPGRADE_RESULT=" + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
