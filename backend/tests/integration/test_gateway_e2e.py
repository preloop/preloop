"""End-to-end integration tests for the Preloop model gateway.

These tests exercise the *full* gateway stack over real HTTP:

    TestClient -> FastAPI route -> real bearer-token authentication
    (``authenticate_bearer_token``) -> ``OpenAIGatewayService`` -> upstream
    -> ``ApiUsage`` ledger persistence

They are deliberately *non-overlapping* with the existing suites:

* ``tests/endpoints/test_openai_gateway.py`` / ``test_anthropic_gateway.py``
  override the auth dependency (bypassing real authentication) and never
  assert the persisted ``ApiUsage`` ledger.
* ``tests/services/test_model_gateway_usage.py`` asserts the ledger but calls
  the service directly, bypassing HTTP, routing and authentication.

Here we instead:

1. Mint a **real** runtime API key via the CRUD layer and present it as a
   bearer token, so the real authentication + subject-attribution chain runs.
2. Drive the **real** HTTP endpoints through ``TestClient``.
3. Assert the **persisted ``ApiUsage`` ledger row** (tokens, model alias,
   account / api-key / runtime-principal attribution).

The provider network is kept hermetic two ways:

* For the core OpenAI-provider flows we stand up a tiny *real* OpenAI-compatible
  upstream HTTP server (reusing ``tests/e2e_support/fake_upstream``'s response
  body) so ``litellm`` makes a genuine HTTP round-trip -- no provider key, no
  ``litellm`` mock. A request counter on that server lets us prove that budget /
  allow-list denials block **before** any upstream dispatch.
* For streaming (SSE) and the Anthropic wire format -- which the OpenAI-shaped
  fake upstream cannot speak -- we mock ``litellm.completion`` (the provider
  network boundary), while still routing through real HTTP + real auth and
  asserting the ledger.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

import pytest

from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
)
from preloop.models.crud.plan import plan as crud_plan
from preloop.models.crud.plan import subscription as crud_subscription
from preloop.models.models.api_usage import ApiUsage
from preloop.services.subject_governance import (
    SUBJECT_TYPE_API_KEYS,
    set_subject_governance,
)
from tests.e2e_support.fake_upstream import _Handler as _BaseUpstreamHandler
from tests.e2e_support.fake_upstream import _completion_body


# ---------------------------------------------------------------------------
# Real OpenAI-compatible upstream server (counting) for true-wire E2E.
# ---------------------------------------------------------------------------


@dataclass
class _UpstreamState:
    """Shared, thread-safe-ish state for the fake upstream server."""

    base_url: str
    request_count: int = 0
    paths: List[str] = field(default_factory=list)

    def reset(self) -> None:
        self.request_count = 0
        self.paths.clear()


def _make_counting_handler(state: _UpstreamState):
    class _CountingHandler(_BaseUpstreamHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._send_json(404, {"error": {"message": "not found"}})
                return
            state.request_count += 1
            state.paths.append(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {}
            self._send_json(200, _completion_body(payload))

    return _CountingHandler


@pytest.fixture(scope="module")
def fake_upstream() -> Iterator[_UpstreamState]:
    """Start one OpenAI-compatible upstream server for the module."""
    state = _UpstreamState(base_url="")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_counting_handler(state))
    host, port = server.server_address[0], server.server_address[1]
    state.base_url = f"http://{host}:{port}/v1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _reset_upstream(request: Any) -> None:
    """Reset the upstream request counter before each test that uses it."""
    if "fake_upstream" in request.fixturenames:
        request.getfixturevalue("fake_upstream").reset()


@pytest.fixture(autouse=True)
def _allow_permissions(mocker: Any) -> None:
    """Permit endpoints if the EE RBAC plugin is importable (mirrors endpoint conftest)."""
    try:
        import preloop.plugins.proprietary.rbac.permissions  # noqa: F401

        mocker.patch(
            "preloop.plugins.proprietary.rbac.permissions.has_permission",
            return_value=True,
        )
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_PROMPT_TOKENS = 11
_FAKE_COMPLETION_TOKENS = 9
_FAKE_TOTAL_TOKENS = 20
_FAKE_CONTENT = "Hello from the Preloop E2E fake upstream."


def _seed_gateway_model(
    db_session: Any,
    account_id: Any,
    *,
    alias: str,
    provider_name: str = "openai",
    model_identifier: str = "gpt-4o-mini",
    api_endpoint: Optional[str] = None,
    is_default: bool = False,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Any:
    """Create a gateway-enabled AIModel resolvable by ``alias``."""
    meta: Dict[str, Any] = {
        "gateway": {
            "enabled": True,
            "model_alias": alias,
            "provider_adapter": "preloop",
        }
    }
    if extra_meta:
        meta.update(extra_meta)
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"E2E Gateway Model {alias}",
            "provider_name": provider_name,
            "model_identifier": model_identifier,
            "api_key": "e2e-fake-upstream-key",
            "api_endpoint": api_endpoint,
            "is_default": is_default,
            "meta_data": meta,
        },
        account_id=account_id,
    )


def _mint_runtime_token(
    db_session: Any,
    test_user: Any,
    *,
    principal: Optional[Dict[str, Any]] = None,
    context_extra: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, str]:
    """Mint a real runtime API key; returns ``(api_key, bearer_token)``."""
    context_data: Dict[str, Any] = {}
    if principal is not None:
        context_data["runtime_principal"] = principal
    if context_extra:
        context_data.update(context_extra)
    return crud_api_key.create_runtime_key(
        db_session,
        name="E2E Gateway Runtime Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data=context_data,
    )


def _usage_rows(db_session: Any, endpoint: str) -> List[ApiUsage]:
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == endpoint)
        .order_by(ApiUsage.timestamp.asc())
        .all()
    )


def _latest_usage(db_session: Any, endpoint: str) -> Optional[ApiUsage]:
    rows = _usage_rows(db_session, endpoint)
    return rows[-1] if rows else None


_OPENAI_LITELLM_RESPONSE = {
    "id": "chatcmpl_e2e",
    "created": 1710000000,
    "choices": [
        {
            "message": {"role": "assistant", "content": "Hello from mocked upstream"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
}


def _openai_stream_chunks() -> Iterator[Dict[str, Any]]:
    return iter(
        [
            {
                "id": "chatcmpl_e2e",
                "created": 1710000000,
                "choices": [{"index": 0, "delta": {"content": "Hello"}}],
            },
            {
                "id": "chatcmpl_e2e",
                "created": 1710000000,
                "choices": [{"index": 0, "delta": {"content": " world"}}],
            },
            {
                "id": "chatcmpl_e2e",
                "created": 1710000000,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 5,
                    "total_tokens": 12,
                },
            },
        ]
    )


# ===========================================================================
# 1. OpenAI chat/completions -- real wire round-trip + ledger attribution
# ===========================================================================


def test_openai_chat_completion_success_persists_usage_ledger(
    client, db_session, test_user, fake_upstream
):
    """A successful OpenAI chat completion returns content and persists usage."""
    model = _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="openai/gpt-4o-e2e",
        api_endpoint=fake_upstream.base_url,
    )
    api_key, token = _mint_runtime_token(
        db_session,
        test_user,
        principal={"type": "custom", "id": "agent-e2e-1", "name": "E2E Agent"},
    )

    response = client.post(
        "/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "openai/gpt-4o-e2e",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == _FAKE_CONTENT
    assert body["usage"]["total_tokens"] == _FAKE_TOTAL_TOKENS
    # A genuine HTTP round-trip happened against the fake upstream.
    assert fake_upstream.request_count >= 1

    usage = _latest_usage(db_session, "/openai/v1/chat/completions")
    assert usage is not None
    assert usage.status_code == 200
    assert usage.prompt_tokens == _FAKE_PROMPT_TOKENS
    assert usage.completion_tokens == _FAKE_COMPLETION_TOKENS
    assert usage.total_tokens == _FAKE_TOTAL_TOKENS
    assert usage.model_alias == "openai/gpt-4o-e2e"
    assert usage.provider_name == "openai"
    # Attribution: account, model, api-key subject, runtime principal.
    assert str(usage.account_id) == str(test_user.account_id)
    assert str(usage.ai_model_id) == str(model.id)
    assert str(usage.api_key_id) == str(api_key.id)
    assert usage.auth_subject_type == "api_key"
    assert usage.runtime_principal_type == "custom"
    assert usage.runtime_principal_id == "agent-e2e-1"
    assert usage.runtime_principal_name == "E2E Agent"


def test_openai_chat_completion_default_model_when_model_omitted(
    client, db_session, test_user, fake_upstream
):
    """Omitting ``model`` resolves the gateway-enabled default and dispatches."""
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="openai/default-e2e",
        api_endpoint=fake_upstream.base_url,
        is_default=True,
    )
    _, token = _mint_runtime_token(db_session, test_user)

    response = client.post(
        "/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200, response.text
    assert fake_upstream.request_count >= 1
    usage = _latest_usage(db_session, "/openai/v1/chat/completions")
    assert usage is not None
    assert usage.model_alias == "openai/default-e2e"


# ===========================================================================
# 2. OpenAI streaming (SSE) -- chunks stream and usage is still accounted
# ===========================================================================


def test_openai_streaming_sse_accounts_usage(client, db_session, test_user):
    """Streaming chat completions emit SSE chunks and persist a usage ledger row."""
    _seed_gateway_model(db_session, test_user.account_id, alias="openai/stream-e2e")
    _, token = _mint_runtime_token(
        db_session,
        test_user,
        principal={"type": "custom", "id": "stream-agent", "name": "Stream Agent"},
    )

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_openai_stream_chunks(),
    ):
        response = client.post(
            "/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "openai/stream-e2e",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "chat.completion.chunk" in response.text
    assert "data: [DONE]" in response.text
    # Streamed text is reassembled across deltas.
    assert "Hello" in response.text and "world" in response.text

    usage = _latest_usage(db_session, "/openai/v1/chat/completions")
    assert usage is not None
    assert usage.status_code == 200
    assert usage.total_tokens == 12
    assert usage.model_alias == "openai/stream-e2e"
    assert usage.runtime_principal_id == "stream-agent"


# ===========================================================================
# 3. Anthropic-compatible messages -- success + ledger accounting
# ===========================================================================


def test_anthropic_messages_success_persists_usage_ledger(
    client, db_session, test_user
):
    """A successful Anthropic message returns Anthropic shape and persists usage."""
    model = _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="anthropic/claude-e2e",
        provider_name="anthropic",
        model_identifier="claude-sonnet-4-5",
    )
    api_key, token = _mint_runtime_token(
        db_session,
        test_user,
        principal={"type": "custom", "id": "claude-agent", "name": "Claude Agent"},
    )

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_OPENAI_LITELLM_RESPONSE,
    ):
        response = client.post(
            "/anthropic/v1/messages",
            headers={
                "x-api-key": token,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "anthropic/claude-e2e",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 256,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["content"][0]["text"] == "Hello from mocked upstream"
    assert body["usage"]["input_tokens"] == 7
    assert body["usage"]["output_tokens"] == 5

    usage = _latest_usage(db_session, "/anthropic/v1/messages")
    assert usage is not None
    assert usage.status_code == 200
    assert usage.prompt_tokens == 7
    assert usage.completion_tokens == 5
    assert usage.model_alias == "anthropic/claude-e2e"
    assert usage.provider_name == "anthropic"
    assert str(usage.ai_model_id) == str(model.id)
    assert str(usage.api_key_id) == str(api_key.id)
    assert usage.runtime_principal_id == "claude-agent"


def test_anthropic_messages_stream_sse_accounts_usage(client, db_session, test_user):
    """Anthropic streaming emits Anthropic SSE events and persists usage."""
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="anthropic/claude-stream-e2e",
        provider_name="anthropic",
        model_identifier="claude-sonnet-4-5",
    )
    _, token = _mint_runtime_token(db_session, test_user)

    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_openai_stream_chunks(),
    ):
        response = client.post(
            "/anthropic/v1/messages",
            headers={
                "x-api-key": token,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "anthropic/claude-stream-e2e",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 256,
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: message_start" in response.text
    assert "event: content_block_delta" in response.text
    assert "event: message_stop" in response.text

    usage = _latest_usage(db_session, "/anthropic/v1/messages")
    assert usage is not None
    assert usage.total_tokens == 12
    assert usage.model_alias == "anthropic/claude-stream-e2e"


# ===========================================================================
# 4. GET /openai/v1/models -- lists allowed aliases for the subject
# ===========================================================================


def test_list_models_returns_gateway_aliases_for_subject(
    client, db_session, test_user, fake_upstream
):
    """The models endpoint lists every gateway-enabled alias for the account."""
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="openai/list-a",
        api_endpoint=fake_upstream.base_url,
    )
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="anthropic/list-b",
        provider_name="anthropic",
        model_identifier="claude-sonnet-4-5",
    )
    # A non-gateway model must NOT appear in the listing.
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Direct Non-Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-4o",
            "api_key": "secret",
        },
        account_id=test_user.account_id,
    )
    _, token = _mint_runtime_token(db_session, test_user)

    response = client.get(
        "/openai/v1/models",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "list"
    ids = {entry["id"] for entry in body["data"]}
    assert ids == {"openai/list-a", "anthropic/list-b"}
    # No upstream dispatch for a metadata listing.
    assert fake_upstream.request_count == 0


# ===========================================================================
# 5. Budget enforcement -- block before upstream dispatch
# ===========================================================================


def _seed_trialing_subscription(db_session: Any, account_id: Any) -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    crud_plan.create(
        db_session,
        obj_in={
            "id": "teams",
            "name": "Teams",
            "price_monthly": 0.0,
            "price_annually": 0.0,
            "is_active": True,
            "features": {},
            "is_custom": False,
        },
    )
    crud_subscription.create(
        db_session,
        obj_in={
            "account_id": account_id,
            "plan_id": "teams",
            "status": "trialing",
            "current_period_start": now - timedelta(days=1),
            "current_period_end": now + timedelta(days=13),
        },
    )


def test_trial_hosted_budget_blocks_before_upstream_dispatch(
    client, db_session, test_user, fake_upstream
):
    """A trial hosted-model hard cap denies with 403 and never dispatches upstream."""
    _seed_trialing_subscription(db_session, test_user.account_id)
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="openai/hosted-e2e",
        api_endpoint=fake_upstream.base_url,
        extra_meta={
            "hosted": True,
            "pricing": {
                "input_price_per_1k": 100.0,
                "output_price_per_1k": 100.0,
            },
        },
    )
    _, token = _mint_runtime_token(db_session, test_user)

    response = client.post(
        "/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "openai/hosted-e2e",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 1000,
        },
    )

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["type"] == "permission_error"
    assert "budget exceeded" in body["error"]["message"].lower()
    # Block happened BEFORE any upstream dispatch.
    assert fake_upstream.request_count == 0
    # A denial fact is still persisted to the ledger.
    usage = _latest_usage(db_session, "/openai/v1/chat/completions")
    assert usage is not None
    assert usage.status_code == 403
    assert usage.model_alias == "openai/hosted-e2e"


# ===========================================================================
# 6. Allowed-model enforcement (subject governance allow-list)
# ===========================================================================


def _set_allowed_models(
    db_session: Any, account_id: Any, api_key: Any, allowed: List[str]
) -> None:
    account = crud_account.get(db_session, id=account_id)
    account.meta_data = set_subject_governance(
        account.meta_data or {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id=str(api_key.id),
        config={"allowed_models": allowed},
    )
    db_session.add(account)
    db_session.commit()


def test_disallowed_model_rejected_before_dispatch(
    client, db_session, test_user, fake_upstream
):
    """A model outside the subject's allow-list is rejected without dispatch."""
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="openai/allowed-e2e",
        api_endpoint=fake_upstream.base_url,
    )
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="openai/forbidden-e2e",
        api_endpoint=fake_upstream.base_url,
    )
    api_key, token = _mint_runtime_token(db_session, test_user)
    _set_allowed_models(
        db_session, test_user.account_id, api_key, ["openai/allowed-e2e"]
    )

    response = client.post(
        "/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "openai/forbidden-e2e",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    assert response.status_code == 403, response.text
    assert fake_upstream.request_count == 0


def test_allowed_model_permitted_and_dispatches(
    client, db_session, test_user, fake_upstream
):
    """A model present in the subject's allow-list dispatches normally."""
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="openai/allowed-e2e",
        api_endpoint=fake_upstream.base_url,
    )
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="openai/forbidden-e2e",
        api_endpoint=fake_upstream.base_url,
    )
    api_key, token = _mint_runtime_token(db_session, test_user)
    _set_allowed_models(
        db_session, test_user.account_id, api_key, ["openai/allowed-e2e"]
    )

    response = client.post(
        "/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "openai/allowed-e2e",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    assert response.status_code == 200, response.text
    assert fake_upstream.request_count >= 1


def test_unknown_model_returns_404_without_dispatch(
    client, db_session, test_user, fake_upstream
):
    """Requesting a model alias that is not configured returns 404 without dispatch."""
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="openai/known-e2e",
        api_endpoint=fake_upstream.base_url,
    )
    _, token = _mint_runtime_token(db_session, test_user)

    response = client.post(
        "/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "openai/does-not-exist",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    assert response.status_code == 404, response.text
    assert fake_upstream.request_count == 0


# ===========================================================================
# Authentication boundary (real bearer-token auth chain)
# ===========================================================================


def test_missing_bearer_token_returns_401(client, db_session, test_user):
    """No Authorization header is rejected by the real auth dependency."""
    response = client.post(
        "/openai/v1/chat/completions",
        json={
            "model": "openai/anything",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 401


def test_invalid_bearer_token_returns_401(client, db_session, test_user):
    """An unrecognised bearer token fails real authentication."""
    response = client.post(
        "/openai/v1/chat/completions",
        headers={"Authorization": "Bearer not-a-real-token"},
        json={
            "model": "openai/anything",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )
    assert response.status_code == 401


# ===========================================================================
# Per-run session attribution over real HTTP (X-Preloop-Session-Id)
# ===========================================================================


def test_same_session_header_shares_runtime_session_over_http(
    client, db_session, test_user, fake_upstream
):
    """Two requests with the same X-Preloop-Session-Id share one runtime session."""
    _seed_gateway_model(
        db_session,
        test_user.account_id,
        alias="openai/session-e2e",
        api_endpoint=fake_upstream.base_url,
    )
    payload = {
        "model": "openai/session-e2e",
        "messages": [{"role": "user", "content": "Hello"}],
    }

    _, token_a = _mint_runtime_token(
        db_session,
        test_user,
        principal={"type": "custom", "id": "sess-agent", "name": "Sess Agent"},
    )
    resp_a = client.post(
        "/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {token_a}",
            "X-Preloop-Session-Id": "run-shared",
        },
        json=payload,
    )
    _, token_b = _mint_runtime_token(
        db_session,
        test_user,
        principal={"type": "custom", "id": "sess-agent", "name": "Sess Agent"},
    )
    resp_b = client.post(
        "/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {token_b}",
            "X-Preloop-Session-Id": "run-shared",
        },
        json=payload,
    )

    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text

    rows = _usage_rows(db_session, "/openai/v1/chat/completions")
    success_rows = [r for r in rows if r.status_code == 200]
    session_ids = {r.runtime_session_id for r in success_rows}
    assert None not in session_ids
    assert len(session_ids) == 1
