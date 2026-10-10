"""Client-facing usage keeps the upstream cache breakdown.

Reported behaviour: through Preloop, every ``response.completed`` usage object
on the Codex OAuth path lacked ``input_tokens_details.cached_tokens`` while
the direct upstream carried it, and indexed gateway records read
``cache_read_tokens: 0``. These tests drive a mocked Codex upstream (real SSE
bytes through ``urlopen``) and pin:

* Responses API (streamed and not): the upstream ``usage`` object reaches the
  client verbatim, details and extras included;
* Chat Completions over Codex: ``prompt_tokens_details.cached_tokens`` survives;
* chat-bridged providers: ``prompt_tokens_details`` is renamed, not dropped;
* the ledger writer stores the cached count from a Codex-shaped usage;
* indexed search rows expose the cache split, and "unknown" is not "zero".
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from preloop.models.crud.api_usage import cache_split_for_usage_row
from preloop.schemas.gateway_usage import GatewayTokenUsage
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService

#: The usage object a Codex upstream sends on ``response.completed`` (shape of
#: a mid-session call, plus a hypothetical provider extra).
UPSTREAM_USAGE: Dict[str, Any] = {
    "input_tokens": 5859,
    "input_tokens_details": {"cached_tokens": 4864},
    "output_tokens": 61,
    "output_tokens_details": {"reasoning_tokens": 22},
    "total_tokens": 5920,
    "x_provider_extra": {"tier": "default"},
}


def _sse_bytes(usage: Dict[str, Any]) -> bytes:
    events = [
        {
            "type": "response.output_item.added",
            "item": {"id": "msg_1", "type": "message", "role": "assistant"},
        },
        {"type": "response.output_text.done", "item_id": "msg_1", "text": "OK"},
        {
            "type": "response.completed",
            "response": {"id": "resp_1", "usage": usage},
        },
    ]
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


class _FakeUpstream:
    def __init__(self, body: bytes):
        self.headers: Dict[str, str] = {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self._body.splitlines(keepends=True))


def _service() -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(MagicMock(), auth_context)


def _codex_model() -> SimpleNamespace:
    return SimpleNamespace(
        id="model-1",
        provider_name="openai-codex",
        model_identifier="gpt-codex-example",
        api_endpoint="https://chatgpt.com/backend-api/codex",
        meta_data={},
    )


def _run(service: OpenAIGatewayService, fn, payload: Dict[str, Any], usage=None):
    """Call ``fn(payload)`` against a mocked Codex upstream, recorder patched."""
    body = _sse_bytes(UPSTREAM_USAGE if usage is None else usage)
    credentials = SimpleNamespace(value="oauth", payload={"account_id": "acct-1"})
    with (
        patch.object(service, "_resolve_requested_model", return_value=_codex_model()),
        patch.object(service, "_check_budget", return_value=None),
        patch.object(service, "_record_gateway_request") as record,
        patch.object(service, "_emit_gateway_request_started"),
        patch.object(
            service, "_resolve_openai_codex_credentials", return_value=credentials
        ),
        patch(
            "preloop.services.openai_gateway.urllib_request.urlopen",
            side_effect=lambda req, timeout=None: _FakeUpstream(body),
        ),
        patch(
            "preloop.services.hosted_spend_guard.guard_unmetered_hosted_call",
            return_value=None,
        ),
    ):
        result = fn(payload)
        if not isinstance(result, dict):
            result = list(result)
        service.flush_deferred_stream_record()
    return result, record


def _sse_payloads(events: List[str]) -> List[Any]:
    out = []
    for event in events:
        for line in event.splitlines():
            if line.startswith("data: "):
                data = line[len("data: ") :]
                out.append(data if data == "[DONE]" else json.loads(data))
    return out


_RESPONSES_PAYLOAD = {"model": "openai/gpt-codex-example", "input": "Hi"}
_CHAT_PAYLOAD = {
    "model": "openai/gpt-codex-example",
    "messages": [{"role": "user", "content": "Hi"}],
}


def test_codex_responses_stream_completed_usage_is_upstream_verbatim():
    service = _service()
    events, record = _run(
        service, service.stream_response, {**_RESPONSES_PAYLOAD, "stream": True}
    )
    completed = next(
        p
        for p in _sse_payloads(events)
        if isinstance(p, dict) and p.get("type") == "response.completed"
    )
    usage = completed["response"]["usage"]
    assert json.dumps(usage, sort_keys=True) == json.dumps(
        UPSTREAM_USAGE, sort_keys=True
    )
    # The recorder still sees the raw upstream usage for the ledger.
    upstream = record.call_args.kwargs["upstream_response"]
    assert upstream["usage"] == UPSTREAM_USAGE


def test_codex_responses_non_stream_usage_is_upstream_verbatim():
    service = _service()
    response, _ = _run(service, service.create_response, dict(_RESPONSES_PAYLOAD))
    assert json.dumps(response["usage"], sort_keys=True) == json.dumps(
        UPSTREAM_USAGE, sort_keys=True
    )


def test_codex_responses_fills_totals_only_when_upstream_omits_them():
    service = _service()
    sparse = {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 8}}
    response, _ = _run(
        service, service.create_response, dict(_RESPONSES_PAYLOAD), usage=sparse
    )
    assert response["usage"] == {
        "input_tokens": 10,
        "input_tokens_details": {"cached_tokens": 8},
        "output_tokens": 0,
        "total_tokens": 10,
    }


def test_codex_chat_completion_non_stream_keeps_cached_tokens():
    service = _service()
    response, _ = _run(service, service.create_chat_completion, dict(_CHAT_PAYLOAD))
    usage = response["usage"]
    assert usage["prompt_tokens"] == 5859
    assert usage["completion_tokens"] == 61
    assert usage["prompt_tokens_details"] == {"cached_tokens": 4864}
    assert usage["completion_tokens_details"] == {"reasoning_tokens": 22}


def test_codex_chat_completion_stream_keeps_cached_tokens():
    service = _service()
    events, _ = _run(
        service, service.stream_chat_completion, {**_CHAT_PAYLOAD, "stream": True}
    )
    final = next(
        p
        for p in _sse_payloads(events)
        if isinstance(p, dict) and p.get("usage") and p["usage"].get("total_tokens")
    )
    assert final["usage"]["prompt_tokens"] == 5859
    assert final["usage"]["prompt_tokens_details"] == {"cached_tokens": 4864}


def test_chat_bridged_provider_renames_details_to_responses_shape():
    """A litellm/chat-completions provider answering a Responses request."""
    chat_usage = {
        "prompt_tokens": 100,
        "completion_tokens": 7,
        "total_tokens": 107,
        "prompt_tokens_details": {"cached_tokens": 64, "audio_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 3},
        "cache_creation_input_tokens": 5,
    }
    normalized = OpenAIGatewayService._normalize_usage(
        chat_usage,
        prompt_key="prompt_tokens",
        completion_key="completion_tokens",
        output_names=("completion_tokens", "output_tokens"),
    )
    usage = OpenAIGatewayService._responses_api_usage(chat_usage, normalized)
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 7,
        "total_tokens": 107,
        "input_tokens_details": {"cached_tokens": 64, "audio_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 3},
        "cache_creation_input_tokens": 5,
    }


def test_responses_usage_without_upstream_usage_is_plain_totals():
    normalized = OpenAIGatewayService._normalize_usage(
        None, prompt_key="prompt_tokens", completion_key="completion_tokens"
    )
    assert OpenAIGatewayService._responses_api_usage(None, normalized) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def _ledger_token_details(record: MagicMock) -> Dict[str, Any]:
    """What ``_record_gateway_request_inner`` stores in the cache columns.

    The writer prefers ``upstream_response["usage"]`` over the client payload
    and feeds it to ``_extract_token_details``; this mirrors that selection
    without needing a full model row and database.
    """
    kwargs = record.call_args.kwargs
    upstream = kwargs["upstream_response"] or {}
    usage_details = upstream.get("usage")
    if not isinstance(usage_details, dict):
        usage_details = (kwargs["response_payload"] or {}).get("usage") or {}
    return OpenAIGatewayService._extract_token_details(usage_details)


def test_ledger_writer_stores_cached_tokens_from_codex_shaped_usage():
    service = _service()
    _, record = _run(
        service, service.stream_response, {**_RESPONSES_PAYLOAD, "stream": True}
    )
    details = _ledger_token_details(record)
    assert details["cache_read_tokens"] == 4864
    assert details["reasoning_tokens"] == 22


def test_ledger_writer_stores_null_not_zero_when_upstream_has_no_detail():
    service = _service()
    _, record = _run(
        service,
        service.stream_response,
        {**_RESPONSES_PAYLOAD, "stream": True},
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    )
    details = _ledger_token_details(record)
    assert details["cache_read_tokens"] is None
    assert details["cache_creation_tokens"] is None


def test_search_row_cache_split_reports_upstream_detail():
    row = SimpleNamespace(
        prompt_tokens=5859, cache_read_tokens=4864, cache_creation_tokens=None
    )
    split = cache_split_for_usage_row(row)
    assert split == {
        "cache_read_tokens": 4864,
        "cache_write_tokens": 0,
        "uncached_input_tokens": 995,
        "cache_detail_source": "upstream",
    }
    usage = GatewayTokenUsage.from_row({"prompt_tokens": 5859, **split})
    assert usage.cache_read_tokens == 4864
    assert usage.cache_hit_ratio == round(4864 / 5859, 4)


def test_search_row_cache_split_marks_unknown_as_absent_not_zero_hit():
    row = SimpleNamespace(
        prompt_tokens=5859, cache_read_tokens=None, cache_creation_tokens=None
    )
    split = cache_split_for_usage_row(row)
    assert split["cache_detail_source"] == "absent"
    usage = GatewayTokenUsage.from_row({"prompt_tokens": 5859, **split})
    # Unknown, not "0% cached".
    assert usage.cache_hit_ratio is None


def test_search_row_explicit_zero_is_a_reported_miss():
    row = SimpleNamespace(
        prompt_tokens=100, cache_read_tokens=0, cache_creation_tokens=None
    )
    split = cache_split_for_usage_row(row)
    assert split["cache_detail_source"] == "upstream"
    assert (
        GatewayTokenUsage.from_row({"prompt_tokens": 100, **split}).cache_hit_ratio
        == 0.0
    )
