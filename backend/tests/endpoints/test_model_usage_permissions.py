"""Model and usage controls admit every account member in OSS builds."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_mock import MockerFixture
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import (
    ai_models,
    copilot_usage,
    cost,
    issue_costs,
    usage_import,
)
from preloop.models import crud, models
from preloop.models.db.session import get_db_session
from preloop.schemas.ai_model_pricing import AIModelPrice, AIModelPriceQuote
from preloop.schemas.usage_import import UsageIngestRecordResult
from preloop.services.ai_model_catalog_sync import CatalogSyncSummary
from preloop.services.secret_service import (
    OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
    ResolvedModelCredentials,
)
from preloop.services.usage_repricing import RepriceResult
from preloop.utils import permissions

MODEL_ID = UUID("00000000-0000-0000-0000-000000000001")
WINDOW = {
    "start_date": "2026-09-01T00:00:00Z",
    "end_date": "2026-09-02T00:00:00Z",
}
EVENT = {"timestamp": WINDOW["start_date"], "model": "example-model", "cost_usd": 1}


@dataclass
class Control:
    """An HTTP operation and the effect that should follow authorization."""

    module: ModuleType
    method: str
    path: str
    permission: str
    effect: str
    status: int = 200
    kwargs: dict[str, Any] = field(default_factory=dict)


CONTROLS = [
    Control(
        ai_models,
        "POST",
        "/ai-models",
        "create_ai_models",
        "create",
        201,
        {
            "json": {
                "name": "Example model",
                "provider_name": "openai",
                "model_identifier": "example-model",
                "api_key": "example-key",
            }
        },
    ),
    Control(
        ai_models,
        "PUT",
        f"/ai-models/{MODEL_ID}",
        "edit_ai_models",
        "update",
        kwargs={"json": {"name": "Updated model"}},
    ),
    Control(
        ai_models, "DELETE", f"/ai-models/{MODEL_ID}", "delete_ai_models", "delete", 204
    ),
    Control(
        ai_models,
        "POST",
        f"/ai-models/{MODEL_ID}/pricing/fetch",
        "edit_ai_models",
        "fetch",
    ),
    Control(ai_models, "POST", "/ai-models/sync", "create_ai_models", "sync"),
    Control(
        ai_models,
        "POST",
        f"/ai-models/{MODEL_ID}/credentials/export",
        "edit_ai_models",
        "export",
    ),
    Control(
        copilot_usage,
        "PUT",
        "/cost/copilot/connection",
        "manage_budgets",
        "update",
        kwargs={"json": {"organization": "example-org", "token": "example-token"}},
    ),
    Control(
        copilot_usage,
        "DELETE",
        "/cost/copilot/connection",
        "manage_budgets",
        "delete",
        204,
    ),
    Control(
        copilot_usage,
        "POST",
        "/cost/copilot/connection/sync",
        "manage_budgets",
        "publish",
        202,
    ),
    Control(
        cost,
        "POST",
        "/cost/reprice",
        "manage_budgets",
        "reprice",
        kwargs={"json": WINDOW},
    ),
    Control(
        cost,
        "POST",
        "/cost/ledger-backfill/csv",
        "manage_budgets",
        "backfill",
        kwargs={
            "files": {
                "file": (
                    "ledger.csv",
                    "date__day,model,total_usage\n2026-09-01,example-model,1\n",
                    "text/csv",
                )
            },
            "data": {"apply": "true"},
        },
    ),
    Control(
        issue_costs,
        "POST",
        "/cost/by-issue/rebuild",
        "manage_budgets",
        "rebuild",
        kwargs={"json": WINDOW},
    ),
    Control(
        usage_import,
        "POST",
        "/usage/import",
        "import_usage",
        "import",
        kwargs={"json": {"events": [EVENT]}},
    ),
    Control(
        usage_import,
        "POST",
        "/usage/ingest",
        "import_usage",
        "ingest",
        kwargs={
            "json": {
                "source": "cursor",
                "records": [
                    {
                        "external_id": "example-record",
                        "timestamp": WINDOW["start_date"],
                        "model": "example-model",
                        "charged_cost": 1,
                    }
                ],
            }
        },
    ),
    Control(
        usage_import,
        "POST",
        "/usage/import/csv",
        "import_usage",
        "import",
        kwargs={
            "files": {
                "file": (
                    "usage.csv",
                    "Date,Model,Cost\n2026-09-01,example-model,$1\n",
                    "text/csv",
                )
            }
        },
    ),
]


@pytest.fixture(params=CONTROLS, ids=lambda c: f"{c.method} {c.path}")
def control(request: pytest.FixtureRequest) -> Control:
    return request.param


@pytest.fixture
def operator(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> models.User:
    """Use an active account member with no implicit owner or superuser grant."""
    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    account = crud.crud_account.create(
        db_session, obj_in={"organization_name": "Example organization"}
    )
    return crud.crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "username": f"operator-{uuid4().hex}",
            "email": f"{uuid4().hex}@example.com",
            "hashed_password": "example-password",
            "is_active": True,
            "is_superuser": False,
        },
    )


@pytest.fixture
def effects(
    control: Control, operator: models.User, mocker: MockerFixture
) -> dict[str, MagicMock]:
    """Stub external I/O and writes, leaving permission and ownership reads real."""
    effects: dict[str, MagicMock] = {}

    def stub(name: str, attribute: str, **kwargs: Any) -> MagicMock:
        result = mocker.patch.object(control.module, attribute, **kwargs)
        effects[name] = result
        return result

    if control.module is ai_models:
        model = models.AIModel(
            id=MODEL_ID,
            account_id=operator.account_id,
            name="Example model",
            provider_name="openai",
            model_identifier="example-model",
            is_default=False,
            endpoint_type="direct",
        )
        model_crud = stub("model_crud", "crud_ai_model")
        model_crud.get.return_value = model
        model_crud.create_with_account.return_value = model
        model_crud.update.return_value = model
        effects.update(
            create=model_crud.create_with_account,
            update=model_crud.update,
            delete=model_crud.remove,
        )
        stub(
            "fetch",
            "fetch_provider_pricing",
            return_value=AIModelPriceQuote(
                ai_model_id=str(MODEL_ID),
                source_url="https://example.com/pricing",
                model_key="example-model",
                price=AIModelPrice(input_per_1m=1),
                fetched_at=datetime.now(UTC),
            ),
        )
        stub(
            "sync",
            "sync_account_model_catalog",
            new_callable=AsyncMock,
            return_value=CatalogSyncSummary(),
        )
        secret_service = stub("secret_service", "get_secret_service").return_value
        effects["export"] = secret_service.resolve_ai_model_credentials
        effects["export"].return_value = ResolvedModelCredentials(
            credential_type=OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE,
            backend_type="local_encrypted",
            value="example-access",
            payload={"access": "example-access", "refresh": "example-refresh"},
        )
    elif control.module is copilot_usage:
        connection = models.CopilotImportConnection(
            id=uuid4(),
            account_id=operator.account_id,
            organization="example-org",
            is_active=True,
            currency="USD",
        )
        connection_crud = stub("connection_crud", "crud_copilot_import_connection")
        connection_crud.get_for_account.return_value = connection
        connection_crud.update.return_value = connection
        effects.update(update=connection_crud.update, delete=connection_crud.delete)
        stub("secrets", "get_secret_service")
        effects["publish"] = mocker.patch.object(
            copilot_usage.event_bus_service,
            "publish_task",
            new_callable=AsyncMock,
            return_value=object(),
        )
        stub("delete_secret", "_delete_secret")
    elif control.module is cost:
        stub("reprice", "reprice_gateway_usage", return_value=RepriceResult())
        stub("load_rows", "load_unpriced_rows", return_value=[])
        stub("backfill", "apply_ledger_backfill", return_value=0)
        stub("invalidate", "_forget_cost_health")
    elif control.module is issue_costs:
        effects["rebuild"] = mocker.patch.object(
            issue_costs.issue_cost_rollup, "rebuild", return_value=(1, 1, 0)
        )
    else:
        stub(
            "resolve_agent",
            "resolve_target_agent",
            return_value=SimpleNamespace(
                id=uuid4(),
                display_name="Example agent",
            ),
        )
        stub("import", "ingest_events", return_value=(1, 0))
        stub(
            "ingest",
            "ingest_push_records",
            return_value=[UsageIngestRecordResult(external_id="example-record")],
        )
    return effects


@pytest.fixture
def permission_client(
    control: Control, operator: models.User, db_session: Session
) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(control.module.router, prefix="/api/v1")
    app.dependency_overrides[get_current_active_user] = lambda: operator
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("grant", ["none", "primary", "permission"])
def test_every_account_member_can_use_control(
    control: Control,
    permission_client: TestClient,
    effects: dict[str, MagicMock],
    operator: models.User,
    db_session: Session,
    grant: str,
) -> None:
    """Members reach the control in core whether or not they hold a role.

    The core edition has no access controls: a member with no role at all
    (``none``) is admitted just like the primary user or a role holder.
    """
    account = crud.crud_account.get(db_session, id=operator.account_id)
    assert account is not None
    assert permissions.members_have_full_access()
    if grant == "primary":
        crud.crud_account.update(
            db_session, db_obj=account, obj_in={"primary_user_id": operator.id}
        )
    elif grant == "permission":
        permission = crud.crud_permission.get_by_name(
            db_session, name=control.permission
        )
        if permission is None:
            permission = crud.crud_permission.create(
                db_session,
                obj_in={
                    "name": control.permission,
                    "description": "Example permission",
                    "category": "example",
                },
            )
        role = crud.crud_role.create(
            db_session,
            obj_in={
                "account_id": account.id,
                "name": "Example operator",
                "is_system_role": False,
            },
        )
        crud.crud_role.assign_permission(
            db_session, role_id=role.id, permission_id=permission.id
        )
        crud.crud_user_role.create(
            db_session, obj_in={"user_id": operator.id, "role_id": role.id}
        )

    response = permission_client.request(
        control.method, f"/api/v1{control.path}", **control.kwargs
    )

    assert response.status_code == control.status, response.text
    effects[control.effect].assert_called_once()
