"""Tests for Entra ID / managed identity auth on Azure OpenAI models."""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import httpx
import litellm
import pytest

from preloop.models.models.ai_model import AIModel
from preloop.schemas.ai_model import AIModelCreate, AIModelUpdate
from preloop.services import azure_entra
from preloop.services.azure_entra import (
    AZURE_COGNITIVE_SERVICES_SCOPE,
    REFRESH_MARGIN_SECONDS,
    AzureEntraTokenError,
)
from preloop.services.azure_openai import (
    azure_auth_mode,
    azure_request_kwargs,
    normalize_azure_auth_meta,
)
from preloop.services.litellm_routing import preloop_user_agent
from preloop.services.model_credentials import (
    build_aux_kwargs,
    build_aux_openai_client,
    resolve_model_call_credentials,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService

RESOURCE = "https://example-resource.openai.azure.com"


class FakeCredential:
    """Issues numbered tokens that expire ``lifetime`` seconds after issue."""

    def __init__(self, now: List[float], lifetime: float = 3600.0) -> None:
        self.now = now
        self.lifetime = lifetime
        self.calls: List[tuple] = []
        self.error: Optional[Exception] = None

    def get_token(self, *scopes: str) -> SimpleNamespace:
        self.calls.append(scopes)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            token=f"token-{len(self.calls)}",
            expires_on=int(self.now[0] + self.lifetime),
        )


@pytest.fixture
def fake_identity(monkeypatch: pytest.MonkeyPatch):
    """Replace the azure-identity chain and the clock with fakes."""
    now = [1_000_000.0]
    credentials: Dict[Optional[str], FakeCredential] = {}

    def factory(client_id: Optional[str]) -> FakeCredential:
        credential = FakeCredential(now)
        credentials[client_id] = credential
        return credential

    azure_entra.reset_token_cache()
    monkeypatch.setattr(azure_entra, "credential_factory", factory)
    monkeypatch.setattr(azure_entra, "clock", lambda: now[0])
    yield SimpleNamespace(now=now, credentials=credentials)
    azure_entra.reset_token_cache()


def _entra_model(
    *, client_id: Optional[str] = None, version: str = "2024-10-21"
) -> SimpleNamespace:
    runtime: Dict[str, Any] = {"api_version": version, "azure_auth": "entra"}
    if client_id:
        runtime["azure_client_id"] = client_id
    return SimpleNamespace(
        provider_name="azure",
        model_identifier="chat-deployment",
        api_endpoint=RESOURCE,
        meta_data={"provider_runtime": runtime},
    )


def _service(upstream: MagicMock) -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(MagicMock(), auth_context, upstream_backend=upstream)


# --- token acquisition and refresh -----------------------------------------


def test_token_uses_cognitive_services_scope_and_is_cached(fake_identity) -> None:
    assert azure_entra.get_azure_ad_token() == "token-1"
    assert azure_entra.get_azure_ad_token() == "token-1"
    credential = fake_identity.credentials[None]
    assert credential.calls == [(AZURE_COGNITIVE_SERVICES_SCOPE,)]


def test_token_refreshes_before_expiry(fake_identity) -> None:
    assert azure_entra.get_azure_ad_token() == "token-1"
    # Still outside the refresh margin: cached token is reused.
    fake_identity.now[0] += 3600 - REFRESH_MARGIN_SECONDS - 1
    assert azure_entra.get_azure_ad_token() == "token-1"
    # Inside the margin: a new token is fetched although the old one has
    # not expired yet, so an expiring token never reaches the upstream.
    fake_identity.now[0] += 2
    assert azure_entra.get_azure_ad_token() == "token-2"


def test_expired_token_from_chain_is_rejected(fake_identity) -> None:
    azure_entra.get_azure_ad_token()
    credential = fake_identity.credentials[None]
    credential.lifetime = -10
    fake_identity.now[0] += 3600
    with pytest.raises(AzureEntraTokenError):
        azure_entra.get_azure_ad_token()


def test_token_cache_is_per_identity(fake_identity) -> None:
    azure_entra.get_azure_ad_token()
    azure_entra.get_azure_ad_token("11111111-2222-3333-4444-555555555555")
    assert set(fake_identity.credentials) == {
        None,
        "11111111-2222-3333-4444-555555555555",
    }


def test_provider_callable_returns_current_token(fake_identity) -> None:
    provider = azure_entra.azure_ad_token_provider()
    assert provider() == "token-1"
    fake_identity.now[0] += 3600
    assert provider() == "token-2"


def test_default_factory_passes_user_assigned_client_id() -> None:
    with patch("azure.identity.DefaultAzureCredential") as default_credential:
        azure_entra._default_credential_factory("client-1")
    kwargs = default_credential.call_args.kwargs
    assert kwargs["managed_identity_client_id"] == "client-1"
    assert kwargs["workload_identity_client_id"] == "client-1"
    assert kwargs["exclude_interactive_browser_credential"] is True


# --- model config -----------------------------------------------------------


def test_auth_mode_defaults_to_key() -> None:
    model = SimpleNamespace(provider_name="azure", meta_data={})
    assert azure_auth_mode(model) == "key"
    other = SimpleNamespace(
        provider_name="openai", meta_data={"provider_runtime": {"azure_auth": "entra"}}
    )
    assert azure_auth_mode(other) == "key"
    assert azure_auth_mode(_entra_model()) == "entra"


def test_normalize_entra_sets_ambient_and_key_clears_it() -> None:
    meta = {"provider_runtime": {"azure_auth": " Entra ", "azure_client_id": " c1 "}}
    normalize_azure_auth_meta(meta, provider_name="azure")
    assert meta["provider_runtime"] == {
        "azure_auth": "entra",
        "azure_client_id": "c1",
        "ambient_credentials": True,
    }
    meta["provider_runtime"]["azure_auth"] = "key"
    normalize_azure_auth_meta(meta, provider_name="Azure")
    assert meta["provider_runtime"] == {"azure_auth": "key"}


def test_normalize_entra_on_non_azure_does_not_mark_configured() -> None:
    """Entra metadata on another provider must not set ambient credentials."""
    meta = {
        "provider_runtime": {
            "azure_auth": "entra",
            "azure_client_id": "c1",
            "region": "us-east-1",
        }
    }
    normalize_azure_auth_meta(meta, provider_name="openai")
    assert meta["provider_runtime"] == {"region": "us-east-1"}
    created = AIModelCreate(
        name="GPT",
        provider_name="openai",
        model_identifier="gpt-4o",
        meta_data={
            "provider_runtime": {"azure_auth": "entra", "ambient_credentials": True}
        },
    )
    runtime = created.meta_data["provider_runtime"]
    assert "azure_auth" not in runtime
    assert "ambient_credentials" not in runtime
    model = AIModel(
        provider_name="openai",
        model_identifier="gpt-4o",
        meta_data=created.meta_data,
    )
    assert model.has_api_key is False


def test_normalize_keeps_bedrock_ambient_without_azure_auth() -> None:
    meta = {"provider_runtime": {"ambient_credentials": True, "region": "us-east-1"}}
    normalize_azure_auth_meta(meta, provider_name="bedrock")
    assert meta["provider_runtime"]["ambient_credentials"] is True
    assert meta["provider_runtime"]["region"] == "us-east-1"


def test_schema_rejects_unknown_auth_mode() -> None:
    with pytest.raises(ValueError):
        AIModelUpdate(meta_data={"provider_runtime": {"azure_auth": "password"}})


def test_schema_create_entra_needs_no_key() -> None:
    created = AIModelCreate(
        name="Azure chat",
        provider_name="azure",
        model_identifier="chat-deployment",
        api_endpoint=RESOURCE,
        meta_data={"provider_runtime": {"azure_auth": "entra"}},
    )
    runtime = created.meta_data["provider_runtime"]
    assert runtime["ambient_credentials"] is True
    model = AIModel(
        provider_name="azure",
        model_identifier="chat-deployment",
        meta_data=created.meta_data,
    )
    assert model.has_api_key is True
    assert model.credential_type == "ambient_provider"


def test_model_row_with_entra_counts_as_configured() -> None:
    model = AIModel(
        provider_name="azure",
        model_identifier="chat-deployment",
        meta_data={"provider_runtime": {"azure_auth": "entra"}},
    )
    assert model.uses_ambient_credentials is True
    assert model.has_api_key is True


# --- request kwargs and gateway --------------------------------------------


def test_aux_kwargs_carry_entra_token_provider(fake_identity) -> None:
    """Server-side generation gets the same Entra kwargs as the gateway."""
    pasted = (
        f"{RESOURCE}/openai/deployments/chat-deployment/chat/completions"
        "?api-version=2024-10-21"
    )
    model = AIModel(
        id="model-entra",
        provider_name="azure",
        model_identifier="chat-deployment",
        api_endpoint=pasted,
        meta_data={
            "provider_runtime": {
                "azure_auth": "entra",
                "api_version": "2024-10-21",
            }
        },
    )
    with patch("preloop.services.model_credentials.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="old-azure-key")
        )
        creds = resolve_model_call_credentials(model)
    assert creds["api_key"] is None
    assert creds["api_base"] == RESOURCE
    assert creds["api_version"] == "2024-10-21"
    assert creds["azure_ad_token_provider"]() == "token-1"
    kwargs = build_aux_kwargs(
        model,
        creds,
        call_site_kwargs={
            "model": "azure/chat-deployment",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert kwargs["api_key"] is None
    assert kwargs["api_base"] == RESOURCE
    assert kwargs["azure_ad_token_provider"]() == "token-1"


def test_aux_openai_client_entra_needs_no_static_key(
    fake_identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The raw OpenAI SDK client for Entra uses the token provider, not a key."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    pasted = (
        f"{RESOURCE}/openai/deployments/chat-deployment/chat/completions"
        "?api-version=2024-10-21"
    )
    model = AIModel(
        id="model-entra",
        provider_name="azure",
        model_identifier="chat-deployment",
        api_endpoint=pasted,
        meta_data={
            "provider_runtime": {
                "azure_auth": "entra",
                "api_version": "2024-10-21",
            }
        },
    )
    with patch("preloop.services.model_credentials.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = None
        creds = resolve_model_call_credentials(model)
    openai_module = SimpleNamespace(OpenAI=MagicMock(), AzureOpenAI=MagicMock())
    build_aux_openai_client(
        openai_module,
        model,
        creds,
        static_key_fallback=None,
        timeout=30.0,
        max_retries=0,
    )
    openai_module.OpenAI.assert_not_called()
    openai_module.AzureOpenAI.assert_called_once_with(
        azure_endpoint=RESOURCE,
        azure_ad_token_provider=creds["azure_ad_token_provider"],
        api_version="2024-10-21",
        timeout=30.0,
        max_retries=0,
    )


def test_aux_build_without_resolved_creds_still_uses_entra(fake_identity) -> None:
    model = AIModel(
        provider_name="azure",
        model_identifier="chat-deployment",
        api_endpoint=RESOURCE,
        meta_data={"provider_runtime": {"azure_auth": "entra"}},
    )
    kwargs = build_aux_kwargs(
        model, {}, call_site_kwargs={"model": "azure/chat-deployment"}
    )
    assert kwargs["api_key"] is None
    assert kwargs["azure_ad_token_provider"]() == "token-1"


def test_aux_token_failure_is_provider_auth_error(fake_identity) -> None:
    def failing_factory(client_id: Optional[str]) -> FakeCredential:
        credential = FakeCredential(fake_identity.now)
        credential.error = RuntimeError("no identity endpoint")
        return credential

    model = AIModel(
        id="model-entra",
        provider_name="azure",
        model_identifier="chat-deployment",
        api_endpoint=RESOURCE,
        meta_data={"provider_runtime": {"azure_auth": "entra"}},
    )
    with (
        patch.object(azure_entra, "credential_factory", failing_factory),
        patch("preloop.services.model_credentials.get_secret_service") as secrets,
    ):
        secrets.return_value.resolve_ai_model_credentials.return_value = None
        with pytest.raises(ModelGatewayAPIError) as raised:
            resolve_model_call_credentials(model)
    assert raised.value.status_code == 401
    assert raised.value.code == "azure_entra_token_error"
    assert "Cognitive Services OpenAI User" in raised.value.message
    assert "no identity endpoint" not in raised.value.message


def test_request_kwargs_carry_token_provider_not_key(fake_identity) -> None:
    kwargs = azure_request_kwargs(_entra_model())
    assert kwargs["api_key"] is None
    assert kwargs["api_base"] == RESOURCE
    assert kwargs["azure_ad_token_provider"]() == "token-1"
    assert "azure_ad_token" not in kwargs


def test_gateway_entra_completion_without_stored_key(fake_identity) -> None:
    upstream = MagicMock()
    service = _service(upstream)
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = None
        service._call_litellm(
            _entra_model(),
            messages=[{"role": "user", "content": "ping"}],
            payload={},
            provider="openai",
        )
    kwargs = upstream.completion.call_args.kwargs
    assert kwargs["api_key"] is None
    assert kwargs["azure_ad_token_provider"]() == "token-1"
    assert service._last_upstream_credential_type == "ambient"


def test_gateway_entra_ignores_a_stored_key(fake_identity) -> None:
    """A key left on the row from key mode is never sent in entra mode."""
    upstream = MagicMock()
    service = _service(upstream)
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="old-azure-key")
        )
        kwargs = service._build_completion_kwargs(
            _entra_model(),
            messages=[{"role": "user", "content": "ping"}],
            payload={},
            stream=False,
            provider="openai",
        )
    assert kwargs["api_key"] is None
    assert "azure_ad_token_provider" in kwargs


def test_gateway_entra_embedding(fake_identity) -> None:
    service = _service(MagicMock())
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = None
        kwargs = service._build_embedding_kwargs(
            _entra_model(), payload={"input": "hi"}
        )
    assert kwargs["api_key"] is None
    assert kwargs["azure_ad_token_provider"]() == "token-1"


@pytest.mark.parametrize("builder", ["completion", "embedding"])
def test_token_failure_is_provider_auth_error(fake_identity, builder) -> None:
    azure_entra.reset_token_cache()

    def failing_factory(client_id: Optional[str]) -> FakeCredential:
        credential = FakeCredential(fake_identity.now)
        credential.error = RuntimeError("no identity endpoint")
        return credential

    service = _service(MagicMock())
    with (
        patch.object(azure_entra, "credential_factory", failing_factory),
        patch("preloop.services.openai_gateway.get_secret_service") as secrets,
    ):
        secrets.return_value.resolve_ai_model_credentials.return_value = None
        with pytest.raises(ModelGatewayAPIError) as raised:
            if builder == "completion":
                service._build_completion_kwargs(
                    _entra_model(),
                    messages=[{"role": "user", "content": "ping"}],
                    payload={},
                    stream=False,
                    provider="openai",
                )
            else:
                service._build_embedding_kwargs(_entra_model(), payload={"input": "x"})
    assert raised.value.status_code == 401
    assert raised.value.code == "azure_entra_token_error"
    assert "Cognitive Services OpenAI User" in raised.value.message
    assert "no identity endpoint" not in raised.value.message


def test_key_mode_unchanged(fake_identity) -> None:
    """Regression: a key-mode row sends api-key and never asks for a token."""
    upstream = MagicMock()
    service = _service(upstream)
    model = SimpleNamespace(
        provider_name="azure",
        model_identifier="chat-deployment",
        api_endpoint=RESOURCE,
        meta_data={"provider_runtime": {"api_version": "2024-10-21"}},
    )
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="azure-key")
        )
        service._call_litellm(
            model,
            messages=[{"role": "user", "content": "ping"}],
            payload={},
            provider="openai",
        )
    upstream.completion.assert_called_once_with(
        model="azure/chat-deployment",
        messages=[{"role": "user", "content": "ping"}],
        api_key="azure-key",
        api_base=RESOURCE,
        api_version="2024-10-21",
        timeout=600,
        drop_params=True,
        extra_headers={"User-Agent": preloop_user_agent()},
    )
    assert fake_identity.credentials == {}


# --- wire level: LiteLLM sends the bearer header ---------------------------


@pytest.mark.parametrize("version", ["2024-10-21", "v1"])
def test_litellm_sends_bearer_header_not_api_key(
    fake_identity, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.delenv("AZURE_API_KEY", raising=False)
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    monkeypatch.setattr(
        litellm, "client_session", httpx.Client(transport=httpx.MockTransport(handler))
    )
    kwargs = azure_request_kwargs(_entra_model(version=version))
    litellm.completion(
        model="azure/chat-deployment",
        messages=[{"role": "user", "content": "ping"}],
        **kwargs,
    )
    assert seen["headers"]["authorization"] == "Bearer token-1"
    assert "api-key" not in seen["headers"]
