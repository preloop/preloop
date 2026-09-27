"""Account hierarchy hooks on HTTP paths: H1 sign in, H3 gateway, H4 gates.

The crud and service level cases (H2, H3 lists, H4 tool, model and runner
checks, H5 to H8) are in ``tests/services/test_account_hierarchy_hooks.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
import jwt
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_password_hash, verify_password
from preloop.api.endpoints.openai_gateway import get_model_gateway_auth_context
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_flow,
    crud_user,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import (
    ACTION_RESOURCE_VIEW,
    VISIBLE_AI_MODEL,
    Decision,
    LoginRowSelector,
    VisibilityProvider,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.runner_service import hash_runner_token
from preloop.utils.tokens import create_password_reset_token

PASSWORD = "hierarchy-hooks-pass-1"


@pytest.fixture(autouse=True)
def _no_hooks():
    account_hooks.reset_account_hooks()
    yield
    account_hooks.reset_account_hooks()


@pytest.fixture
def anon_client(db_session: Session) -> Iterator[TestClient]:
    from preloop.api.app import create_app
    from preloop.models.db.session import get_db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        yield client


def _password_user(db: Session, email: str | None = None) -> models.User:
    unique = uuid.uuid4().hex[:8]
    account = crud_account.create(
        db, obj_in={"organization_name": f"Org {unique}", "is_active": True}
    )
    return crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "username": f"hook{unique}",
            "email": email or f"hook{unique}@example.com",
            "full_name": "Hook User",
            "is_active": True,
            "email_verified": True,
            "hashed_password": get_password_hash(PASSWORD),
            "user_source": "local",
        },
    )


class _Selector(LoginRowSelector):
    """Lands every row-based action on ``target`` and records the purposes."""

    def __init__(self, target: models.User | None = None) -> None:
        self.target = target
        self.row_calls: list[tuple[Any, str]] = []
        self.email_calls: list[tuple[str, str, int]] = []

    def select_row(self, db, *, user, purpose):
        self.row_calls.append((user.id, purpose))
        return self.target or user

    def select_email_rows(self, db, *, email, rows, purpose):
        self.email_calls.append((email, purpose, len(rows)))
        if self.target is not None:
            return [row for row in rows if row.id == self.target.id]
        return rows[:1]


def _token_sub(response) -> str:
    token = response.json()["access_token"]
    return jwt.decode(token, options={"verify_signature": False})["sub"]


# ---------------------------------------------------------------------------
# H1: login row selector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("form", [False, True])
def test_h1_sign_in_lands_on_the_selected_row(
    db_session: Session, anon_client: TestClient, form: bool
) -> None:
    checked = _password_user(db_session)
    landing = _password_user(db_session)

    def sign_in():
        if form:
            return anon_client.post(
                "/api/v1/auth/token",
                data={"username": checked.username, "password": PASSWORD},
            )
        return anon_client.post(
            "/api/v1/auth/token/json",
            json={"username": checked.username, "password": PASSWORD},
        )

    unset = sign_in()
    assert unset.status_code == 200, unset.text
    assert _token_sub(unset) == str(checked.id)

    selector = _Selector(landing)
    account_hooks.register_login_row_selector(selector)
    response = sign_in()

    assert response.status_code == 200, response.text
    assert _token_sub(response) == str(landing.id)
    assert selector.row_calls == [(checked.id, "login")]


def test_h1_reset_link_acts_on_the_selected_row(
    db_session: Session, anon_client: TestClient
) -> None:
    named = _password_user(db_session)
    landing = _password_user(db_session)
    selector = _Selector(landing)
    account_hooks.register_login_row_selector(selector)
    token = create_password_reset_token(named.email, user_id=named.id)

    response = anon_client.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": "a-brand-new-pass-2"},
    )

    assert response.status_code == 200, response.text
    assert selector.row_calls == [(named.id, "reset_password")]
    db_session.expire_all()
    assert verify_password(
        "a-brand-new-pass-2", crud_user.get(db_session, id=landing.id).hashed_password
    )
    assert verify_password(
        PASSWORD, crud_user.get(db_session, id=named.id).hashed_password
    )


def test_h1_address_requests_mail_only_the_selected_rows(
    db_session: Session, anon_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = f"shared{uuid.uuid4().hex[:8]}@example.com"
    _password_user(db_session, email=email)
    second = _password_user(db_session, email=email)
    for row in crud_user.list_by_email(db_session, email=email):
        row.email_verified = False
    db_session.flush()
    resets: list[Any] = []
    verifications: list[Any] = []
    monkeypatch.setattr(
        "preloop.api.auth.router.send_password_reset_email",
        lambda *args, **kwargs: resets.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "preloop.api.auth.router.send_verification_email",
        lambda *args, **kwargs: verifications.append((args, kwargs)),
    )
    selector = _Selector(target=second)
    account_hooks.register_login_row_selector(selector)

    assert (
        anon_client.post(
            "/api/v1/auth/forgot-password", json={"email": email}
        ).status_code
        == 200
    )
    assert (
        anon_client.post(
            "/api/v1/auth/resend-verification", json={"email": email}
        ).status_code
        == 200
    )

    assert selector.email_calls == [
        (email, "forgot_password", 2),
        (email, "resend_verification", 2),
    ]
    assert [kwargs["username"] for _args, kwargs in resets] == [second.username]
    assert [kwargs["username"] for _args, kwargs in verifications] == [second.username]


# ---------------------------------------------------------------------------
# H3: a model another account shares, through the gateway
# ---------------------------------------------------------------------------


class _Visible(VisibilityProvider):
    def __init__(self, **ids: list[Any]) -> None:
        self.ids = ids

    def extra_visible_ids(self, db, account_id, resource_type):
        return self.ids.get(resource_type, [])


def _gateway_model(db: Session, account_id: Any, secret: str) -> models.AIModel:
    return crud_ai_model.create_with_account(
        db=db,
        obj_in={
            "name": f"Shared {uuid.uuid4().hex[:6]}",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": secret,
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "team/shared-model",
                    "provider_adapter": "preloop",
                    "responses_api": "transcode",
                }
            },
        },
        account_id=account_id,
    )


def _chat(client: TestClient):
    completion = {
        "id": "chatcmpl_shared",
        "created": 1710000000,
        "choices": [
            {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }
    with patch(
        "preloop.services.openai_gateway.litellm.completion", return_value=completion
    ) as upstream:
        response = client.post(
            "/openai/v1/chat/completions",
            json={
                "model": "team/shared-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    return response, upstream


def test_h3_gateway_resolves_a_shared_model_and_an_own_alias_wins(
    app, client: TestClient, db_session: Session, test_user
) -> None:
    owner = crud_account.create(
        db_session, obj_in={"organization_name": f"Owner {uuid.uuid4().hex[:6]}"}
    )
    foreign_secret = f"sk-foreign-{uuid.uuid4().hex}"
    foreign = _gateway_model(db_session, owner.id, foreign_secret)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=test_user)
    )

    unshared, upstream = _chat(client)
    assert unshared.status_code != 200
    upstream.assert_not_called()

    account_hooks.register_visibility_provider(
        _Visible(**{VISIBLE_AI_MODEL: [foreign.id]})
    )
    shared, upstream = _chat(client)

    assert shared.status_code == 200, shared.text
    # The owner's credential is used on the server...
    assert upstream.call_args.kwargs["api_key"] == foreign_secret
    # ...and never reaches the caller, in any response it can read.
    listed = client.get("/openai/v1/models")
    account_models = client.get("/api/v1/ai-models")
    for response in (shared, listed, account_models):
        assert foreign_secret not in response.text
    assert "team/shared-model" in listed.text

    own_secret = f"sk-own-{uuid.uuid4().hex}"
    _gateway_model(db_session, test_user.account_id, own_secret)
    own, upstream = _chat(client)

    assert own.status_code == 200, own.text
    assert upstream.call_args.kwargs["api_key"] == own_secret
    assert foreign_secret not in own.text


def test_h3_shared_model_credential_cannot_drive_provider_listing(
    client: TestClient,
    db_session: Session,
    test_user,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing decrypts the stored key and may send it to a caller-chosen
    endpoint, so a shared model id must not resolve there."""
    owner = crud_account.create(
        db_session, obj_in={"organization_name": f"Owner {uuid.uuid4().hex[:6]}"}
    )
    foreign = _gateway_model(db_session, owner.id, f"sk-foreign-{uuid.uuid4().hex}")
    account_hooks.register_visibility_provider(
        _Visible(**{VISIBLE_AI_MODEL: [foreign.id]})
    )
    decrypted: list[Any] = []
    monkeypatch.setattr(
        crud_ai_model,
        "resolve_listing_secret",
        lambda ai_model: decrypted.append(ai_model.id) or "never-used",
    )

    response = client.post(
        "/api/v1/ai-models/providers/openai/available-models",
        json={
            "ai_model_id": str(foreign.id),
            "api_endpoint": "https://collector.example.com/v1",
        },
    )

    assert response.status_code == 404, response.text
    assert decrypted == []
    assert (
        crud_ai_model.get_for_account(
            db_session, id=foreign.id, account_id=test_user.account_id
        )
        is None
    )


# ---------------------------------------------------------------------------
# H4: require_permission and list endpoints
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self, deny=lambda action, resource: False) -> None:
        self.calls: list[tuple[Any, str, Any]] = []
        self.deny = deny

    def __call__(self, ctx, action, resource):
        self.calls.append((ctx, action, resource))
        if self.deny(action, resource):
            return Decision("deny", rule_ids=("r1",), reason="denied by r1")
        return Decision("allow")


def test_h4_require_permission_consults_the_authorizer(
    client: TestClient, test_user
) -> None:
    assert client.get("/api/v1/flows").status_code == 200
    recorder = _Recorder(lambda action, resource: action == "view_flows")
    account_hooks.register_authorizer(recorder)

    response = client.get("/api/v1/flows")

    assert response.status_code == 403
    assert response.json()["detail"] == "denied by r1"
    ctx, action, resource = recorder.calls[0]
    assert (action, resource) == ("view_flows", None)
    assert ctx.account_id == test_user.account_id


def _seed_listables(db: Session, account_id: Any) -> dict[str, list[str]]:
    """Two rows of every shareable kind in the account, keyed by name."""
    names: dict[str, list[str]] = {}
    now = datetime.now(UTC).replace(tzinfo=None)
    for index in range(2):
        tag = f"{index}-{uuid.uuid4().hex[:6]}"
        crud_flow.create(
            db=db,
            flow_in=FlowCreate(
                name=f"flow-{tag}",
                prompt_template="Test",
                trigger_event_source="github",
                trigger_event_types=["push"],
                agent_type="codex",
                agent_config={},
                account_id=account_id,
            ),
            account_id=account_id,
        )
        db.add(
            models.FlowRunner(
                account_id=account_id,
                name=f"runner-{tag}",
                token_hash=hash_runner_token(f"token-{tag}"),
                labels=[],
                status="offline",
            )
        )
        db.add(
            models.MCPServer(
                name=f"mcp-{tag}",
                url="http://localhost:8080/mcp",
                transport="http-streaming",
                auth_type="none",
                account_id=account_id,
                status="active",
            )
        )
        db.add(
            models.AIModel(
                name=f"model-{tag}",
                provider_name="openai",
                model_identifier="gpt-5",
                account_id=account_id,
            )
        )
        db.add(
            models.ManagedAgent(
                id=uuid.uuid4(),
                account_id=account_id,
                agent_kind="codex",
                session_source_type="codex",
                session_source_id=f"agent-{tag}",
                display_name=f"agent-{tag}",
                enrolled_via="runtime_session_token",
                lifecycle_state="active",
                lifecycle_updated_at=now,
                last_seen_at=now,
                tags={},
            )
        )
        for kind in ("flow", "runner", "mcp", "model", "agent"):
            names.setdefault(kind, []).append(f"{kind}-{tag}")
    db.flush()
    return names


def _name_of(resource: Any) -> str | None:
    if isinstance(resource, dict):
        return resource.get("display_name") or resource.get("name")
    return getattr(resource, "name", None)


def _listed_names(body: Any) -> set[str]:
    items = body["items"] if isinstance(body, dict) else body
    return {item.get("display_name") or item.get("name") for item in items}


@pytest.mark.parametrize(
    "path, kind, resource_type",
    [
        ("/api/v1/flows", "flow", "flow"),
        ("/api/v1/runners", "runner", "runner"),
        ("/api/v1/mcp-servers", "mcp", "mcp_server"),
        ("/api/v1/ai-models", "model", "ai_model"),
        ("/api/v1/agents", "agent", "managed_agent"),
    ],
)
def test_h4_list_endpoints_drop_rows_denied_resource_view(
    client: TestClient,
    db_session: Session,
    test_user,
    path: str,
    kind: str,
    resource_type: str,
) -> None:
    names = _seed_listables(db_session, test_user.account_id)[kind]
    before = client.get(path)
    assert before.status_code == 200, before.text
    assert set(names) <= _listed_names(before.json())

    hidden = names[0]
    recorder = _Recorder(
        lambda action, resource: action == ACTION_RESOURCE_VIEW
        and _name_of(resource) == hidden
    )
    account_hooks.register_authorizer(recorder)
    after = client.get(path)

    assert after.status_code == 200, after.text
    listed = _listed_names(after.json())
    assert hidden not in listed
    assert names[1] in listed
    views = [call for call in recorder.calls if call[1] == ACTION_RESOURCE_VIEW]
    assert views
    assert {call[0].attributes["resource_type"] for call in views} == {resource_type}


def test_no_response_body_changes_without_hooks(
    client: TestClient, db_session: Session, test_user
) -> None:
    _seed_listables(db_session, test_user.account_id)
    paths = [
        "/api/v1/flows",
        "/api/v1/runners",
        "/api/v1/mcp-servers",
        "/api/v1/ai-models",
        "/api/v1/agents",
    ]
    unset = {path: client.get(path).json() for path in paths}
    # An allow-everything authorizer and an empty visibility provider must
    # leave every list exactly as it was.
    account_hooks.register_authorizer(lambda ctx, action, resource: Decision("allow"))
    account_hooks.register_visibility_provider(_Visible())
    allowed = {path: client.get(path).json() for path in paths}

    assert allowed == unset
