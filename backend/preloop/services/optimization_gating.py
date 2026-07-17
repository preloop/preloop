"""Extension point for gating LLM-powered optimization analysis by model.

Session optimization and replay verification are open capabilities: the
deterministic analyzers and one-click apply run everywhere, and analysis
driven by the user's OWN model keys (BYOK) is never gated. What a deployment
MAY gate is analysis executed on built-in hosted models — compute the operator
pays for. A billing/entitlement plugin registers an authorizer here; without
one (the open-source default) every model is allowed.

The authorizer receives the resolved analysis model and the feature name and
signals denial by raising (typically an ``HTTPException`` carrying the
deployment's upgrade contract). It must never be consulted for deterministic,
non-LLM work.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from sqlalchemy.orm import Session

from preloop.models.models.account import Account
from preloop.models.models.ai_model import AIModel

logger = logging.getLogger(__name__)

# Signature: (db, account=..., ai_model=..., feature=...) -> None, raising to
# deny. ``ai_model`` is the model that would execute the analysis.
AnalysisModelAuthorizer = Callable[..., None]

_authorizer: Optional[AnalysisModelAuthorizer] = None


def register_analysis_model_authorizer(
    authorizer: Optional[AnalysisModelAuthorizer],
) -> None:
    """Register (or clear, with ``None``) the analysis-model authorizer.

    Called by billing/entitlement plugins at startup. Only one authorizer is
    supported; the last registration wins.

    Args:
        authorizer: Callable consulted before LLM-powered analysis runs.
    """
    global _authorizer
    if _authorizer is not None and authorizer is not None:
        logger.info("Replacing previously registered analysis-model authorizer")
    _authorizer = authorizer


def get_analysis_model_authorizer() -> Optional[AnalysisModelAuthorizer]:
    """Return the registered authorizer, or ``None`` when none is registered."""
    return _authorizer


def authorize_analysis_model(
    db: Session,
    *,
    account: Account,
    ai_model: Optional[AIModel],
    feature: str,
) -> None:
    """Consult the registered authorizer before an LLM-powered analysis call.

    The open-source default (no authorizer registered) allows everything.
    ``ai_model=None`` (deterministic-only analysis) is always allowed and the
    authorizer is not consulted.

    Args:
        db: Database session.
        account: Account requesting the analysis.
        ai_model: The model that would execute the analysis.
        feature: Feature identifier (e.g. ``"session_optimization"``).

    Raises:
        Exception: Whatever the registered authorizer raises to deny access
            (typically an ``HTTPException`` with a 402 upgrade contract).
    """
    if ai_model is None or _authorizer is None:
        return
    _authorizer(db, account=account, ai_model=ai_model, feature=feature)
