"""Documented native pages and compatible cursor discovery, without network."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from preloop.services import ai_model_provider as provider


def _response(models: list[dict], *, total: int, page: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.com"),
        json={
            "success": True,
            "output": {
                "models": models,
                "total": total,
                "page_no": page,
                "page_size": 20,
            },
        },
    )


@pytest.mark.asyncio
async def test_native_pages_use_documented_host_and_keep_chat_capabilities() -> None:
    pages = [
        _response(
            [{"model": f"qwen-vl-{i}", "capabilities": ["TG"]} for i in range(20)],
            total=22,
        ),
        _response(
            [
                {"model": "qwen-chat-next", "capabilities": ["TG", "Reasoning"]},
                {"model": "text-embedding", "capabilities": ["Embedding"]},
            ],
            total=22,
            page=2,
        ),
    ]
    with patch("httpx.AsyncClient") as client:
        client.return_value.__aenter__.return_value.get = AsyncMock(side_effect=pages)
        result = await provider._get_qwen_models(
            "synthetic-key", provider.QWEN_INTL_BASE_URL
        )
        calls = client.return_value.__aenter__.return_value.get.call_args_list
    assert result.models == ["qwen-chat-next"]
    assert result.source == "live"
    assert [call.kwargs["params"]["page_no"] for call in calls] == [1, 2]
    assert all(
        call.args[0] == "https://dashscope-intl.aliyuncs.com/api/v1/models"
        for call in calls
    )
    assert calls[0].kwargs["headers"]["Authorization"] == "Bearer synthetic-key"
    assert "synthetic-key" not in calls[0].args[0]


@pytest.mark.asyncio
async def test_native_auth_failure_does_not_fall_back() -> None:
    with (
        patch("httpx.AsyncClient") as client,
        patch.object(
            provider, "_get_catalog_provider_models", new_callable=AsyncMock
        ) as fallback,
    ):
        client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=httpx.Response(401)
        )
        with pytest.raises(provider.ProviderAuthError):
            await provider._get_qwen_models(
                "synthetic-key", provider.QWEN_INTL_BASE_URL
            )
        fallback.assert_not_called()


@pytest.mark.asyncio
async def test_native_unavailable_uses_compatible_listing() -> None:
    with (
        patch("httpx.AsyncClient") as client,
        patch.object(
            provider, "_get_catalog_provider_models", new_callable=AsyncMock
        ) as fallback,
    ):
        client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=httpx.Response(
                404, request=httpx.Request("GET", "https://example.com")
            )
        )
        fallback.return_value = provider.ModelDiscoveryResult(
            models=["qwen-chat"], source="live"
        )
        result = await provider._get_qwen_models(
            "synthetic-key", provider.QWEN_INTL_BASE_URL
        )
    assert result.models == ["qwen-chat"]
    assert fallback.call_args.kwargs["base_url"] == provider.QWEN_INTL_BASE_URL


@pytest.mark.asyncio
async def test_workspace_never_sends_key_to_different_native_host() -> None:
    endpoint = "https://example.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
    with (
        patch.object(
            provider, "_get_qwen_native_models", new_callable=AsyncMock
        ) as native,
        patch.object(
            provider, "_get_catalog_provider_models", new_callable=AsyncMock
        ) as compatible,
    ):
        compatible.return_value = provider.ModelDiscoveryResult(
            models=["qwen-chat"], source="live"
        )
        await provider._get_qwen_models("synthetic-key", endpoint)
    native.assert_not_called()
    assert compatible.call_args.kwargs["base_url"] == endpoint


@pytest.mark.asyncio
async def test_repeated_native_page_is_not_a_complete_live_catalog() -> None:
    page = _response([{"model": "qwen-chat", "capabilities": ["TG"]}], total=100)
    with patch("httpx.AsyncClient") as client:
        get = AsyncMock(return_value=page)
        client.return_value.__aenter__.return_value.get = get
        result = await provider._get_qwen_native_models("synthetic-key")
    assert result.source == "fallback"
    assert result.models == []
    assert get.call_count == 2


@pytest.mark.asyncio
async def test_native_listing_has_overall_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider, "MODEL_DISCOVERY_TIMEOUT_SECONDS", 0.01)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    with patch("httpx.AsyncClient") as client:
        client.return_value.__aenter__.return_value.get = slow
        result = await provider._get_qwen_native_models("synthetic-key")
    assert result.error == provider.ERROR_TIMEOUT


@pytest.mark.asyncio
async def test_compatible_cursor_pages_are_bounded_and_deduplicated() -> None:
    with patch("openai.AsyncOpenAI") as client:
        instance = MagicMock()
        instance.models.list = AsyncMock(
            side_effect=[
                {"data": [{"id": "qwen-a"}], "has_more": True, "last_id": "qwen-a"},
                {"data": [{"id": "qwen-a"}, {"id": "qwen-b"}], "has_more": False},
            ]
        )
        client.return_value = instance
        result = await provider._get_qwen_models("synthetic-key")
    assert result.models == ["qwen-a", "qwen-b"]
    assert instance.models.list.call_args.kwargs == {"extra_query": {"after": "qwen-a"}}


@pytest.mark.asyncio
async def test_compatible_repeated_cursor_returns_safe_failure() -> None:
    with patch("openai.AsyncOpenAI") as client:
        instance = MagicMock()
        instance.models.list = AsyncMock(
            return_value={
                "data": [{"id": "qwen-a"}],
                "has_more": True,
                "last_id": "qwen-a",
            }
        )
        client.return_value = instance
        result = await provider._get_qwen_models("synthetic-key")
    assert result.models == []
    assert result.source == "fallback"
    assert instance.models.list.call_count == 2


@pytest.mark.asyncio
async def test_native_catalog_respects_shared_model_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider, "MAX_DISCOVERED_MODELS", 20)
    with patch("httpx.AsyncClient") as client:
        get = AsyncMock(
            return_value=_response(
                [{"model": f"chat-{i}", "capabilities": ["TG"]} for i in range(20)],
                total=100,
            )
        )
        client.return_value.__aenter__.return_value.get = get
        result = await provider._get_qwen_native_models("synthetic")
    assert len(result.models) == 20
    assert get.call_count == 1
