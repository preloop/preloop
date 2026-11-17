"""Decide whether an automatic triage delivery has anything to assess.

Triage reads an issue's title and description. A provider ``issue_updated``
delivery that changes neither (a GitLab assignee, milestone or due-date edit,
which GitLab reports as a plain issue update because it has no separate
assignment hook) gives the assistant nothing new to read, so a run on it costs
a model call and rewrites the same assessment.

The gate is deliberately scoped to flows that came from the issue-triage
preset. Other flows keep every ``issue_updated`` delivery, including the
GitLab assignment edits that normalize to no other event type.

This is relevance filtering at the trigger boundary, not durable per-revision
coalescing: two deliveries that both change the description still start two
runs once the first one finishes. See issue #448.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Provider change-set keys that name the issue content triage reads. GitHub
# sends ``title``/``body``; GitLab sends ``title``/``description``.
TRIAGE_CONTENT_FIELDS: frozenset[str] = frozenset({"title", "body", "description"})


def issue_update_touches_content(event_data: Mapping[str, Any]) -> bool:
    """True when this delivery changed issue content, or cannot prove otherwise.

    Only a provider change set is treated as evidence. A payload without one
    (Jira, a replayed or synthetic payload, a manual run) keeps its existing
    behavior and counts as relevant.
    """
    if event_data.get("type") != "issue_updated":
        return True
    payload = event_data.get("payload")
    if not isinstance(payload, dict):
        return True
    changes = payload.get("changes")
    if not isinstance(changes, dict) or not changes:
        return True
    return bool(set(changes) & TRIAGE_CONTENT_FIELDS)


def is_triage_preset_flow(db: Session, flow: Any) -> bool:
    """True when ``flow`` is an account copy of the issue-triage preset.

    Identity follows ``resolve_or_create_flow``: the stored source preset
    first, then the preset name for a flow created before that column was
    written. A global preset row is never an account flow.

    The name fallback is deliberately blunt: a hand-built flow with no stored
    source preset whose name is exactly the preset's name is treated as a
    triage flow and inherits the relevance gate, so its metadata-only issue
    updates are skipped too. Operators who want such a flow to keep every
    update (a GitLab "run on assignment" automation, for instance) should give
    it a different name.
    """
    from preloop.flow_presets import PRESET_SLUGS
    from preloop.models.crud import crud_flow
    from preloop.services.preset_runner import TRIAGE_SLUG

    preset_name = PRESET_SLUGS.get(TRIAGE_SLUG)
    if not preset_name or getattr(flow, "is_preset", False):
        return False
    source_preset_id = getattr(flow, "source_preset_id", None)
    if source_preset_id is None:
        return (getattr(flow, "name", None) or "") == preset_name
    try:
        preset = crud_flow.get_global_preset_by_name(db, name=preset_name)
    except SQLAlchemyError:
        # Identity is an optimization for a skip decision. If it cannot be
        # read, keep the delivery rather than dropping a run silently.
        logger.warning("Could not read the triage preset row")
        return False
    return preset is not None and str(preset.id) == str(source_preset_id)


def skip_triage_flow_for_event(
    db: Session,
    flow: Any,
    event_data: Mapping[str, Any],
    *,
    event_touches_content: Optional[bool] = None,
) -> bool:
    """True when this triage flow must not run for this delivery.

    Relevance is a property of the delivery, not of the flow, so a caller
    filtering many flows for one event can evaluate
    :func:`issue_update_touches_content` once and pass it as
    ``event_touches_content``. The result is identical either way.
    """
    touches_content = (
        issue_update_touches_content(event_data)
        if event_touches_content is None
        else event_touches_content
    )
    return not touches_content and is_triage_preset_flow(db, flow)
