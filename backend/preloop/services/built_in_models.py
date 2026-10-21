"""Deployment-provided built-in model discovery and safe public presentation."""

from preloop.models import models
from preloop.plugins.base import get_plugin_manager
from preloop.schemas.ai_model import AIModelRead

from preloop.services.model_runtime_resolver import effective_gateway_alias


def built_in_models_enabled() -> bool:
    """Whether this deployment provides hosted models (off in OSS by default)."""
    return (
        get_plugin_manager().get_enabled_features()["features"].get("hosted_models")
        is True
    )


def is_retired_system_model(model: models.AIModel) -> bool:
    """Retain historical global rows without selecting them for new work."""
    metadata = model.meta_data if isinstance(model.meta_data, dict) else {}
    gateway = metadata.get("gateway")
    return model.account_id is None and (
        metadata.get("retired") is True
        or (isinstance(gateway, dict) and gateway.get("enabled") is False)
    )


def is_available_built_in_model(model: models.AIModel) -> bool:
    """Accept every enabled, non-retired deployment-owned model."""
    metadata = model.meta_data if isinstance(model.meta_data, dict) else {}
    gateway = metadata.get("gateway")
    return (
        model.account_id is None
        and not is_retired_system_model(model)
        and isinstance(gateway, dict)
        and gateway.get("enabled") is True
    )


def public_built_in_model(
    model: models.AIModel | AIModelRead, *, account_has_default: bool = False
) -> AIModelRead:
    """Expose a selectable model without operator credentials or routing metadata."""
    return AIModelRead(
        id=model.id,
        name=model.name,
        description=model.description,
        provider_name=model.provider_name,
        model_identifier=model.model_identifier,
        model_kind=model.model_kind,
        is_default=bool(model.is_default) and not account_has_default,
        is_built_in=True,
        unpriced=getattr(model, "unpriced", False),
        has_api_key=model.has_api_key,
        supports_server_side_generation=model.supports_server_side_generation,
        updated_at=model.updated_at,
        meta_data={
            "gateway": {"enabled": True, "model_alias": effective_gateway_alias(model)},
        },
    )
