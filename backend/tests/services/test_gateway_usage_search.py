"""Tests for gateway interaction search corpus helpers."""

import pytest
from datetime import timedelta
import hashlib
from unittest.mock import patch

from preloop.config import settings
from preloop.models.crud import crud_api_usage, crud_gateway_usage_search_document
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.gateway_usage_search_document import (
    GatewayUsageSearchDocument,
)
from preloop.services.gateway_usage_search import GatewayUsageSearchService


def test_build_searchable_text_normalizes_and_redacts_gateway_payloads():
    """Gateway search text should normalize whitespace and redact secrets."""
    usage = ApiUsage(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.25,
        provider_name="openai",
        model_alias="openai/gpt-5",
        runtime_principal_type="flow_execution",
        runtime_principal_name="Gateway Flow",
        meta_data={
            "requested_model": "openai/gpt-5",
            "gateway_provider": "preloop",
            "endpoint_kind": "responses",
            "finish_reason": "stop",
            "error_detail": None,
        },
    )

    text = GatewayUsageSearchService().build_searchable_text(
        usage=usage,
        request_payload={
            "instructions": "  summarize\nthis request  ",
            "input": "hello   world",
            "api_key": "sk-secret",
            "messages": [{"role": "user", "content": "hi there"}],
            "nested": {"session_token": "abc123", "temperature": 0.2},
        },
        response_payload={
            "output_text": "final  answer",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "done now"}],
                }
            ],
        },
    )

    assert "kind: gateway_interaction" in text
    assert "provider_name: openai" in text
    assert "requested_model: openai/gpt-5" in text
    assert "request.instructions: summarize this request" in text
    assert "request.input: hello world" in text
    assert "request.messages.0.content: hi there" in text
    assert "request.nested.temperature: 0.2" in text
    assert "response.output_text: final answer" in text
    assert "response.output.0.content.0.text: done now" in text
    assert "request.api_key: [redacted]" in text
    assert "request.nested.session_token: [redacted]" in text
    assert "sk-secret" not in text
    assert "abc123" not in text


def test_newest_first_keeps_the_latest_turn_and_reply_under_the_ceiling():
    """A long history fills the ceiling with its oldest turns only in request order.

    Newest first, the reply and the turn a call added come before the history
    that earlier calls already indexed, and each turn keeps its own index.
    """
    usage = ApiUsage(
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        meta_data={},
    )
    messages = [
        {"role": "user", "content": f"turn {index} " + "history " * 200}
        for index in range(40)
    ]
    messages.append({"role": "user", "content": "latest turn lumenwick"})
    request = {"model": "gpt-5", "messages": messages}
    response = {"choices": [{"message": {"content": "reply brambleton"}}]}
    service = GatewayUsageSearchService()

    in_order = service.build_searchable_text(
        usage=usage, request_payload=request, response_payload=response
    )
    newest = service.build_searchable_text(
        usage=usage,
        request_payload=request,
        response_payload=response,
        newest_first=True,
    )

    assert "lumenwick" not in in_order
    assert "brambleton" not in in_order
    assert "request.messages.40.content: latest turn lumenwick" in newest
    assert "response.choices.0.message.content: reply brambleton" in newest
    assert newest.index("brambleton") < newest.index("lumenwick")
    assert newest.index("messages.40.") < newest.index("messages.39.")
    assert "request.messages.0." not in newest


@pytest.mark.parametrize(
    ("endpoint_kind", "request_payload", "prefix", "reversed_expected"),
    [
        (
            "responses",
            {"input": [{"role": "user", "content": f"turn {i}"} for i in range(3)]},
            "request.input",
            True,
        ),
        (
            "gemini",
            {
                "contents": [
                    {"role": "user", "parts": [{"text": f"turn {i}"}]} for i in range(3)
                ]
            },
            "request.contents",
            True,
        ),
        (
            "embeddings",
            {"input": ["first text", "second text", "third text"]},
            "request.input",
            False,
        ),
    ],
)
def test_newest_first_reverses_only_conversation_shapes(
    endpoint_kind: str,
    request_payload: dict,
    prefix: str,
    reversed_expected: bool,
) -> None:
    """Turn histories walk newest first; an embeddings batch keeps its order."""
    usage = ApiUsage(
        endpoint="/v1/x",
        method="POST",
        status_code=200,
        duration=0.1,
        meta_data={"endpoint_kind": endpoint_kind},
    )
    text = GatewayUsageSearchService().build_searchable_text(
        usage=usage,
        request_payload=request_payload,
        response_payload=None,
        newest_first=True,
    )

    first = text.index(f"{prefix}.0")
    last = text.index(f"{prefix}.2")
    assert (last < first) is reversed_expected


def test_index_interaction_upserts_gateway_search_document(db_session, test_user):
    """Indexing should create one corpus row per API usage and update in place."""
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        meta_data={"requested_model": "openai/gpt-5", "endpoint_kind": "responses"},
    )

    service = GatewayUsageSearchService(db_session)
    created = service.index_interaction(
        usage=usage,
        request_payload={"input": "first prompt"},
        response_payload={"output_text": "first answer"},
    )

    assert isinstance(created, GatewayUsageSearchDocument)
    assert created.api_usage_id == usage.id
    assert created.meta_data == {
        "source": "gateway_interaction",
        "endpoint": "/openai/v1/responses",
        "method": "POST",
        "status_code": 200,
        "provider_name": "openai",
        "model_alias": "openai/gpt-5",
        "request_payload_present": True,
        "response_payload_present": True,
        "request_fingerprint": None,
        "gateway_attempt": None,
        "is_retry": None,
        "retry_of_api_usage_id": None,
        "error_detail": None,
        "upstream_request_id": None,
    }
    assert (
        created.content_hash
        == hashlib.sha256(created.searchable_text.encode("utf-8")).hexdigest()
    )
    assert (
        crud_gateway_usage_search_document.get_by_api_usage_id(
            db_session, api_usage_id=str(usage.id)
        ).id
        == created.id
    )
    original_hash = created.content_hash

    updated = service.index_interaction(
        usage=usage,
        request_payload={"input": "first prompt"},
        response_payload={"output_text": "updated answer"},
    )

    assert updated.id == created.id
    assert updated.content_hash != original_hash
    assert "updated answer" in updated.searchable_text


def test_auto_index_interaction_is_opt_in(db_session, test_user):
    """Automatic indexing should stay disabled until explicitly enabled."""
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
    )

    with patch.object(settings, "model_gateway_auto_index_interactions", False):
        indexed = GatewayUsageSearchService(db_session).auto_index_interaction(
            usage=usage,
            request_payload={"input": "hidden prompt"},
            response_payload={"output_text": "hidden answer"},
        )

    assert indexed is None
    assert (
        crud_gateway_usage_search_document.get_by_api_usage_id(
            db_session, api_usage_id=str(usage.id)
        )
        is None
    )


def test_auto_index_interaction_uses_metadata_only_when_capture_disabled(
    db_session, test_user
):
    """Auto-indexing should omit request/response bodies when content capture is off."""
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        meta_data={
            "requested_model": "openai/gpt-5",
            "endpoint_kind": "responses",
            "gateway_provider": "preloop",
            "finish_reason": "stop",
        },
    )

    with (
        patch.object(settings, "model_gateway_auto_index_interactions", True),
        patch.object(settings, "model_gateway_capture_content", False),
    ):
        indexed = GatewayUsageSearchService(db_session).auto_index_interaction(
            usage=usage,
            request_payload={"input": "should not be indexed"},
            response_payload={"output_text": "should not be indexed either"},
        )

    assert indexed is not None
    assert "requested_model: openai/gpt-5" in indexed.searchable_text
    assert "request.input" not in indexed.searchable_text
    assert "response.output_text" not in indexed.searchable_text
    assert indexed.meta_data["request_payload_present"] is False
    assert indexed.meta_data["response_payload_present"] is False


def test_auto_index_interaction_skips_failures_unless_explicitly_enabled(
    db_session, test_user
):
    """Failed interactions should require an explicit indexing opt-in."""
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=429,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        meta_data={"error_detail": "Rate limit exceeded"},
    )

    service = GatewayUsageSearchService(db_session)
    with (
        patch.object(settings, "model_gateway_auto_index_interactions", True),
        patch.object(settings, "model_gateway_auto_index_failed_interactions", False),
        patch.object(settings, "model_gateway_capture_content", True),
    ):
        skipped = service.auto_index_interaction(
            usage=usage,
            request_payload={"input": "retry later"},
            response_payload=None,
        )

    assert skipped is None
    assert (
        crud_gateway_usage_search_document.get_by_api_usage_id(
            db_session, api_usage_id=str(usage.id)
        )
        is None
    )

    with (
        patch.object(settings, "model_gateway_auto_index_interactions", True),
        patch.object(settings, "model_gateway_auto_index_failed_interactions", True),
        patch.object(settings, "model_gateway_capture_content", True),
    ):
        indexed = service.auto_index_interaction(
            usage=usage,
            request_payload={"input": "retry later"},
            response_payload=None,
        )

    assert indexed is not None
    assert "outcome: error" in indexed.searchable_text
    assert "error_detail: Rate limit exceeded" in indexed.searchable_text
    assert "request.input: retry later" in indexed.searchable_text


def test_search_account_documents_matches_non_contiguous_terms(db_session, test_user):
    """Search should use token-aware matching instead of contiguous substring scans."""
    usage = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
    )
    service = GatewayUsageSearchService(db_session)
    service.index_interaction(
        usage=usage,
        request_payload={"input": "Please review the production rollback checklist"},
        response_payload={"output_text": "Checklist reviewed"},
    )

    results = crud_gateway_usage_search_document.search_account_documents(
        db_session,
        account_id=str(test_user.account_id),
        start_date=usage.timestamp - timedelta(days=1),
        end_date=usage.timestamp + timedelta(days=1),
        query="rollback production",
    )

    assert results["total"] == 1
    assert results["items"][0]["api_usage_id"] == str(usage.id)
