"""Optional hosted spending guard for provider paths without settlement hooks.

Also owns the shared shape of one refusal: a deployment-provided hosted model
the operator has not given a verified fixed USD tariff. The metering plugin
raises it, the gateway records it, and the agent-side classifiers read it, so
the code, the class and the sentence are defined once here rather than three
times in three repositories.
"""

import logging
from typing import Any, Optional

from preloop.models.models.ai_model import AIModel
from preloop.plugins import get_plugin_manager
from preloop.services.model_gateway_errors import GatewayProvider, ModelGatewayAPIError
from preloop.services.upstream_errors import ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED

logger = logging.getLogger(__name__)

#: OpenAI-shaped ``error.code`` for the refusal. Distinct from
#: ``hosted_metering_unavailable`` (a temporary ledger problem, worth a retry):
#: this one cannot change until an operator acts, so clients must stop.
HOSTED_TARIFF_UNCONFIGURED_CODE = "hosted_tariff_unconfigured"


def _model_alias(model: Any) -> str:
    """The name the caller asked for, so the message names what they typed.

    Deliberately reimplements the alias spelling instead of calling the
    runtime resolver: this only ever builds an error message, on a path that
    is already failing, for rows that may be half-configured. Attribute reads
    cannot raise where resolution can.

    Args:
        model: The resolved model, or anything shaped like one.

    Returns:
        The configured gateway alias, else ``provider/model_identifier``, else
        the display name, else ``"unknown"``.
    """
    meta_data = getattr(model, "meta_data", None)
    gateway = meta_data.get("gateway") if isinstance(meta_data, dict) else None
    configured = gateway.get("model_alias") if isinstance(gateway, dict) else None
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    identifier = str(getattr(model, "model_identifier", "") or "").strip()
    provider = str(getattr(model, "provider_name", "") or "").strip().lower()
    if identifier:
        return f"{provider}/{identifier}" if provider else identifier
    name = getattr(model, "name", None)
    return str(name).strip() if name and str(name).strip() else "unknown"


def hosted_tariff_unconfigured_error(
    model: Any,
    *,
    reason: Optional[str] = None,
    provider: GatewayProvider = "openai",
) -> ModelGatewayAPIError:
    """Build the terminal refusal for a hosted model with no operator tariff.

    The status stays 503 (the deployment, not the request, is at fault) but
    the error is marked terminal and carries its own error class, so neither
    the gateway's bounded upstream retries nor the agent harness's stream
    recovery treats it as a provider hiccup worth five more attempts.

    Args:
        model: The hosted model that has no verified tariff.
        reason: Operator-facing detail naming which part of the tariff is
            missing. Appended in parentheses; never shown alone.
        provider: Gateway dialect the caller is speaking.

    Returns:
        A 503 :class:`ModelGatewayAPIError` naming the model and the way out.
    """
    message = (
        f"Hosted model {_model_alias(model)} has no operator tariff; "
        "use your own provider key or pick another model."
    )
    if reason:
        message = f"{message} ({reason})"
    return ModelGatewayAPIError(
        provider=provider,
        status_code=503,
        message=message,
        code=HOSTED_TARIFF_UNCONFIGURED_CODE,
        error_class=ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED,
        terminal=True,
    )


def guard_unmetered_hosted_call(model: Any, *, account_id: Any = None) -> None:
    """BYOK/OSS pass; metered hosted adapters must reserve before dispatch."""
    from preloop.services.billing_enforcement import entitlements_enforced

    if account_id is not None and not entitlements_enforced(account_id):
        return
    meter = get_plugin_manager().get_service("hosted_spend")
    if meter is not None:
        meter.guard_unmetered(model)


def hosted_model_selectable(model: Any) -> bool:
    """Whether a model may be offered in a "what can I pick" answer.

    Only deployment-provided hosted models can be unselectable, and only when
    the hosted metering plugin is loaded (billing enforcement on) and its
    ``selectable(model)`` hook says the operator has not verified a tariff.
    Core never reads the tariff flag itself; with no plugin, or a plugin
    without the hook, every model stays selectable.

    Args:
        model: The AI model row to classify.

    Returns:
        False only for a hosted model the metering plugin rejects.
    """
    from preloop.services.model_gateway_budget import is_built_in_hosted_model

    if not isinstance(model, AIModel) or not is_built_in_hosted_model(model):
        return True
    meter = get_plugin_manager().get_service("hosted_spend")
    selectable = getattr(meter, "selectable", None) if meter is not None else None
    if not callable(selectable):
        return True
    try:
        return bool(selectable(model))
    except Exception:
        # A metering failure must not take every model picker down; fail open
        # to the pre-hook behavior (the request-time guard still refuses).
        logger.exception("hosted_spend.selectable failed for model %s", model.id)
        return True
