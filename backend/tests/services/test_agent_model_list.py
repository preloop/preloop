"""Tests for the agent_model_list shared helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from preloop.services.agent_model_list import (
    list_authorized_gateway_models,
)

_CRUD_PATCH = "preloop.models.crud.ai_model.ai_model"


def _make_ai_model(
    id_: str,
    identifier: str,
    provider: str = "openai",
    display_name: str | None = None,
    gateway_enabled: bool = True,
    gateway_alias: str | None = None,
) -> MagicMock:
    """Create a minimal AIModel mock."""
    m = MagicMock()
    m.id = id_
    m.model_identifier = identifier
    m.provider_name = provider
    m.display_name = display_name
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
        """All gateway-enabled models are returned when no auth context."""
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
        """display_name falls back to model_identifier then alias."""
        models = [
            _make_ai_model("1", "gpt-5", "openai", display_name="GPT 5"),
            _make_ai_model("2", "claude-opus-4", "anthropic", display_name=None),
        ]
        db = MagicMock()
        with patch(_CRUD_PATCH) as mock_crud:
            mock_crud.get_all_for_account.return_value = models
            result = list_authorized_gateway_models(db, "acct-1")

        by_alias = {m.alias: m for m in result}
        assert by_alias["openai/gpt-5"].display_name == "GPT 5"
        assert by_alias["anthropic/claude-opus-4"].display_name == "claude-opus-4"
