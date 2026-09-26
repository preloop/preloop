"""Endpoint tests for the gateway embeddings route.

Vectors are metered spend like completions: the route must return the
upstream vectors, record exactly one usage row against the calling account,
key and model, price that row from the catalog, and flag a model the catalog
does not know as ``unpriced`` rather than a silent $0. The authentication and
account-scoping tests are parametrized over the two pre-existing routes and
the new one so the shared contract is asserted by the same test bodies.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from litellm import model_cost, register_model

from preloop.api.endpoints.openai_gateway import get_model_gateway_auth_context
from preloop.models.crud import (
    crud_account,
    crud_account_halt,
    crud_ai_model,
    crud_api_key,
    crud_runtime_session,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.services.kill_switch import invalidate_kill_switch_cache
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_budget import BudgetCheckResult
from preloop.services.openai_gateway import OpenAIGatewayService

LITELLM_EMBEDDING = "preloop.services.openai_gateway.litellm.embedding"
EMBEDDINGS_URL = "/openai/v1/embeddings"

# Priced only inside the fixture below, so the cost assertion is pinned to a
# known number instead of whatever the vendored catalog happens to charge.
FIXTURE_MODEL = "preloop-test-embedding-fixture"
FIXTURE_INPUT_COST_PER_TOKEN = 0.00000002
# Absent from litellm's map and from the vendored catalog on purpose.
UNPRICED_MODEL = "preloop-test-embedding-without-a-price"


def _create_embedding_model(
    db_session,
    account_id,
    *,
    model_identifier: str = FIXTURE_MODEL,
    alias: str | None = None,
):
    """Register a gateway-enabled embedding model for an account."""
    alias = alias or f"openai/{model_identifier}"
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Embedding Model {model_identifier}",
            "provider_name": "openai",
            "model_identifier": model_identifier,
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": alias,
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=account_id,
    )


def _runtime_key_auth(app, db_session, test_user):
    """Authenticate as a runtime key of the test account."""
    api_key, presented_token = crud_api_key.create_runtime_key(
        db_session,
        name="Embeddings Runtime Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={},
    )
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token=presented_token, user=test_user, api_key=api_key)
    )
    return api_key


def _upstream_response(*, prompt_tokens: int = 8, vectors=None) -> dict:
    vectors = vectors if vectors is not None else [[0.1, 0.2, 0.3]]
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "model": "text-embedding-fixture",
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }


def _usage_rows(db_session, account_id) -> list[ApiUsage]:
    return (
        db_session.query(ApiUsage)
        .filter(
            ApiUsage.account_id == account_id,
            ApiUsage.endpoint == EMBEDDINGS_URL,
        )
        .order_by(ApiUsage.timestamp.desc())
        .all()
    )


@pytest.fixture
def fixture_price():
    """Price the fixture model in litellm's map for the length of one test."""
    register_model(
        {
            FIXTURE_MODEL: {
                "litellm_provider": "openai",
                "mode": "embedding",
                "input_cost_per_token": FIXTURE_INPUT_COST_PER_TOKEN,
                "output_cost_per_token": 0.0,
            }
        }
    )
    try:
        yield FIXTURE_INPUT_COST_PER_TOKEN
    finally:
        model_cost.pop(FIXTURE_MODEL, None)


def test_embeddings_endpoint_returns_vectors_and_records_one_usage_row(
    app, client, db_session, test_user, fixture_price
):
    """Vectors come back and exactly one usage row names account, key, model."""
    ai_model = _create_embedding_model(db_session, test_user.account_id)
    api_key = _runtime_key_auth(app, db_session, test_user)

    with patch(
        LITELLM_EMBEDDING,
        return_value=_upstream_response(
            prompt_tokens=11, vectors=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        ),
    ) as mock_embedding:
        response = client.post(
            EMBEDDINGS_URL,
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": f"openai/{FIXTURE_MODEL}",
                "input": ["first chunk", "second chunk"],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [item["embedding"] for item in body["data"]] == [
        [0.1, 0.2, 0.3],
        [0.4, 0.5, 0.6],
    ]
    assert [item["index"] for item in body["data"]] == [0, 1]
    assert body["model"] == f"openai/{FIXTURE_MODEL}"
    assert body["usage"]["prompt_tokens"] == 11
    assert mock_embedding.call_args.kwargs["input"] == ["first chunk", "second chunk"]

    rows = _usage_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.account_id == test_user.account_id
    assert row.api_key_id == api_key.id
    assert row.ai_model_id == ai_model.id
    assert row.model_alias == f"openai/{FIXTURE_MODEL}"
    assert row.status_code == 200
    assert row.prompt_tokens == 11


def test_recording_payload_keeps_the_shape_and_drops_the_vectors():
    """Events and the interaction index store widths, never the float arrays."""
    summarized = OpenAIGatewayService._embedding_recording_payload(
        {
            "object": "list",
            "model": "openai/embedding",
            "usage": {"prompt_tokens": 4, "completion_tokens": 0, "total_tokens": 4},
            "data": [
                {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]},
                {"object": "embedding", "index": 1, "embedding": [0.4, 0.5]},
            ],
        }
    )

    assert summarized["usage"]["prompt_tokens"] == 4
    assert summarized["embeddings"] == [
        {"object": "embedding", "index": 0, "dimensions": 3},
        {"object": "embedding", "index": 1, "dimensions": 2},
    ]
    assert "0.1" not in str(summarized)


def test_embeddings_usage_cost_matches_the_catalog_price(
    app, client, db_session, test_user, fixture_price
):
    """Recorded cost is the catalog price times the tokens the upstream billed."""
    _create_embedding_model(db_session, test_user.account_id)
    _runtime_key_auth(app, db_session, test_user)

    with patch(LITELLM_EMBEDDING, return_value=_upstream_response(prompt_tokens=1000)):
        response = client.post(
            EMBEDDINGS_URL,
            headers={"Authorization": "Bearer ignored"},
            json={"model": f"openai/{FIXTURE_MODEL}", "input": "price me"},
        )

    assert response.status_code == 200
    rows = _usage_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    assert rows[0].cost_source == "catalog"
    assert rows[0].estimated_cost == pytest.approx(1000 * fixture_price)


def test_embeddings_model_missing_from_the_catalog_is_flagged_unpriced(
    app, client, db_session, test_user
):
    """An unpriced embedding model records the call, not a fictitious $0."""
    assert UNPRICED_MODEL not in model_cost
    _create_embedding_model(
        db_session, test_user.account_id, model_identifier=UNPRICED_MODEL
    )
    _runtime_key_auth(app, db_session, test_user)

    with (
        patch(LITELLM_EMBEDDING, return_value=_upstream_response(prompt_tokens=42)),
        patch("preloop.services.openai_gateway.schedule_price_lookup") as mock_lookup,
        patch("preloop.services.openai_gateway.notify_unpriced_model"),
    ):
        response = client.post(
            EMBEDDINGS_URL,
            headers={"Authorization": "Bearer ignored"},
            json={"model": f"openai/{UNPRICED_MODEL}", "input": "no price for me"},
        )

    assert response.status_code == 200
    rows = _usage_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    assert rows[0].prompt_tokens == 42
    assert rows[0].cost_source == "unpriced"
    assert rows[0].estimated_cost is None
    # The same follow-up a missing completion price gets.
    mock_lookup.assert_called_once()


def _chat_payload(alias: str) -> dict:
    return {"model": alias, "messages": [{"role": "user", "content": "Hello"}]}


def _responses_payload(alias: str) -> dict:
    return {"model": alias, "input": "Hello"}


def _embeddings_payload(alias: str) -> dict:
    return {"model": alias, "input": "Hello"}


GATEWAY_ROUTES = [
    ("/openai/v1/chat/completions", _chat_payload),
    ("/openai/v1/responses", _responses_payload),
    (EMBEDDINGS_URL, _embeddings_payload),
]


@pytest.mark.parametrize("url,payload_for", GATEWAY_ROUTES)
def test_gateway_routes_require_a_bearer_token(
    app, client, db_session, test_user, url, payload_for
):
    """Embeddings authenticate exactly like the routes that preceded it."""
    _create_embedding_model(db_session, test_user.account_id)
    app.dependency_overrides.pop(get_model_gateway_auth_context, None)

    with (
        patch(LITELLM_EMBEDDING) as mock_embedding,
        patch("preloop.services.openai_gateway.litellm.completion") as mock_completion,
    ):
        missing = client.post(url, json=payload_for(f"openai/{FIXTURE_MODEL}"))
        invalid = client.post(
            url,
            headers={"Authorization": "Bearer not-a-real-token"},
            json=payload_for(f"openai/{FIXTURE_MODEL}"),
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    mock_embedding.assert_not_called()
    mock_completion.assert_not_called()


@pytest.mark.parametrize("url,payload_for", GATEWAY_ROUTES)
def test_gateway_routes_do_not_serve_another_accounts_model(
    app, client, db_session, test_user, url, payload_for
):
    """A model owned by another account is invisible on every gateway route."""
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    _create_embedding_model(
        db_session,
        other_account.id,
        model_identifier="other-account-embedding",
        alias="openai/other-account-embedding",
    )
    _runtime_key_auth(app, db_session, test_user)

    with (
        patch(LITELLM_EMBEDDING) as mock_embedding,
        patch("preloop.services.openai_gateway.litellm.completion") as mock_completion,
    ):
        response = client.post(
            url,
            headers={"Authorization": "Bearer ignored"},
            json=payload_for("openai/other-account-embedding"),
        )

    assert response.status_code == 404
    mock_embedding.assert_not_called()
    mock_completion.assert_not_called()
    assert _usage_rows(db_session, other_account.id) == []


def test_embeddings_reject_stream_true(app, client, db_session, test_user):
    """Embeddings have no SSE counterpart, so stream=true is a 400."""
    _create_embedding_model(db_session, test_user.account_id)
    _runtime_key_auth(app, db_session, test_user)

    with patch(LITELLM_EMBEDDING) as mock_embedding:
        response = client.post(
            EMBEDDINGS_URL,
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": f"openai/{FIXTURE_MODEL}",
                "input": "hello",
                "stream": True,
            },
        )

    assert response.status_code == 400
    assert "does not support stream=true" in response.json()["error"]["message"]
    mock_embedding.assert_not_called()
    assert _usage_rows(db_session, test_user.account_id) == []


@pytest.mark.parametrize("empty_input", ["", []])
def test_embeddings_reject_empty_input(app, client, db_session, test_user, empty_input):
    """A missing or empty embeddings input is a 400 before any upstream call."""
    _create_embedding_model(db_session, test_user.account_id)
    _runtime_key_auth(app, db_session, test_user)

    with patch(LITELLM_EMBEDDING) as mock_embedding:
        response = client.post(
            EMBEDDINGS_URL,
            headers={"Authorization": "Bearer ignored"},
            json={"model": f"openai/{FIXTURE_MODEL}", "input": empty_input},
        )

    assert response.status_code == 400
    assert "non-empty string or list" in response.json()["error"]["message"]
    mock_embedding.assert_not_called()
    assert _usage_rows(db_session, test_user.account_id) == []


def test_embeddings_rejected_while_gateway_halted(app, client, db_session, test_user):
    """The embeddings route honours the same kill switch as completions."""
    _create_embedding_model(db_session, test_user.account_id)
    _runtime_key_auth(app, db_session, test_user)
    crud_account_halt.set_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=["gateway"],
        active=True,
        user_id=None,
        reason="runaway agent",
    )
    invalidate_kill_switch_cache(test_user.account_id)
    try:
        with patch(LITELLM_EMBEDDING) as mock_embedding:
            response = client.post(
                EMBEDDINGS_URL,
                headers={"Authorization": "Bearer ignored"},
                json={"model": f"openai/{FIXTURE_MODEL}", "input": "hello"},
            )
    finally:
        crud_account_halt.set_scopes(
            db_session,
            account_id=test_user.account_id,
            scopes=["gateway"],
            active=False,
            user_id=None,
        )
        invalidate_kill_switch_cache(test_user.account_id)

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "preloop_account_halted"
    assert "kill switch" in error["message"].lower()
    mock_embedding.assert_not_called()
    rows = _usage_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    assert rows[0].status_code == 403
    assert rows[0].error_class == "kill_switch"
    assert rows[0].meta_data["endpoint_kind"] == "embeddings"


def _denied_budget_result() -> BudgetCheckResult:
    return BudgetCheckResult(
        account_limit_usd=0.00001,
        account_soft_limit_usd=None,
        account_current_spend_usd=0.0,
        account_estimated_total_usd=1.0,
        flow_limit_usd=None,
        flow_soft_limit_usd=None,
        flow_current_spend_usd=0.0,
        flow_estimated_total_usd=None,
        estimated_request_cost_usd=1.0,
        trial_hosted_model_limit_usd=None,
        trial_hosted_model_current_spend_usd=None,
        trial_hosted_model_estimated_total_usd=None,
        hard_limit_exceeded=True,
        soft_limit_exceeded=False,
        enforcement_reason="account_budget_exceeded",
        pricing_available=True,
    )


def test_embeddings_budget_denial_records_embeddings_kind(
    app, client, db_session, test_user
):
    """A 403 budget denial on embeddings is stamped embeddings, not chat."""
    _create_embedding_model(db_session, test_user.account_id)
    _runtime_key_auth(app, db_session, test_user)

    with (
        patch(LITELLM_EMBEDDING) as mock_embedding,
        patch.object(
            OpenAIGatewayService,
            "_check_budget",
            return_value=_denied_budget_result(),
        ),
    ):
        response = client.post(
            EMBEDDINGS_URL,
            headers={"Authorization": "Bearer ignored"},
            json={
                "model": f"openai/{FIXTURE_MODEL}",
                "input": ["first chunk", "second chunk"],
            },
        )

    assert response.status_code == 403
    body = response.json()
    assert "account monthly limit reached" in body["error"]["message"]
    mock_embedding.assert_not_called()
    rows = _usage_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    assert rows[0].status_code == 403
    assert rows[0].meta_data["endpoint_kind"] == "embeddings"


def test_embeddings_upstream_failure_records_error_row(
    app, client, db_session, test_user
):
    """An upstream embeddings failure is recorded, then re-raised to the client."""
    _create_embedding_model(db_session, test_user.account_id)
    _runtime_key_auth(app, db_session, test_user)

    with patch(
        LITELLM_EMBEDDING,
        side_effect=Exception("upstream exploded"),
    ):
        response = client.post(
            EMBEDDINGS_URL,
            headers={"Authorization": "Bearer ignored"},
            json={"model": f"openai/{FIXTURE_MODEL}", "input": "hello"},
        )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == (
        "Gateway upstream error: upstream exploded"
    )
    rows = _usage_rows(db_session, test_user.account_id)
    assert len(rows) == 1
    assert rows[0].status_code == 502
    assert rows[0].meta_data["endpoint_kind"] == "embeddings"
    assert rows[0].error_class == "upstream_error"


def _opencode_runtime_key(db_session, test_user):
    """Durable OpenCode credential so native parent headers are trusted."""
    api_key, presented_token = crud_api_key.create_runtime_key(
        db_session,
        name="OpenCode Durable Credential",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "credential_kind": "managed_agent_durable",
            "runtime_principal": {
                "type": "opencode",
                "id": "opencode-64fd76044120",
                "name": "opencode",
            },
        },
    )
    return api_key, presented_token


def _create_chat_model(db_session, account_id):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Chat Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "openai/gpt-5",
                    "provider_adapter": "preloop",
                },
                "pricing": {
                    "input_price_per_1k": 0.01,
                    "output_price_per_1k": 0.02,
                },
            },
            "is_default": True,
        },
        account_id=account_id,
    )


def test_embeddings_pass_derived_parent_to_gateway_service(
    app, client, db_session, test_user
):
    """`/v1/embeddings` must pass the same derived parent as chat and responses."""
    api_key, _ = _opencode_runtime_key(db_session, test_user)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=test_user, api_key=api_key)
    )

    for headers, expected_session, expected_parent, expected_explicit in (
        (
            {
                "X-Session-Id": "ses_child",
                "X-Parent-Session-Id": "ses_parent",
            },
            "ses_child",
            "ses_parent",
            False,
        ),
        (
            {
                "X-Session-Id": "ses_child",
                "X-Parent-Session-Id": "ses_parent",
                "X-Preloop-Session-Id": "explicit-run",
            },
            "explicit-run",
            None,
            True,
        ),
    ):
        with patch(
            "preloop.api.endpoints.openai_gateway.OpenAIGatewayService"
        ) as service_cls:
            service_cls.return_value.response_warning = None
            service_cls.return_value.create_embedding.return_value = {
                "object": "list",
                "data": [],
            }
            response = client.post(
                EMBEDDINGS_URL,
                headers={"Authorization": "Bearer ignored", **headers},
                json={"model": f"openai/{FIXTURE_MODEL}", "input": "hello"},
            )

        assert response.status_code == 200
        kwargs = service_cls.call_args.kwargs
        assert kwargs["client_session_id"] == expected_session
        assert kwargs["client_parent_session_id"] == expected_parent
        # Only the explicit Preloop header opts a plain key in; a gated
        # vendor header does not.
        assert kwargs["client_session_id_is_explicit"] is expected_explicit


def test_embedding_first_opencode_subagent_records_parent_over_http(
    app, client, db_session, test_user
):
    """Embedding-first OpenCode subagent keeps X-Parent-Session-Id on the row.

    A later chat turn on the same session must not leave parent_session_id NULL.
    """
    _create_embedding_model(db_session, test_user.account_id)
    _create_chat_model(db_session, test_user.account_id)
    api_key, _ = _opencode_runtime_key(db_session, test_user)
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=test_user, api_key=api_key)
    )
    child_headers = {
        "Authorization": "Bearer ignored",
        "X-Session-Id": "ses_f599dfca1ffe933RP1BLey87Mr",
        "X-Parent-Session-Id": "ses_f599e0060ffe4gLnO1q69ZcNFw",
    }

    with (
        patch(LITELLM_EMBEDDING, return_value=_upstream_response()),
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value={
                "id": "chatcmpl_lineage",
                "created": 1710000000,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "total_tokens": 8,
                },
            },
        ),
    ):
        embedding_response = client.post(
            EMBEDDINGS_URL,
            headers=child_headers,
            json={"model": f"openai/{FIXTURE_MODEL}", "input": "hello"},
        )
        chat_response = client.post(
            "/openai/v1/chat/completions",
            headers=child_headers,
            json={
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert embedding_response.status_code == 200
    assert chat_response.status_code == 200
    embedding_row = _usage_rows(db_session, test_user.account_id)[0]
    child = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(embedding_row.runtime_session_id),
    )
    assert child is not None
    assert child.parent_session_id is not None
    parent = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(child.parent_session_id),
    )
    assert parent is not None
    assert parent.session_source_id.endswith(":ses_f599e0060ffe4gLnO1q69ZcNFw")
    chat_rows = (
        db_session.query(ApiUsage)
        .filter(
            ApiUsage.account_id == test_user.account_id,
            ApiUsage.endpoint == "/openai/v1/chat/completions",
        )
        .all()
    )
    assert len(chat_rows) == 1
    assert str(chat_rows[0].runtime_session_id) == str(child.id)
    db_session.refresh(child)
    assert child.parent_session_id == parent.id
