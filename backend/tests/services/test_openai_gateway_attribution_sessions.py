"""Tests for gateway per-tool attribution (T1) and per-run sessions (T2).

Covers:
  - ``meta_data["tools_meta"]`` per-tool attribution on the usage row.
  - ``X-Preloop-Session-Id`` per-run runtime-session scoping.
  - Integration of the partial-strip ``tool_choice`` sanitize (T5).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

from datetime import datetime, timezone

from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
    crud_runtime_session,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.subject_governance import (
    SUBJECT_TYPE_API_KEYS,
    set_subject_governance,
)


_LITELLM_RESPONSE = {
    "id": "chatcmpl_attr",
    "created": 1710000000,
    "choices": [
        {
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


def _create_gateway_model(db_session, account_id) -> Any:
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
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


def _runtime_key(db_session, test_user) -> Any:
    context_data: Dict[str, Any] = {
        "runtime_principal": {
            "type": "custom",
            "id": "static-agent-cred",
            "name": "Static Agent",
        },
    }
    runtime_api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Gateway Runtime Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data=context_data,
    )
    return runtime_api_key


def _service(
    db_session,
    test_user,
    api_key,
    *,
    client_session_id: Optional[str] = None,
) -> OpenAIGatewayService:
    return OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="t", user=test_user, api_key=api_key),
        client_session_id=client_session_id,
    )


def _latest_usage(db_session) -> ApiUsage:
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/chat/completions")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )


def _run_chat(service: OpenAIGatewayService, payload: Dict[str, Any]) -> Any:
    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_LITELLM_RESPONSE,
    ):
        return service.create_chat_completion(payload)


def _primary_completion_kwargs(mock_completion: Any) -> Dict[str, Any]:
    """Return kwargs of the request-carrying litellm call.

    A successful gateway request may trigger a second ``_call_litellm`` for the
    runtime-session summary, which would clobber ``call_args``. Pick the call
    that actually carries our request tools.
    """
    for call in mock_completion.call_args_list:
        if call.kwargs.get("tools"):
            return call.kwargs
    return mock_completion.call_args_list[0].kwargs


# --------------------------------------------------------------------------
# T1 -- tools_meta attribution
# --------------------------------------------------------------------------


def test_tools_meta_records_names_and_nonzero_estimates(db_session, test_user):
    model = _create_gateway_model(db_session, test_user.account_id)
    api_key = _runtime_key(db_session, test_user)
    service = _service(db_session, test_user, api_key)

    _run_chat(
        service,
        {
            "model": "openai/gpt-5",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "search",
                        "description": "search the web",
                        "parameters": {"type": "object"},
                    },
                },
                {"type": "function", "function": {"name": "read_file"}},
            ],
        },
    )

    usage = _latest_usage(db_session)
    assert usage is not None
    tools_meta = usage.meta_data["tools_meta"]
    assert {t["name"] for t in tools_meta} == {"search", "read_file"}
    for entry in tools_meta:
        assert entry["source"] == "payload"
        assert entry["schema_tokens_estimate"] > 0
        assert entry["stripped"] is False
    assert usage.ai_model_id == model.id


def test_no_tools_means_no_tools_meta(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _runtime_key(db_session, test_user)
    service = _service(db_session, test_user, api_key)

    _run_chat(
        service,
        {
            "model": "openai/gpt-5",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    usage = _latest_usage(db_session)
    assert usage is not None
    assert usage.meta_data["tools_meta"] is None


def test_tools_meta_marks_stripped_tool(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _runtime_key(db_session, test_user)

    account = crud_account.get(db_session, id=test_user.account_id)
    account.meta_data = set_subject_governance(
        account.meta_data or {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id=str(api_key.id),
        config={"tool_enabled_overrides": {"search": False}},
    )
    db_session.add(account)
    db_session.commit()

    service = _service(db_session, test_user, api_key)
    _run_chat(
        service,
        {
            "model": "openai/gpt-5",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"type": "function", "function": {"name": "search"}},
                {"type": "function", "function": {"name": "read_file"}},
            ],
        },
    )

    usage = _latest_usage(db_session)
    tools_meta = {t["name"]: t for t in usage.meta_data["tools_meta"]}
    assert tools_meta["search"]["stripped"] is True
    assert tools_meta["read_file"]["stripped"] is False


def test_malformed_tool_is_skipped_but_request_logged(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _runtime_key(db_session, test_user)
    service = _service(db_session, test_user, api_key)

    _run_chat(
        service,
        {
            "model": "openai/gpt-5",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"type": "function", "function": {}},  # no name -> skipped
                {"type": "function", "function": {"name": "ok_tool"}},
            ],
        },
    )

    usage = _latest_usage(db_session)
    assert usage is not None
    names = [t["name"] for t in usage.meta_data["tools_meta"]]
    assert names == ["ok_tool"]


# --------------------------------------------------------------------------
# T2 -- per-run runtime sessions via X-Preloop-Session-Id
# --------------------------------------------------------------------------


def _usage_rows(db_session) -> List[ApiUsage]:
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/chat/completions")
        .order_by(ApiUsage.timestamp.asc())
        .all()
    )


def test_distinct_session_headers_create_distinct_sessions(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    payload = {
        "model": "openai/gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
    }

    api_key_a = _runtime_key(db_session, test_user)
    _run_chat(
        _service(db_session, test_user, api_key_a, client_session_id="run-a"), payload
    )
    api_key_b = _runtime_key(db_session, test_user)
    _run_chat(
        _service(db_session, test_user, api_key_b, client_session_id="run-b"), payload
    )

    rows = _usage_rows(db_session)
    session_ids = {r.runtime_session_id for r in rows}
    assert None not in session_ids
    assert len(session_ids) == 2


def test_same_session_header_reuses_session(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    payload = {
        "model": "openai/gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
    }

    api_key_a = _runtime_key(db_session, test_user)
    _run_chat(
        _service(db_session, test_user, api_key_a, client_session_id="run-x"), payload
    )
    api_key_b = _runtime_key(db_session, test_user)
    _run_chat(
        _service(db_session, test_user, api_key_b, client_session_id="run-x"), payload
    )

    rows = _usage_rows(db_session)
    session_ids = {r.runtime_session_id for r in rows}
    assert None not in session_ids
    assert len(session_ids) == 1


def test_no_header_keeps_source_keyed_session(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    payload = {
        "model": "openai/gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
    }

    api_key_a = _runtime_key(db_session, test_user)
    _run_chat(_service(db_session, test_user, api_key_a), payload)
    api_key_b = _runtime_key(db_session, test_user)
    _run_chat(_service(db_session, test_user, api_key_b), payload)

    rows = _usage_rows(db_session)
    session_ids = {r.runtime_session_id for r in rows}
    assert None not in session_ids
    # Same source id, no header -> collapses to a single session row.
    assert len(session_ids) == 1


def test_malformed_header_falls_back_without_error(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    payload = {
        "model": "openai/gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
    }

    # Invalid charset and an over-long value both ignored -> source keyed.
    api_key_a = _runtime_key(db_session, test_user)
    _run_chat(
        _service(db_session, test_user, api_key_a, client_session_id="bad value!"),
        payload,
    )
    api_key_b = _runtime_key(db_session, test_user)
    _run_chat(
        _service(db_session, test_user, api_key_b, client_session_id="x" * 5000),
        payload,
    )

    rows = _usage_rows(db_session)
    session_ids = {r.runtime_session_id for r in rows}
    assert None not in session_ids
    assert len(session_ids) == 1


# --------------------------------------------------------------------------
# #2 deep fix -- durable-credential traffic attributes to the current per-run
# session, not a collapsed base-principal session.
# --------------------------------------------------------------------------


def _runtime_key_for_principal(
    db_session, test_user, principal_type: str, principal_id: str
) -> Any:
    """A durable credential: a runtime_principal but no per-run id and no
    explicit runtime_session_id (mirrors a managed-agent durable credential)."""
    context_data: Dict[str, Any] = {
        "runtime_principal": {
            "type": principal_type,
            "id": principal_id,
            "name": principal_id,
        },
    }
    runtime_api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name=f"Durable {principal_id}",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data=context_data,
    )
    return runtime_api_key


def test_durable_credential_attributes_to_open_per_run_session(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    now = datetime.now(timezone.utc)
    # The runtime lifecycle (session-token mint) already created a per-run row.
    per_run = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="hermes",
        session_source_id="hermes-test-run1",
        runtime_principal_type="hermes",
        runtime_principal_id="hermes-test",
        runtime_principal_name="Hermes",
        started_at=now,
        last_activity_at=now,
        reopen_if_ended=True,
    )
    db_session.commit()

    api_key = _runtime_key_for_principal(db_session, test_user, "hermes", "hermes-test")
    _run_chat(
        _service(db_session, test_user, api_key),
        {"model": "openai/gpt-5", "messages": [{"role": "user", "content": "hi"}]},
    )

    session_ids = {str(r.runtime_session_id) for r in _usage_rows(db_session)}
    # Usage lands on the existing per-run session, not a new base session.
    assert session_ids == {str(per_run.id)}


def test_durable_credential_without_per_run_falls_back_to_source(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _runtime_key_for_principal(
        db_session, test_user, "openclaw", "openclaw-test"
    )
    _run_chat(
        _service(db_session, test_user, api_key),
        {"model": "openai/gpt-5", "messages": [{"role": "user", "content": "hi"}]},
    )

    # No per-run session existed, so attribution falls back to source keying:
    # a session whose source id equals the base principal id (unchanged path).
    base = crud_runtime_session.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-test",
    )
    assert base is not None
    session_ids = {str(r.runtime_session_id) for r in _usage_rows(db_session)}
    assert session_ids == {str(base.id)}


# --------------------------------------------------------------------------
# T5 -- partial-strip tool_choice sanitize, end to end
# --------------------------------------------------------------------------


def test_partial_strip_sanitizes_tool_choice_to_auto(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _runtime_key(db_session, test_user)

    account = crud_account.get(db_session, id=test_user.account_id)
    account.meta_data = set_subject_governance(
        account.meta_data or {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id=str(api_key.id),
        config={"tool_enabled_overrides": {"search": False}},
    )
    db_session.add(account)
    db_session.commit()

    service = _service(db_session, test_user, api_key)
    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_LITELLM_RESPONSE,
    ) as mock_completion:
        service.create_chat_completion(
            {
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {"type": "function", "function": {"name": "search"}},
                    {"type": "function", "function": {"name": "read_file"}},
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "search"},
                },
            }
        )

    kwargs = _primary_completion_kwargs(mock_completion)
    # search was stripped, so the dangling tool_choice falls back to "auto".
    assert kwargs["tool_choice"] == "auto"
    kept_names = [t["function"]["name"] for t in kwargs["tools"]]
    assert kept_names == ["read_file"]


def test_partial_strip_keeps_tool_choice_for_kept_tool(db_session, test_user):
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _runtime_key(db_session, test_user)

    account = crud_account.get(db_session, id=test_user.account_id)
    account.meta_data = set_subject_governance(
        account.meta_data or {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id=str(api_key.id),
        config={"tool_enabled_overrides": {"search": False}},
    )
    db_session.add(account)
    db_session.commit()

    service = _service(db_session, test_user, api_key)
    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_LITELLM_RESPONSE,
    ) as mock_completion:
        service.create_chat_completion(
            {
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {"type": "function", "function": {"name": "search"}},
                    {"type": "function", "function": {"name": "read_file"}},
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "read_file"},
                },
            }
        )

    kwargs = _primary_completion_kwargs(mock_completion)
    # read_file was kept, so its tool_choice is preserved unchanged.
    assert kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "read_file"},
    }


def test_optimize_request_context_skips_repeated_account_fetch(
    db_session, test_user
) -> None:
    """Accounts without governance should not query the DB on every request."""
    from preloop.services.account_governance_cache import clear_account_governance_cache

    clear_account_governance_cache()
    api_key = _runtime_key(db_session, test_user)
    service = _service(db_session, test_user, api_key)
    messages = [{"role": "user", "content": "hi"}]
    payload: Dict[str, Any] = {"messages": messages}

    with patch(
        "preloop.services.account_governance_cache.crud_account.get",
        wraps=crud_account.get,
    ) as get_mock:
        service._optimize_request_context(messages=messages, payload=payload)
        service._optimize_request_context(messages=messages, payload=payload)
        assert get_mock.call_count == 1
