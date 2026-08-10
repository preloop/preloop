"""Service for creating and managing flow presets.

Flow presets are global templates (account_id=None) that are available to all accounts.
They are NOT copied to individual accounts - instead, users clone them when they want
to use them, which creates an account-specific flow based on the preset.

Template Sync System:
- When users clone a preset, the flow stores a reference to the source preset
- If the user hasn't customized the prompt/tools, the flow auto-updates when the preset changes
- If the user has customized, they get a notification that an update is available
"""

import logging
from dataclasses import dataclass
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import schemas
from preloop.models.crud import crud_flow
from preloop.models.db.session import get_session_factory
from preloop.models.models.flow import Flow
from preloop.flow_presets import FLOW_PRESETS
from preloop.utils.hashing import compute_content_hash

logger = logging.getLogger(__name__)


@dataclass
class PresetSyncResult:
    """Result of syncing a preset to its derived flows."""

    preset_id: UUID
    preset_name: str
    auto_updated: int  # Flows that were auto-updated
    notified: int  # Flows that got update notifications (customized)
    skipped: int  # Flows already up-to-date
    errors: int  # Flows that failed to update


def ensure_global_presets_exist(db: Session) -> List[schemas.FlowResponse]:
    """
    Ensure global flow presets exist in the database.

    Global presets have account_id=None and are available to all accounts.
    This function creates any missing presets but does not update existing ones.
    Use the sync_flow_presets.py script for updates.

    Returns:
        List of created flow preset responses
    """
    created_flows = []

    for preset_config in FLOW_PRESETS:
        preset_name = preset_config["name"]

        # Check if global preset already exists
        existing = crud_flow.get_global_preset_by_name(db, name=preset_name)
        if existing:
            logger.debug(f"Global preset '{preset_name}' already exists")
            continue

        try:
            # Create a copy of the preset config
            flow_data = preset_config.copy()

            # Global presets have no account_id
            flow_data.pop("account_id", None)

            # Presets are disabled by default - user enables after cloning
            flow_data["is_enabled"] = False
            flow_data["is_preset"] = True

            # Create the flow using Pydantic schema (account_id=None for global)
            flow_in = schemas.FlowCreate(**flow_data)
            flow = crud_flow.create(db=db, flow_in=flow_in, account_id=None)

            created_flows.append(flow)
            logger.info(f"Created global preset flow '{flow.name}'")

        except Exception as e:
            logger.error(
                f"Failed to create global preset '{preset_name}': {e}",
                exc_info=True,
            )
            # Continue with other presets even if one fails
            continue

    if created_flows:
        logger.info(f"Created {len(created_flows)} global preset flows")
    return created_flows


# Keep the old function name as an alias for backwards compatibility
# but it now just ensures global presets exist (doesn't create per-account copies)
def create_default_presets_for_account(
    db: Session,
    account_id: UUID,
    tracker_id: Optional[UUID] = None,
) -> List[schemas.FlowResponse]:
    """
    DEPRECATED: Presets are now global and not copied per-account.

    This function now just ensures global presets exist.
    Users should clone presets when they want to use them.

    Args:
        db: Database session
        account_id: Ignored (kept for backwards compatibility)
        tracker_id: Ignored (kept for backwards compatibility)

    Returns:
        List of created global preset flows (if any were missing)
    """
    logger.warning(
        "create_default_presets_for_account is deprecated. "
        "Presets are now global. Use ensure_global_presets_exist() instead."
    )
    return ensure_global_presets_exist(db)


def get_preset_names() -> List[str]:
    """Get list of available preset names."""
    return [preset["name"] for preset in FLOW_PRESETS]


def get_preset_by_name(name: str) -> Optional[dict]:
    """Get preset configuration by name."""
    for preset in FLOW_PRESETS:
        if preset["name"] == name:
            return preset.copy()
    return None


def ensure_global_presets_exist_background() -> None:
    """
    Background task-safe wrapper for ensuring global presets exist.

    Creates its own database session to avoid issues with request-scoped sessions
    being closed before the background task runs.
    """
    session_factory = get_session_factory()
    db = session_factory()
    try:
        ensure_global_presets_exist(db=db)
        db.commit()
        logger.info("Background task: Successfully ensured global presets exist")
    except Exception as e:
        logger.error(
            f"Background task: Failed to ensure global presets: {e}",
            exc_info=True,
        )
        db.rollback()
    finally:
        db.close()


# Keep old function for backwards compatibility
def create_default_presets_for_account_background(
    account_id: UUID,
    tracker_id: Optional[UUID] = None,
) -> None:
    """
    DEPRECATED: Use ensure_global_presets_exist_background() instead.

    This now just ensures global presets exist (ignores account_id and tracker_id).
    """
    logger.warning(
        "create_default_presets_for_account_background is deprecated. "
        "Presets are now global. Use ensure_global_presets_exist_background() instead."
    )
    ensure_global_presets_exist_background()


# =============================================================================
# Template Sync System
# =============================================================================


def link_unlinked_flows_by_content(db: Session, dry_run: bool = False) -> int:
    """
    Link unlinked flows to presets by prompt content hash.

    The name-based linking pass in scripts/sync_flow_presets.py only matches
    flows named "Copy of <preset name>". Flows that were cloned from a preset
    and then RENAMED (but never edited) end up with source_preset_id=NULL and
    silently never receive preset updates.

    This pass links any flow whose prompt_template is byte-identical to:
    - a preset's CURRENT prompt content, or
    - a preset's prompt content at some earlier link time (recovered from the
      distinct source_prompt_hash values of flows already linked to that
      preset — i.e. a pristine clone of an older preset version).

    Conservative semantics:
    - Only byte-identical prompts are linked (hash equality). A flow whose
      prompt was customized in any way can never match and is never touched,
      so user work can never be overwritten by this pass.
    - If a prompt hash matches more than one preset, the flow is skipped and
      the ambiguity is logged. No guess is made.
    - Linked flows get prompt_customized=False (identity is proven by the
      hash) and source_prompt_hash set to the matched (link-time) hash, so a
      flow matching an OLDER preset version is auto-updated by the next
      sync_preset_to_derived_flows run. Tools are handled exactly like the
      name-based path: source_tools_hash is the preset's current tools hash
      and tools_customized reflects whether the flow's tools differ (differing
      tools are notified, never overwritten).

    Args:
        db: Database session
        dry_run: If True, only log what would be linked without changes

    Returns:
        Number of flows linked (or that would be linked in dry_run)
    """
    presets = (
        db.query(Flow)
        .filter(
            Flow.is_preset.is_(True),
            Flow.account_id.is_(None),
        )
        .all()
    )
    if not presets:
        return 0

    # Map prompt-content hash -> {preset_id: preset} for current and
    # historical (link-time) preset prompt versions.
    hash_to_presets: dict = {}

    def _register(hash_value: str, preset: Flow) -> None:
        hash_to_presets.setdefault(hash_value, {})[preset.id] = preset

    presets_by_id = {preset.id: preset for preset in presets}
    for preset in presets:
        _register(compute_content_hash(preset.prompt_template), preset)

    # Historical (link-time) hashes for ALL presets in one query rather than
    # one query per preset.
    historical_rows = (
        db.query(Flow.source_preset_id, Flow.source_prompt_hash)
        .filter(
            Flow.source_preset_id.in_(presets_by_id.keys()),
            Flow.source_prompt_hash.isnot(None),
        )
        .distinct()
        .all()
    )
    for preset_id, historical_hash in historical_rows:
        _register(historical_hash, presets_by_id[preset_id])

    candidates = (
        db.query(Flow)
        .filter(
            Flow.is_preset.is_(False),
            Flow.account_id.isnot(None),
            Flow.source_preset_id.is_(None),
        )
        .all()
    )

    linked_count = 0
    for flow in candidates:
        flow_prompt_hash = compute_content_hash(flow.prompt_template)
        matches = hash_to_presets.get(flow_prompt_hash)
        if not matches:
            continue

        if len(matches) > 1:
            preset_names = sorted(p.name for p in matches.values())
            logger.warning(
                f"Flow {flow.id} ({flow.name}) prompt matches multiple presets "
                f"{preset_names}; skipping ambiguous content-hash link"
            )
            continue

        preset = next(iter(matches.values()))
        logger.info(
            f"Linking flow '{flow.name}' ({flow.id}) to preset "
            f"'{preset.name}' ({preset.id}) by prompt content hash"
        )
        linked_count += 1
        if dry_run:
            continue

        flow.source_preset_id = preset.id
        # The matched hash IS the flow's current prompt hash, so the prompt is
        # provably unmodified since link time: not customized. If it matched an
        # older preset version, the hash mismatch against the preset's current
        # content lets sync_preset_to_derived_flows auto-update it.
        flow.source_prompt_hash = flow_prompt_hash
        flow.prompt_customized = False

        # Tools: identical to the name-based linking path.
        flow.source_tools_hash = compute_content_hash(preset.allowed_mcp_tools or [])
        current_flow_tools_hash = compute_content_hash(flow.allowed_mcp_tools or [])
        flow.tools_customized = current_flow_tools_hash != flow.source_tools_hash
        flow.preset_update_available = flow.prompt_customized or flow.tools_customized

        db.add(flow)

    if not dry_run and linked_count:
        db.commit()

    logger.info(
        f"Content-hash linking: {'would link' if dry_run else 'linked'} "
        f"{linked_count} flows to their source presets"
    )
    return linked_count


def sync_preset_to_derived_flows(db: Session, preset_id: UUID) -> PresetSyncResult:
    """
    Sync a preset's changes to all flows derived from it.

    For flows that haven't customized the prompt/tools:
    - Auto-update them with the new preset content

    For flows that have customized the prompt/tools:
    - Set preset_update_available=True so they get a notification

    Args:
        db: Database session
        preset_id: ID of the preset to sync

    Returns:
        PresetSyncResult with counts of updated/notified/skipped flows
    """
    preset = crud_flow.get(db, id=preset_id)
    if not preset or not preset.is_preset:
        raise ValueError(f"Preset {preset_id} not found or is not a preset")

    # Compute current preset hashes
    current_prompt_hash = compute_content_hash(preset.prompt_template)
    current_tools_hash = compute_content_hash(preset.allowed_mcp_tools or [])

    # Find all flows derived from this preset
    derived_flows = db.query(Flow).filter(Flow.source_preset_id == preset_id).all()

    result = PresetSyncResult(
        preset_id=preset_id,
        preset_name=preset.name,
        auto_updated=0,
        notified=0,
        skipped=0,
        errors=0,
    )

    for flow in derived_flows:
        try:
            # Check if flow is already up to date
            prompt_up_to_date = flow.source_prompt_hash == current_prompt_hash
            tools_up_to_date = flow.source_tools_hash == current_tools_hash

            if prompt_up_to_date and tools_up_to_date:
                # Flow is already synced with preset
                result.skipped += 1
                continue

            # Determine what needs updating
            needs_prompt_update = not prompt_up_to_date and not flow.prompt_customized
            needs_tools_update = not tools_up_to_date and not flow.tools_customized

            # Check if user has customized any out-of-date fields
            has_customized_outdated = (
                not prompt_up_to_date and flow.prompt_customized
            ) or (not tools_up_to_date and flow.tools_customized)

            if has_customized_outdated:
                # User customized fields that have updates - notify them
                flow.preset_update_available = True
                result.notified += 1
                logger.info(
                    f"Flow {flow.id} ({flow.name}) has customizations - "
                    f"notifying of available update"
                )

            # Auto-update non-customized fields
            if needs_prompt_update:
                flow.prompt_template = preset.prompt_template
                flow.source_prompt_hash = current_prompt_hash
                logger.debug(f"Auto-updated prompt for flow {flow.id}")

            if needs_tools_update:
                flow.allowed_mcp_tools = preset.allowed_mcp_tools
                flow.source_tools_hash = current_tools_hash
                logger.debug(f"Auto-updated tools for flow {flow.id}")

            if needs_prompt_update or needs_tools_update:
                result.auto_updated += 1
                logger.info(
                    f"Auto-updated flow {flow.id} ({flow.name}) from preset {preset.name}"
                )

        except Exception as e:
            result.errors += 1
            logger.error(
                f"Failed to sync flow {flow.id} from preset {preset_id}: {e}",
                exc_info=True,
            )

    db.commit()

    logger.info(
        f"Synced preset '{preset.name}': "
        f"{result.auto_updated} auto-updated, "
        f"{result.notified} notified, "
        f"{result.skipped} skipped, "
        f"{result.errors} errors"
    )

    return result


def sync_all_presets(db: Session) -> List[PresetSyncResult]:
    """
    Sync all global presets to their derived flows.

    This is typically called after deploying new preset versions.

    Args:
        db: Database session

    Returns:
        List of PresetSyncResult for each preset
    """
    results = []

    # Get all global presets
    presets = (
        db.query(Flow)
        .filter(
            Flow.is_preset,
            Flow.account_id.is_(None),
        )
        .all()
    )

    for preset in presets:
        try:
            result = sync_preset_to_derived_flows(db, preset.id)
            results.append(result)
        except Exception as e:
            logger.error(
                f"Failed to sync preset {preset.name} ({preset.id}): {e}",
                exc_info=True,
            )

    return results


def sync_all_presets_background() -> List[PresetSyncResult]:
    """
    Background task-safe wrapper for syncing all presets.

    Creates its own database session to avoid issues with request-scoped sessions.
    """
    session_factory = get_session_factory()
    db = session_factory()
    try:
        results = sync_all_presets(db)
        logger.info(f"Background task: Synced {len(results)} presets")
        return results
    except Exception as e:
        logger.error(
            f"Background task: Failed to sync presets: {e}",
            exc_info=True,
        )
        db.rollback()
        return []
    finally:
        db.close()


def apply_preset_update_to_flow(db: Session, flow_id: UUID) -> Flow:
    """
    Apply pending preset update to a flow (user action).

    This is called when a user clicks "Apply Update" on a flow that has
    preset_update_available=True. It updates the flow with the latest
    preset content, overwriting their customizations.

    Args:
        db: Database session
        flow_id: ID of the flow to update

    Returns:
        Updated flow object

    Raises:
        ValueError: If flow not found or has no source preset
    """
    flow = crud_flow.get(db, id=flow_id)
    if not flow:
        raise ValueError(f"Flow {flow_id} not found")

    if not flow.source_preset_id:
        raise ValueError(f"Flow {flow_id} is not linked to a preset")

    preset = crud_flow.get(db, id=flow.source_preset_id)
    if not preset:
        raise ValueError(f"Source preset {flow.source_preset_id} not found")

    # Security: Verify the source is actually a preset (not an arbitrary flow)
    if not preset.is_preset:
        logger.warning(
            f"Security: Flow {flow_id} has source_preset_id={flow.source_preset_id} "
            f"which is not a preset (is_preset=False). Refusing to apply."
        )
        raise ValueError(
            f"Source flow {flow.source_preset_id} is not a preset. "
            "Cannot apply updates from non-preset flows."
        )

    # Security: Verify the preset is accessible to this flow's account
    # Presets must be either global (account_id is None) or belong to the same account
    if preset.account_id is not None and preset.account_id != flow.account_id:
        logger.warning(
            f"Security: Flow {flow_id} (account={flow.account_id}) attempted to apply "
            f"preset {preset.id} (account={preset.account_id}). Access denied."
        )
        raise ValueError(
            f"Preset {flow.source_preset_id} is not accessible to this account."
        )

    # Update flow with preset content
    flow.prompt_template = preset.prompt_template
    flow.allowed_mcp_tools = preset.allowed_mcp_tools

    # Update hashes
    flow.source_prompt_hash = compute_content_hash(preset.prompt_template)
    flow.source_tools_hash = compute_content_hash(preset.allowed_mcp_tools or [])

    # Clear customization flags and update notification
    flow.prompt_customized = False
    flow.tools_customized = False
    flow.preset_update_available = False

    db.commit()
    db.refresh(flow)

    logger.info(
        f"Applied preset update to flow {flow.id} ({flow.name}) "
        f"from preset {preset.name}"
    )

    return flow


def dismiss_preset_update(db: Session, flow_id: UUID) -> Flow:
    """
    Dismiss the preset update notification for a flow.

    This is called when a user clicks "Dismiss" on the update notification.
    The notification won't reappear until the preset changes again.

    Args:
        db: Database session
        flow_id: ID of the flow

    Returns:
        Updated flow object
    """
    flow = crud_flow.get(db, id=flow_id)
    if not flow:
        raise ValueError(f"Flow {flow_id} not found")

    flow.preset_update_available = False
    db.commit()
    db.refresh(flow)

    logger.info(f"Dismissed preset update notification for flow {flow.id}")

    return flow
