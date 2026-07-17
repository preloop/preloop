"""Tests for the analysis-model authorizer extension point."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.services import optimization_gating as gating
from preloop.services.model_gateway_budget import is_built_in_hosted_model


@pytest.fixture(autouse=True)
def _clean_registry():
    """Never leak a registered authorizer across tests (module-global state)."""
    gating.register_analysis_model_authorizer(None)
    yield
    gating.register_analysis_model_authorizer(None)


def _model(**kwargs):
    return SimpleNamespace(id=uuid4(), account_id=uuid4(), meta_data={}, **kwargs)


class TestAuthorizeAnalysisModel:
    def test_default_allows_everything(self):
        # No authorizer registered: any model passes silently.
        gating.authorize_analysis_model(
            MagicMock(), account=MagicMock(), ai_model=_model(), feature="x"
        )

    def test_none_model_never_consults_authorizer(self):
        calls = []
        gating.register_analysis_model_authorizer(lambda db, **kw: calls.append(kw))
        gating.authorize_analysis_model(
            MagicMock(), account=MagicMock(), ai_model=None, feature="x"
        )
        assert calls == []

    def test_registered_authorizer_is_consulted_and_can_deny(self):
        class DeniedError(Exception):
            pass

        def authorizer(db, *, account, ai_model, feature):
            raise DeniedError(feature)

        gating.register_analysis_model_authorizer(authorizer)
        with pytest.raises(DeniedError):
            gating.authorize_analysis_model(
                MagicMock(),
                account=MagicMock(),
                ai_model=_model(),
                feature="session_optimization",
            )

    def test_clearing_registration_restores_allow(self):
        gating.register_analysis_model_authorizer(
            lambda db, **kw: (_ for _ in ()).throw(RuntimeError("deny"))
        )
        gating.register_analysis_model_authorizer(None)
        gating.authorize_analysis_model(
            MagicMock(), account=MagicMock(), ai_model=_model(), feature="x"
        )


class TestIsBuiltInHostedModel:
    def test_hosted_flag_marks_hosted(self):
        model = SimpleNamespace(account_id=uuid4(), meta_data={"hosted": True})
        assert is_built_in_hosted_model(model) is True

    def test_system_gateway_model_is_hosted(self):
        model = SimpleNamespace(
            account_id=None, meta_data={"gateway": {"enabled": True}}
        )
        assert is_built_in_hosted_model(model) is True

    def test_account_owned_byok_model_is_not_hosted(self):
        model = SimpleNamespace(
            account_id=uuid4(), meta_data={"gateway": {"enabled": True}}
        )
        assert is_built_in_hosted_model(model) is False

    def test_plain_model_without_metadata_is_not_hosted(self):
        model = SimpleNamespace(account_id=uuid4(), meta_data=None)
        assert is_built_in_hosted_model(model) is False
