"""Tests for the agent_model_list shared helper."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from preloop.models.crud import crud_ai_model
from preloop.services.agent_model_list import (
    list_authorized_gateway_models,
)
from preloop.services.flow_runtime_token import create_flow_runtime_token
from preloop.services.model_gateway_auth import build_runtime_key_auth_context
from preloop.services.secret_service import (
    ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
)

_CRUD_PATCH = "preloop.models.crud.ai_model.ai_model"


def _make_ai_model(
    id_: str,
    identifier: str,
    provider: str = "openai",
    name: str | None = None,
    gateway_enabled: bool = True,
    gateway_alias: str | None = None,
) -> MagicMock:
    """Create a minimal AIModel mock."""
    m = MagicMock()
    m.id = id_
    m.model_identifier = identifier
    m.provider_name = provider
    m.name = name
    m.is_principal_bound_oauth = False
    m.meta_data = {
        "gateway": {
            "enabled": gateway_enabled,
            **({"model_alias": gateway_alias} if gateway_alias else {}),
        }
    }
    m.api_endpoint = None
    m.model_parameters = None
    m.is_default = False
    m.credentials_secret = None
    return m


class TestListAuthorizedGatewayModels:
    """Test list_authorized_gateway_models."""

    def test_returns_all_gateway_enabled_models(self):
        """Gateway-enabled BYOK models are returned when no auth context."""
        models = [
            _make_ai_model("1", "claude-sonnet-4", "anthropic", gateway_enabled=True),
            _make_ai_model("2", "gpt-5", "openai", gateway_enabled=True),
            _make_ai_model("3", "local-only", "custom", gateway_enabled=False),
        ]
        db = MagicMock()
        with patch(_CRUD_PATCH) as mock_crud:
            mock_crud.get_all_for_account.return_value = models
            result = list_authorized_gateway_models(db, "acct-1")

        assert len(result) == 2
        aliases = {m.alias for m in result}
        assert "anthropic/claude-sonnet-4" in aliases
        assert "openai/gpt-5" in aliases

    def test_excludes_unauthorized_models(self):
        """Models not in the authorized set are excluded."""
        models = [
            _make_ai_model("1", "claude-sonnet-4", "anthropic"),
            _make_ai_model("2", "gpt-5", "openai"),
        ]
        db = MagicMock()
        auth_ctx = MagicMock()

        with patch(_CRUD_PATCH) as mock_crud:
            mock_crud.get_all_for_account.return_value = models
            with patch(
                "preloop.services.agent_model_list.compute_authorized_model_ids",
                return_value=frozenset({"1"}),
            ):
                result = list_authorized_gateway_models(
                    db, "acct-1", auth_context=auth_ctx
                )

        assert len(result) == 1
        assert result[0].alias == "anthropic/claude-sonnet-4"

    def test_empty_account_returns_empty_list(self):
        """An account with no models returns an empty list."""
        db = MagicMock()
        with patch(_CRUD_PATCH) as mock_crud:
            mock_crud.get_all_for_account.return_value = []
            result = list_authorized_gateway_models(db, "acct-1")
        assert result == []

    def test_custom_alias_used_over_default(self):
        """A configured gateway alias takes precedence over the default."""
        models = [
            _make_ai_model(
                "1",
                "claude-sonnet-4-20250514",
                "anthropic",
                gateway_enabled=True,
                gateway_alias="sonnet-4",
            ),
        ]
        db = MagicMock()
        with patch(_CRUD_PATCH) as mock_crud:
            mock_crud.get_all_for_account.return_value = models
            result = list_authorized_gateway_models(db, "acct-1")

        assert len(result) == 1
        assert result[0].alias == "sonnet-4"

    def test_deduplicates_aliases(self):
        """Two models resolving to the same alias produce one entry."""
        models = [
            _make_ai_model(
                "1", "gpt-5", "openai", gateway_enabled=True, gateway_alias="gpt-5"
            ),
            _make_ai_model(
                "2", "gpt-5", "openai", gateway_enabled=True, gateway_alias="gpt-5"
            ),
        ]
        db = MagicMock()
        with patch(_CRUD_PATCH) as mock_crud:
            mock_crud.get_all_for_account.return_value = models
            result = list_authorized_gateway_models(db, "acct-1")

        assert len(result) == 1

    def test_sorted_by_alias(self):
        """Results are sorted by alias for deterministic output."""
        models = [
            _make_ai_model("1", "zmodel", "openai"),
            _make_ai_model("2", "amodel", "openai"),
            _make_ai_model("3", "mmodel", "openai"),
        ]
        db = MagicMock()
        with patch(_CRUD_PATCH) as mock_crud:
            mock_crud.get_all_for_account.return_value = models
            result = list_authorized_gateway_models(db, "acct-1")

        aliases = [m.alias for m in result]
        assert aliases == sorted(aliases)

    def test_display_name_fallback_chain(self):
        """The model's name falls back to model_identifier then alias."""
        models = [
            _make_ai_model("1", "gpt-5", "openai", name="GPT 5"),
            _make_ai_model("2", "claude-opus-4", "anthropic", name=None),
        ]
        db = MagicMock()
        with patch(_CRUD_PATCH) as mock_crud:
            mock_crud.get_all_for_account.return_value = models
            result = list_authorized_gateway_models(db, "acct-1")

        by_alias = {m.alias: m for m in result}
        assert by_alias["openai/gpt-5"].display_name == "GPT 5"
        assert by_alias["anthropic/claude-opus-4"].display_name == "claude-opus-4"

    def test_principal_bound_models_excluded_without_auth_context(self):
        """No context means fail closed: subscription-OAuth models drop out.

        Only the managed agent holding an active binding may use a
        principal-bound OAuth model, so a caller that cannot name its
        principal must never see one advertised.
        """
        byok = _make_ai_model("1", "gpt-5", "openai")
        bound = _make_ai_model("2", "claude-sonnet-4-5", "anthropic")
        bound.is_principal_bound_oauth = True
        db = MagicMock()

        with patch(_CRUD_PATCH) as mock_crud:
            mock_crud.get_all_for_account.return_value = [byok, bound]
            result = list_authorized_gateway_models(db, "acct-1")

        assert [m.alias for m in result] == ["openai/gpt-5"]


BYOK_ALIAS = "openai/gpt-5-flow"
BOUND_ALIAS = "anthropic/claude-sonnet-4-5-flow"


def _create_byok_model(db_session, test_user):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"BYOK {uuid4()}",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "sk-byok-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": BYOK_ALIAS,
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )


def _create_principal_bound_model(db_session, test_user):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Subscription {uuid4()}",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "credential_type": ANTHROPIC_CLAUDE_CODE_OAUTH_CREDENTIAL_TYPE,
            "credential_payload": {"access": "oauth-access-token"},
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": BOUND_ALIAS,
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )


class TestFlowExecutionPrincipal:
    """The flow-execution credential must not be offered bound models.

    A flow execution mints a runtime key whose ``runtime_principal.type`` is
    ``flow_execution`` and which carries no ``managed_agent_id``, so it
    resolves to no managed agent and is never authorized for
    ``is_principal_bound_oauth`` models.  Advertising them in the generated
    agent config would only produce a 400 at the gateway.
    """

    def test_bound_models_excluded_for_flow_execution_credential(
        self, db_session, test_user
    ):
        _create_byok_model(db_session, test_user)
        _create_principal_bound_model(db_session, test_user)

        flow = SimpleNamespace(
            id=uuid4(),
            account_id=test_user.account_id,
            name="Nightly triage",
            allowed_mcp_tools=[],
            allowed_mcp_servers=[],
        )
        token, api_key_id = create_flow_runtime_token(
            db_session, flow=flow, execution_id=uuid4()
        )
        assert token and api_key_id

        auth_context = build_runtime_key_auth_context(
            db_session, token=token, api_key_id=str(api_key_id)
        )
        assert auth_context is not None
        assert auth_context.api_key is not None

        aliases = [
            m.alias
            for m in list_authorized_gateway_models(
                db_session,
                str(test_user.account_id),
                auth_context=auth_context,
            )
        ]

        assert BYOK_ALIAS in aliases
        assert BOUND_ALIAS not in aliases
