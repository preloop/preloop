"""Non-blocking advice returned with a saved flow.

A warning never blocks a save: it names a configuration that works but is
known to give unreliable results, and links to the fix.
"""

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_ai_model, crud_flow
from preloop.schemas.openrouter import (
    STRUCTURED_RESULTS_DOCS_URL,
    pins_upstream_provider,
)
from preloop.services.litellm_routing import is_openrouter_model

logger = logging.getLogger(__name__)

OPENROUTER_UNPINNED_STRUCTURED_RESULT = "openrouter_unpinned_structured_result"


def unpinned_openrouter_model(ai_model: Any) -> bool:
    """Whether a model is OpenRouter-routed with no pinned upstream provider.

    Args:
        ai_model: A model row or namespace.

    Returns:
        True when OpenRouter may pick a different host for each request.
    """
    if ai_model is None or not is_openrouter_model(ai_model):
        return False
    parameters = getattr(ai_model, "model_parameters", None)
    routing = parameters.get("provider") if isinstance(parameters, dict) else None
    return not pins_upstream_provider(routing)


def _structured_result(db: Session, flow: Any) -> bool:
    """Whether runs of ``flow`` end with a schema-bound native result."""
    from preloop.flow_presets import PRESET_SLUGS_BY_NAME
    from preloop.services.native_result import resolve_native_result

    if not getattr(flow, "result_schema", None):
        return False
    if getattr(flow, "validate_result", True) is False:
        return False
    preset = flow if getattr(flow, "is_preset", False) else None
    if getattr(flow, "source_preset_id", None):
        preset = crud_flow.get(db, id=flow.source_preset_id)
    slug = PRESET_SLUGS_BY_NAME.get(getattr(preset, "name", "") or "")
    enabled, _ = resolve_native_result(flow, preset_slug=slug)
    return enabled


def _model_ids(flow: Any) -> list[Any]:
    """The flow's model and any routing-rule models, in a stable order."""
    from preloop.services.model_routing import (
        parse_model_by_label,
        parse_model_routing,
    )

    ids: list[Any] = []
    if getattr(flow, "ai_model_id", None):
        ids.append(flow.ai_model_id)
    try:
        routing = parse_model_routing(flow.agent_config)
        by_label = parse_model_by_label(flow.agent_config)
    except Exception:  # noqa: BLE001 - save already validated routing
        routing = by_label = None
    if routing:
        ids.extend(rule.ai_model_id for rule in routing.rules if rule.ai_model_id)
    if by_label:
        ids.extend(rule.ai_model_id for rule in by_label.root if rule.ai_model_id)
    return list(dict.fromkeys(str(value) for value in ids))


def flow_config_warnings(
    db: Session, flow: Any, *, account_id: Optional[Any] = None
) -> list[dict[str, Any]]:
    """Advice for a saved flow; never raises.

    Warns when a schema-bound native result runs on an OpenRouter-routed model
    whose ``model_parameters.provider`` pins no upstream (neither ``only`` nor
    ``order`` with ``allow_fallbacks: false``).

    Args:
        db: Database session.
        flow: The saved flow.
        account_id: Caller's account; models outside it are skipped.

    Returns:
        Warnings with ``code``, ``message``, ``ai_model_id`` and ``docs_url``.
    """
    try:
        if not _structured_result(db, flow):
            return []
        warnings: list[dict[str, Any]] = []
        for model_id in _model_ids(flow):
            model = crud_ai_model.get(db, id=model_id)
            if model is None or (
                account_id is not None
                and model.account_id is not None
                and str(model.account_id) != str(account_id)
            ):
                continue
            if not unpinned_openrouter_model(model):
                continue
            warnings.append(
                {
                    "code": OPENROUTER_UNPINNED_STRUCTURED_RESULT,
                    "ai_model_id": str(model.id),
                    "message": (
                        f"Structured results on OpenRouter depend on the upstream "
                        f"provider that serves each request, and {model.name} "
                        "sets no provider preference. Set one on the model "
                        "(only, or order with allow_fallbacks false) to pin a "
                        "provider."
                    ),
                    "docs_url": STRUCTURED_RESULTS_DOCS_URL,
                }
            )
        return warnings
    except Exception:  # noqa: BLE001 - advice must never fail a save
        logger.warning("Flow config warnings failed", exc_info=True)
        return []
