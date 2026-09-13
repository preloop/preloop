"""Provider metadata recovery must be scoped, bounded and preserve actual costs."""

import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import requests

from preloop.models import models
from preloop.services import openrouter_generation_cost as recovery


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Build account-owned rows and mock both secret and network boundaries."""
    account_id = uuid4()
    model = models.AIModel(
        id=uuid4(),
        account_id=account_id,
        provider_name="openai-compatible",
        api_endpoint="https://openrouter.ai/api/v1",
    )
    row = models.ApiUsage(
        id=uuid4(),
        account_id=account_id,
        ai_model_id=model.id,
        upstream_request_id="gen-example-1",
        cost_source="unpriced",
        estimated_cost=None,
    )
    db = Mock()
    secret_service = Mock()
    secret_service.resolve_ai_model_credentials.return_value = SimpleNamespace(
        credential_type="api_key", value="example-provider-key"
    )
    monkeypatch.setattr(recovery, "get_secret_service", lambda: secret_service)
    payload = {
        "data": {
            "id": row.upstream_request_id,
            "total_cost": 0.02,
            "model": "example/routed-model",
            "is_byok": False,
            "upstream_inference_cost": 0.01,
        }
    }
    response = Mock(status_code=200)
    response.json.side_effect = lambda: payload
    fetch = Mock(return_value=response)
    monkeypatch.setattr(recovery.requests, "get", fetch)
    lookup = recovery.OpenRouterGenerationCostLookup(db, account_id=str(account_id))
    return SimpleNamespace(
        account_id=account_id,
        model=model,
        row=row,
        db=db,
        lookup=lookup,
        secret_service=secret_service,
        payload=payload,
        response=response,
        fetch=fetch,
    )


def test_recovers_exact_actual_with_minimal_provenance(setup: SimpleNamespace) -> None:
    """OpenRouter credits are not double-counted with the upstream breakdown."""
    result = setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row)
    assert result is not None
    assert result.cost == 0.02
    assert result.generation_id == "gen-example-1"
    assert result.provenance["method"] == "generation_metadata"
    assert result.usage_details["cost"] == 0.02
    assert result.provenance["routed_model"] == "example/routed-model"
    setup.fetch.assert_called_once_with(
        recovery.GENERATION_URL,
        params={"id": "gen-example-1"},
        headers={"Authorization": "Bearer example-provider-key"},
        timeout=3.0,
        allow_redirects=False,
    )
    setup.secret_service.resolve_ai_model_credentials.assert_called_once_with(
        setup.model, db=setup.db, allow_refresh=False
    )
    assert setup.lookup.summary["recovered"] == 1
    assert "example-provider-key" not in json.dumps(result.provenance)
    assert setup.row.estimated_cost is None
    assert setup.row.cost_source == "unpriced"
    setup.db.commit.assert_not_called()
    setup.db.flush.assert_not_called()


@pytest.mark.parametrize("cost", [0, 0.0, 0.7])
def test_zero_is_a_valid_provider_charge(setup: SimpleNamespace, cost: float) -> None:
    setup.payload["data"]["total_cost"] = cost
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row).cost == cost


def test_byok_ambiguity_stays_unresolved_without_double_counting(
    setup: SimpleNamespace,
) -> None:
    setup.payload["data"].update(is_byok=True, upstream_inference_cost=0.4)
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert setup.lookup.summary["ambiguous_cost"] == 2
    assert setup.lookup.summary["recovered"] == 0
    assert setup.fetch.call_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("total_cost", None),
        ("total_cost", -1),
        ("total_cost", True),
        ("total_cost", "0.1"),
        ("total_cost", float("nan")),
        ("total_cost", float("inf")),
        ("total_cost", 10**1000),
        ("id", "gen-other"),
        ("is_byok", None),
    ],
)
def test_invalid_provider_response_stays_unresolved(
    setup: SimpleNamespace, field: str, value: object
) -> None:
    setup.payload["data"][field] = value
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert setup.lookup.summary["unavailable"] == 1


def test_missing_byok_upstream_does_not_understate_spend(
    setup: SimpleNamespace,
) -> None:
    setup.payload["data"].update(is_byok=True, upstream_inference_cost=None)
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None


@pytest.mark.parametrize(
    "generation_id",
    [None, "", "resp_123", "chatcmpl_123", "msg_123", "gen-x\n", "gen-a?key=x"],
)
def test_missing_or_synthetic_ids_never_trigger_network(
    setup: SimpleNamespace, generation_id: str | None
) -> None:
    setup.row.upstream_request_id = generation_id
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert setup.lookup.summary["missing_id"] == 1
    setup.fetch.assert_not_called()
    setup.secret_service.resolve_ai_model_credentials.assert_not_called()


@pytest.mark.parametrize("target", ["model", "row", "model_id", "secret"])
def test_foreign_account_or_model_never_resolves_credentials(
    setup: SimpleNamespace, target: str
) -> None:
    if target == "model":
        setup.model.account_id = uuid4()
    elif target == "row":
        setup.row.account_id = uuid4()
    elif target == "model_id":
        setup.row.ai_model_id = uuid4()
    else:
        setup.model.credentials_secret = models.SecretReference(account_id=uuid4())
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    setup.fetch.assert_not_called()
    setup.secret_service.resolve_ai_model_credentials.assert_not_called()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://openrouter.ai/api/v1",
        "https://openrouter.ai.evil.example/api/v1",
        "https://evil.example/openrouter.ai",
        "https://user@openrouter.ai/api/v1",
        "https://openrouter.ai:8443/api/v1",
        "https://openrouter.ai:invalid/api/v1",
    ],
)
def test_untrusted_origin_never_resolves_or_sends_key(
    setup: SimpleNamespace, endpoint: str
) -> None:
    setup.model.api_endpoint = endpoint
    setup.model.provider_name = "openrouter"
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    setup.fetch.assert_not_called()
    setup.secret_service.resolve_ai_model_credentials.assert_not_called()


def test_explicit_openrouter_default_origin_is_supported(
    setup: SimpleNamespace,
) -> None:
    setup.model.provider_name = "openrouter"
    setup.model.api_endpoint = None
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is not None


@pytest.mark.parametrize(
    "endpoint",
    [
        "openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai:443/api/v1",
    ],
)
def test_https_and_host_only_openrouter_origins_are_trusted(
    setup: SimpleNamespace, endpoint: str
) -> None:
    setup.model.api_endpoint = endpoint
    setup.model.provider_name = "openai-compatible"
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is not None
    setup.fetch.assert_called_once_with(
        recovery.GENERATION_URL,
        params={"id": "gen-example-1"},
        headers={"Authorization": "Bearer example-provider-key"},
        timeout=3.0,
        allow_redirects=False,
    )


@pytest.mark.parametrize("status", [301, 302, 404, 500])
def test_unavailable_and_redirect_responses_are_not_retried(
    setup: SimpleNamespace, status: int
) -> None:
    setup.response.status_code = status
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert setup.fetch.call_count == 1
    assert setup.lookup.summary["unavailable"] == 2
    setup.response.json.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_and_rate_limits_suppress_further_calls(
    setup: SimpleNamespace, status: int
) -> None:
    setup.response.status_code = status
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    setup.row.upstream_request_id = "gen-example-2"
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert setup.fetch.call_count == 1
    assert setup.lookup.summary["deferred" if status == 429 else "unavailable"] >= 1


def test_duplicate_generations_use_cache_after_call_budget(
    setup: SimpleNamespace,
) -> None:
    setup.lookup = recovery.OpenRouterGenerationCostLookup(
        setup.db, account_id=str(setup.account_id), max_calls=1
    )
    first = setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row)
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is first
    setup.row.upstream_request_id = "gen-example-2"
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert setup.fetch.call_count == 1
    assert setup.lookup.summary["recovered"] == 2
    assert setup.lookup.summary["cache_hits"] == 1
    assert setup.lookup.summary["deferred"] == 1


def test_elapsed_budget_bounds_additional_lookups(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: clock[0])
    setup.lookup = recovery.OpenRouterGenerationCostLookup(
        setup.db, account_id=str(setup.account_id), max_elapsed_seconds=0.5
    )

    def request(*args: object, **kwargs: object) -> Mock:
        clock[0] += 0.5
        return setup.response

    setup.fetch.side_effect = request
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is not None
    assert setup.fetch.call_args.kwargs["timeout"] == 0.5
    setup.row.upstream_request_id = "gen-example-2"
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert setup.fetch.call_count == 1
    assert setup.lookup.summary["deferred"] == 1


def test_network_and_secret_errors_remain_best_effort(setup: SimpleNamespace) -> None:
    setup.fetch.side_effect = requests.Timeout("sensitive transport details")
    assert setup.lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert setup.lookup.summary["unavailable"] == 1
    setup.secret_service.resolve_ai_model_credentials.side_effect = ValueError("secret")
    lookup = recovery.OpenRouterGenerationCostLookup(
        setup.db, account_id=str(setup.account_id)
    )
    assert lookup.lookup(ai_model=setup.model, usage_row=setup.row) is None
    assert lookup.summary["unavailable"] == 1
    assert setup.fetch.call_count == 1
