"""Tests for gateway per-tool attribution (T1) and per-run sessions (T2).

Covers:
  - ``meta_data["tools_meta"]`` per-tool attribution on the usage row.
  - ``X-Preloop-Session-Id`` per-run runtime-session scoping.
  - Integration of the partial-strip ``tool_choice`` sanitize (T5).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from datetime import datetime, timedelta, timezone

from preloop.config import settings
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
    crud_runtime_session,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.services.agent_session_headers import native_session_id_from_headers
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


def test_upstream_credential_type_recorded_on_usage_row(db_session, test_user):
    """T12: the resolved upstream credential type lands in meta_data.

    Doubles as a regression guard that adding the field did not break the
    usage-row write on the hot gateway path.
    """
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
    # The gateway model is provisioned with an api_key credential.
    assert usage.meta_data["upstream_credential_type"] == "api_key"


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


# --------------------------------------------------------------------------
# Claude Code session identity -- a NEW Claude Code conversation must get its
# own runtime session instead of appending onto the previous one.
#
# Claude Code never sends X-Preloop-Session-Id. It stamps its real conversation
# id (the ~/.claude/projects/<slug>/<uuid>.jsonl transcript name) on the
# X-Claude-Code-Session-Id header and inside metadata.user_id, which is a JSON
# *string*. Before the fix, both were ignored, so every run on a machine keyed
# to the durable credential's single principal id and collapsed into one
# eternal session row.
# --------------------------------------------------------------------------


_ANTHROPIC_LITELLM_RESPONSE = {
    "id": "msg_session_identity",
    "created": 1710000000,
    "choices": [
        {
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


def _create_anthropic_gateway_model(db_session, account_id) -> Any:
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Gateway Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "anthropic/claude-sonnet-4-5",
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


def _claude_code_key(db_session, test_user) -> Any:
    """A durable Claude Code credential: one principal id reused for every run."""
    runtime_api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Claude Code Durable Credential",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "credential_kind": "managed_agent_durable",
            "runtime_principal": {
                "type": "claude_code",
                "id": "claude-code-64fd76044120",
                "name": "Claude Code",
            },
        },
    )
    return runtime_api_key


def _claude_code_payload(session_id: Optional[str]) -> Dict[str, Any]:
    """Build the Anthropic payload Claude Code actually sends."""
    payload: Dict[str, Any] = {
        "model": "anthropic/claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 256,
    }
    if session_id is not None:
        payload["metadata"] = {
            "user_id": json.dumps(
                {
                    "device_id": "87f960fb6adfa93494bd7141bcbf3a9489c897fa7c0a9d0ac60f07b6078503db",
                    "account_uuid": "",
                    "session_id": session_id,
                }
            )
        }
    return payload


def _run_message(service: OpenAIGatewayService, payload: Dict[str, Any]) -> Any:
    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_ANTHROPIC_LITELLM_RESPONSE,
    ):
        return service.create_message(payload)


def _anthropic_usage_rows(db_session) -> List[ApiUsage]:
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/anthropic/v1/messages")
        .order_by(ApiUsage.timestamp.asc())
        .all()
    )


def test_claude_code_native_metadata_splits_sessions(db_session, test_user):
    """Two Claude Code conversations must not merge into one runtime session."""
    _create_anthropic_gateway_model(db_session, test_user.account_id)

    api_key = _claude_code_key(db_session, test_user)
    _run_message(
        _service(db_session, test_user, api_key),
        _claude_code_payload("26d2f152-2d10-49e5-a68c-e471d55aadad"),
    )
    _run_message(
        _service(db_session, test_user, api_key),
        _claude_code_payload("5436a7b6-8e38-493b-a172-304a1a000000"),
    )

    rows = _anthropic_usage_rows(db_session)
    session_ids = {r.runtime_session_id for r in rows}
    assert None not in session_ids
    assert len(session_ids) == 2


def test_claude_code_same_conversation_reuses_session(db_session, test_user):
    """Turns of ONE Claude Code conversation stay in one runtime session."""
    _create_anthropic_gateway_model(db_session, test_user.account_id)

    api_key = _claude_code_key(db_session, test_user)
    payload = _claude_code_payload("26d2f152-2d10-49e5-a68c-e471d55aadad")
    _run_message(_service(db_session, test_user, api_key), payload)
    _run_message(_service(db_session, test_user, api_key), payload)

    rows = _anthropic_usage_rows(db_session)
    session_ids = {r.runtime_session_id for r in rows}
    assert None not in session_ids
    assert len(session_ids) == 1


def test_claude_code_session_id_lands_on_runtime_session_row(db_session, test_user):
    """The runtime session is keyed by Claude Code's own conversation id."""
    _create_anthropic_gateway_model(db_session, test_user.account_id)

    session_uuid = "26d2f152-2d10-49e5-a68c-e471d55aadad"
    api_key = _claude_code_key(db_session, test_user)
    _run_message(
        _service(db_session, test_user, api_key), _claude_code_payload(session_uuid)
    )

    row = _anthropic_usage_rows(db_session)[0]
    runtime_session = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(row.runtime_session_id),
    )
    assert runtime_session is not None
    # `<principal id>:<claude code session id>` -- the transcript uuid is what
    # makes this row traceable back to a real Claude Code conversation.
    assert runtime_session.session_source_id.endswith(f":{session_uuid}")


def test_preloop_session_header_wins_over_claude_metadata(db_session, test_user):
    """An explicit X-Preloop-Session-Id is never overridden by the payload."""
    _create_anthropic_gateway_model(db_session, test_user.account_id)

    api_key = _claude_code_key(db_session, test_user)
    _run_message(
        _service(db_session, test_user, api_key, client_session_id="explicit-run"),
        _claude_code_payload("26d2f152-2d10-49e5-a68c-e471d55aadad"),
    )

    row = _anthropic_usage_rows(db_session)[0]
    runtime_session = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(row.runtime_session_id),
    )
    assert runtime_session is not None
    assert runtime_session.session_source_id.endswith(":explicit-run")


def test_claude_code_malformed_metadata_falls_back_without_error(db_session, test_user):
    """Unparseable/oversized/hostile metadata degrades to source keying."""
    _create_anthropic_gateway_model(db_session, test_user.account_id)
    api_key = _claude_code_key(db_session, test_user)

    base = _claude_code_payload(None)
    for metadata in (
        {"user_id": "not-json"},
        {"user_id": json.dumps({"session_id": "bad value!"})},
        {"user_id": json.dumps({"device_id": "d"})},  # no session_id
        {"user_id": json.dumps(["not", "an", "object"])},
        {"user_id": "x" * 5000},  # over the parse cap
        {"user_id": 12345},  # wrong type
        {"user_id": json.dumps({"session_id": "x" * 400})},  # over the id cap
    ):
        _run_message(
            _service(db_session, test_user, api_key), {**base, "metadata": metadata}
        )

    rows = _anthropic_usage_rows(db_session)
    assert len(rows) == 7
    session_ids = {r.runtime_session_id for r in rows}
    assert None not in session_ids
    # No usable session id anywhere -> all collapse to one source-keyed row,
    # exactly the pre-fix behavior. Nothing raised.
    assert len(session_ids) == 1


# ---------------------------------------------------------------------------
# Per-agent native session ids (Codex, OpenCode) and prompt_cache_key.
#
# Same bug class as the Claude Code tests above: a durable managed-agent
# credential has a machine-scoped `runtime_principal.id` that never changes, so
# without a per-conversation signal every conversation on that machine collapses
# onto one runtime_session row that is never ended.
# ---------------------------------------------------------------------------


def _durable_key(db_session, test_user, principal_type: str) -> Any:
    """A durable credential for one agent family: one id reused for every run."""
    runtime_api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name=f"{principal_type} Durable Credential",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "credential_kind": "managed_agent_durable",
            "runtime_principal": {
                "type": principal_type,
                "id": f"{principal_type}-64fd76044120",
                "name": principal_type,
            },
        },
    )
    return runtime_api_key


def _openai_usage_rows(db_session) -> List[ApiUsage]:
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/chat/completions")
        .order_by(ApiUsage.timestamp.asc())
        .all()
    )


def _source_id_for(db_session, test_user, row: ApiUsage) -> str:
    runtime_session = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(row.runtime_session_id),
    )
    assert runtime_session is not None
    return runtime_session.session_source_id


def test_codex_session_header_splits_sessions(db_session, test_user):
    """Codex sends its conversation uuid as `session-id` on every request."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "codex")

    for session_uuid in (
        "26d2f152-2d10-49e5-a68c-e471d55aadad",
        "5436a7b6-8e38-493b-a172-304a1a000000",
    ):
        service = _service(
            db_session,
            test_user,
            api_key,
            client_session_id=native_session_id_from_headers(
                {"session-id": session_uuid},
                auth_context=SimpleNamespace(api_key=api_key),
            ),
        )
        _run_chat(
            service,
            {"model": "openai/gpt-5", "messages": [{"role": "user", "content": "hi"}]},
        )

    rows = _openai_usage_rows(db_session)
    session_ids = {r.runtime_session_id for r in rows}
    assert None not in session_ids
    assert len(session_ids) == 2


def test_codex_thread_id_header_is_read_when_session_id_absent(db_session, test_user):
    """Codex sends the same uuid on `thread-id`; the fix survives either."""
    api_key = _durable_key(db_session, test_user, "codex")

    resolved = native_session_id_from_headers(
        {"thread-id": "26d2f152-2d10-49e5-a68c-e471d55aadad"},
        auth_context=SimpleNamespace(api_key=api_key),
    )

    assert resolved == "26d2f152-2d10-49e5-a68c-e471d55aadad"


def test_opencode_session_header_splits_sessions(db_session, test_user):
    """OpenCode sends `x-session-id` per conversation."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "opencode")

    for session_id in ("ses_8a1f", "ses_9b2e"):
        service = _service(
            db_session,
            test_user,
            api_key,
            client_session_id=native_session_id_from_headers(
                {"x-session-id": session_id},
                auth_context=SimpleNamespace(api_key=api_key),
            ),
        )
        _run_chat(
            service,
            {"model": "openai/gpt-5", "messages": [{"role": "user", "content": "hi"}]},
        )

    rows = _openai_usage_rows(db_session)
    assert len({r.runtime_session_id for r in rows}) == 2


def test_native_headers_are_ignored_for_other_principal_types(db_session, test_user):
    """`Session-Id` is a GENERIC name any proxy or CDN may stamp.

    Session boundaries can never be re-derived after the fact, so a wrong
    boundary is permanent. Reading these headers is therefore gated on the
    credential's own principal type.
    """
    codex_key = _durable_key(db_session, test_user, "codex")
    other_key = _durable_key(db_session, test_user, "gemini_cli")

    assert (
        native_session_id_from_headers(
            {"session-id": "abc"}, auth_context=SimpleNamespace(api_key=codex_key)
        )
        == "abc"
    )
    # Same header, different agent: not trusted.
    assert (
        native_session_id_from_headers(
            {"session-id": "abc"}, auth_context=SimpleNamespace(api_key=other_key)
        )
        is None
    )
    # No credential at all: nothing to gate on, so nothing is trusted.
    assert (
        native_session_id_from_headers(
            {"session-id": "abc"}, auth_context=SimpleNamespace(api_key=None)
        )
        is None
    )
    # OpenCode's header must not be honoured for a codex credential either.
    assert (
        native_session_id_from_headers(
            {"x-session-id": "abc"}, auth_context=SimpleNamespace(api_key=codex_key)
        )
        is None
    )


def test_prompt_cache_key_splits_sessions(db_session, test_user):
    """`prompt_cache_key` is OpenAI's per-conversation field (replaces `user`)."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "custom")

    for cache_key in ("conv-alpha", "conv-beta"):
        _run_chat(
            _service(db_session, test_user, api_key),
            {
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "hi"}],
                "prompt_cache_key": cache_key,
            },
        )

    rows = _openai_usage_rows(db_session)
    assert len({r.runtime_session_id for r in rows}) == 2
    assert _source_id_for(db_session, test_user, rows[0]).endswith(":conv-alpha")


def test_preloop_header_wins_over_prompt_cache_key(db_session, test_user):
    """Explicit Preloop header outranks the body's cache key."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "custom")

    _run_chat(
        _service(db_session, test_user, api_key, client_session_id="explicit-run"),
        {
            "model": "openai/gpt-5",
            "messages": [{"role": "user", "content": "hi"}],
            "prompt_cache_key": "conv-alpha",
        },
    )

    row = _openai_usage_rows(db_session)[0]
    assert _source_id_for(db_session, test_user, row).endswith(":explicit-run")


def test_malformed_prompt_cache_key_falls_back_without_error(db_session, test_user):
    """A hostile or unusable cache key degrades to source keying, never 500s."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "custom")

    for cache_key in ("bad value!", "x" * 400, 12345, None, {"nested": "object"}):
        _run_chat(
            _service(db_session, test_user, api_key),
            {
                "model": "openai/gpt-5",
                "messages": [{"role": "user", "content": "hi"}],
                "prompt_cache_key": cache_key,
            },
        )

    rows = _openai_usage_rows(db_session)
    assert len(rows) == 5
    # All fell back to the same source-keyed session; none errored.
    assert len({r.runtime_session_id for r in rows}) == 1


# ---------------------------------------------------------------------------
# Inactivity closer: the honest fallback for signal-less sources.
#
# Gemini CLI, Hermes and OpenClaw's Anthropic transport put NO conversation id
# on the wire, so only the clock can bound their sessions. This is a safety net
# only: a native id always wins.
# ---------------------------------------------------------------------------


def _age_runtime_session(db_session, row: ApiUsage, *, minutes: int) -> Any:
    """Backdate a session's activity so it looks idle."""
    runtime_session = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(row.account_id),
        runtime_session_id=str(row.runtime_session_id),
    )
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes)
    runtime_session.last_activity_at = stale
    runtime_session.started_at = stale
    db_session.add(runtime_session)
    db_session.flush()
    return runtime_session


def test_idle_signal_less_session_rolls_to_a_new_generation(db_session, test_user):
    """A signal-less agent's next conversation must not append to a stale row."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "gemini_cli")
    payload = {"model": "openai/gpt-5", "messages": [{"role": "user", "content": "hi"}]}

    _run_chat(_service(db_session, test_user, api_key), payload)
    first = _openai_usage_rows(db_session)[0]
    stale_session = _age_runtime_session(db_session, first, minutes=10_000)

    _run_chat(_service(db_session, test_user, api_key), payload)

    rows = _openai_usage_rows(db_session)
    assert len({r.runtime_session_id for r in rows}) == 2
    # The stale row is closed AT ITS OWN LAST ACTIVITY, not at "now": history
    # must not be rewritten to claim the session ran until this moment.
    db_session.refresh(stale_session)
    assert stale_session.ended_at is not None
    assert stale_session.ended_at == stale_session.last_activity_at
    # The new generation is distinguishable as closer-minted.
    assert ":idle-" in _source_id_for(db_session, test_user, rows[1])


def test_active_signal_less_session_is_not_split(db_session, test_user):
    """Inside the window, consecutive turns stay in one session."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "gemini_cli")
    payload = {"model": "openai/gpt-5", "messages": [{"role": "user", "content": "hi"}]}

    _run_chat(_service(db_session, test_user, api_key), payload)
    _run_chat(_service(db_session, test_user, api_key), payload)

    rows = _openai_usage_rows(db_session)
    assert len({r.runtime_session_id for r in rows}) == 1


def test_native_session_id_is_never_split_by_idleness(db_session, test_user):
    """A natively identified conversation is immune to the closer.

    Resuming a real Codex conversation after a week is still that conversation;
    only the agent's own id decides, never the clock.
    """
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "custom")
    payload = {
        "model": "openai/gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "prompt_cache_key": "conv-alpha",
    }

    _run_chat(_service(db_session, test_user, api_key), payload)
    _age_runtime_session(db_session, _openai_usage_rows(db_session)[0], minutes=10_000)
    _run_chat(_service(db_session, test_user, api_key), payload)

    rows = _openai_usage_rows(db_session)
    assert len({r.runtime_session_id for r in rows}) == 1


def test_idle_closer_can_be_disabled(db_session, test_user):
    """`runtime_session_idle_timeout_minutes = 0` restores the old behavior."""
    _create_gateway_model(db_session, test_user.account_id)
    api_key = _durable_key(db_session, test_user, "gemini_cli")
    payload = {"model": "openai/gpt-5", "messages": [{"role": "user", "content": "hi"}]}

    _run_chat(_service(db_session, test_user, api_key), payload)
    _age_runtime_session(db_session, _openai_usage_rows(db_session)[0], minutes=10_000)

    with patch.object(settings, "runtime_session_idle_timeout_minutes", 0):
        _run_chat(_service(db_session, test_user, api_key), payload)

    rows = _openai_usage_rows(db_session)
    assert len({r.runtime_session_id for r in rows}) == 1
