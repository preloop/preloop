#!/usr/bin/env python3
"""
Script to sync global flow presets.

Global presets are system-wide templates (account_id=None) that are available
to all accounts. Users clone these presets to create account-specific flows.

Usage:
    # Sync global presets (create/update)
    python scripts/sync_flow_presets.py

    # Dry run (no changes)
    python scripts/sync_flow_presets.py --dry-run

    # Cleanup: Delete account-specific presets that overlap with global presets
    python scripts/sync_flow_presets.py --cleanup

    # Cleanup with dry run (show what would be deleted)
    python scripts/sync_flow_presets.py --cleanup --dry-run

Environment Variables:
    PRELOOP_PRESETS_PATH: os.pathsep-separated list of preset directories,
                          loaded in order (later dirs override earlier ones
                          on slug collision; union otherwise). Defaults to
                          the open-source presets directory. Set to
                          "/app/backend/presets:/app/presets" in the EE
                          Docker image to layer EE presets on top of OSS.
"""

import argparse
import logging
import sys

from dotenv import load_dotenv
from typing import List, Tuple

from sqlalchemy.orm import Session

from preloop.models.db.session import get_db_session
from preloop.models.crud.flow import CRUDFlow
from preloop.models.models.flow import Flow
from preloop.models import schemas
from preloop.flow_presets import FLOW_PRESETS, PRESETS_DIRS
from preloop.services.flow_presets_service import (
    compute_content_hash,
    link_unlinked_flows_by_content,
    sync_preset_to_derived_flows,
    PresetSyncResult,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

logger.info(f"Loading presets from: {[str(d) for d in PRESETS_DIRS]}")
logger.info(f"Found {len(FLOW_PRESETS)} preset definitions")


def sync_global_presets(db: Session, dry_run: bool = False) -> int:
    """
    Sync global flow presets (account_id=None).

    Creates new presets and updates existing ones to match the preset definitions.

    Args:
        db: Database session
        dry_run: If True, only log what would be done without making changes

    Returns:
        Number of presets created/updated
    """
    crud_flow = CRUDFlow()
    changes_count = 0

    # Get existing global presets
    existing_global_presets = crud_flow.get_global_presets(db, limit=1000)
    existing_by_name = {flow.name: flow for flow in existing_global_presets}

    logger.info(f"Found {len(existing_by_name)} existing global presets")

    # Process each preset definition
    for preset_def in FLOW_PRESETS:
        preset_name = preset_def["name"]
        existing_flow = existing_by_name.get(preset_name)

        if existing_flow:
            # Check if preset needs updating
            needs_update = False
            update_fields = []

            # Compare key fields that might have changed
            if existing_flow.description != preset_def.get("description"):
                needs_update = True
                update_fields.append("description")
            if existing_flow.icon != preset_def.get("icon"):
                needs_update = True
                update_fields.append("icon")
            if existing_flow.prompt_template != preset_def.get("prompt_template"):
                needs_update = True
                update_fields.append("prompt_template")
            if existing_flow.agent_type != preset_def.get("agent_type"):
                needs_update = True
                update_fields.append("agent_type")
            if existing_flow.agent_config != preset_def.get("agent_config"):
                needs_update = True
                update_fields.append("agent_config")
            if existing_flow.allowed_mcp_tools != preset_def.get("allowed_mcp_tools"):
                needs_update = True
                update_fields.append("allowed_mcp_tools")
            if existing_flow.git_clone_config != preset_def.get("git_clone_config"):
                needs_update = True
                update_fields.append("git_clone_config")
            if existing_flow.notifications != preset_def.get("notifications"):
                needs_update = True
                update_fields.append("notifications")

            # Check if preset incorrectly has an account_id (should be None)
            if existing_flow.account_id is not None:
                needs_update = True
                update_fields.append("account_id (should be NULL)")
                logger.warning(
                    f"  Global preset '{preset_name}' has account_id={existing_flow.account_id}, will be fixed"
                )

            if needs_update:
                logger.info(
                    f"  Updating global preset '{preset_name}' (fields: {', '.join(update_fields)})"
                )
                if not dry_run:
                    # Update the preset - set account_id to None explicitly
                    update_data = {
                        "name": preset_name,
                        "description": preset_def.get("description"),
                        "icon": preset_def.get("icon"),
                        "prompt_template": preset_def.get("prompt_template"),
                        "agent_type": preset_def.get("agent_type"),
                        "agent_config": preset_def.get("agent_config"),
                        "allowed_mcp_servers": preset_def.get(
                            "allowed_mcp_servers", []
                        ),
                        "allowed_mcp_tools": preset_def.get("allowed_mcp_tools", []),
                        "git_clone_config": preset_def.get("git_clone_config"),
                        "notifications": preset_def.get("notifications"),
                        "trigger_event_source": preset_def.get("trigger_event_source"),
                        "trigger_event_type": preset_def.get("trigger_event_type"),
                        "trigger_config": preset_def.get("trigger_config"),
                        "is_preset": True,
                        "is_enabled": False,  # Global presets are always disabled
                    }
                    # Update directly to ensure account_id stays NULL
                    for field, value in update_data.items():
                        if hasattr(existing_flow, field):
                            setattr(existing_flow, field, value)
                    existing_flow.account_id = None  # Ensure it's NULL
                    db.add(existing_flow)
                    db.commit()
                changes_count += 1
            else:
                logger.debug(f"  Global preset '{preset_name}' is up to date")
        else:
            # Create new global preset
            logger.info(f"  Creating new global preset '{preset_name}'")
            if not dry_run:
                # Prepare preset data
                preset_data = preset_def.copy()
                preset_data.pop("account_id", None)  # Ensure no account_id
                preset_data["trigger_event_source"] = None
                preset_data["trigger_event_type"] = preset_def.get("trigger_event_type")
                preset_data["is_preset"] = True
                preset_data["is_enabled"] = False

                flow_create = schemas.FlowCreate(**preset_data)
                crud_flow.create(db=db, flow_in=flow_create, account_id=None)
            changes_count += 1

    logger.info(f"Total global preset changes: {changes_count}")
    return changes_count


def find_account_presets_overlapping_global(
    db: Session,
) -> List[Tuple[Flow, str]]:
    """
    Find account-specific presets that have the same name as global presets.

    These are problematic because they shadow the global presets and should
    be cleaned up.

    Returns:
        List of (flow, preset_name) tuples for overlapping presets
    """
    global_preset_names = {preset["name"] for preset in FLOW_PRESETS}

    # Find all account-specific flows that are presets with names matching global presets
    overlapping = []
    query = db.query(Flow).filter(
        Flow.is_preset,
        Flow.account_id.isnot(None),
        Flow.name.in_(global_preset_names),
    )

    for flow in query.all():
        overlapping.append((flow, flow.name))

    return overlapping


def cleanup_overlapping_presets(db: Session, dry_run: bool = False) -> int:
    """
    Delete account-specific presets that overlap with global presets.

    Args:
        db: Database session
        dry_run: If True, only log what would be done without making changes

    Returns:
        Number of presets deleted
    """
    overlapping = find_account_presets_overlapping_global(db)

    if not overlapping:
        logger.info("No overlapping account-specific presets found")
        return 0

    logger.info(
        f"Found {len(overlapping)} account-specific presets overlapping with global presets:"
    )
    for flow, preset_name in overlapping:
        logger.info(f"  - '{preset_name}' (account_id={flow.account_id}, id={flow.id})")

    if dry_run:
        logger.info("DRY RUN - No presets will be deleted")
        return len(overlapping)

    # Confirm deletion
    print(f"\nThis will delete {len(overlapping)} account-specific presets.")
    print("These presets overlap with global presets and should be removed.")
    print("Users will still have access to the global presets after deletion.")
    response = input("\nProceed with deletion? [y/N]: ").strip().lower()

    if response != "y":
        logger.info("Deletion cancelled by user")
        return 0

    deleted_count = 0
    for flow, preset_name in overlapping:
        logger.info(f"  Deleting '{preset_name}' (id={flow.id})")
        db.delete(flow)
        deleted_count += 1

    db.commit()
    logger.info(f"Deleted {deleted_count} overlapping account-specific presets")
    return deleted_count


def link_existing_flows_to_presets(db: Session, dry_run: bool = False) -> int:
    """
    Link existing flows to their source presets by matching name patterns.

    This is a one-time migration for flows created before template tracking
    was implemented. It matches flows named "Copy of {preset_name}" to their
    corresponding presets.

    Args:
        db: Database session
        dry_run: If True, only log what would be done without making changes

    Returns:
        Number of flows linked
    """
    crud_flow = CRUDFlow()
    linked_count = 0

    # Get all global presets
    global_presets = crud_flow.get_global_presets(db, limit=1000)
    preset_by_name = {preset.name: preset for preset in global_presets}

    logger.info(f"Found {len(preset_by_name)} global presets to match against")

    # Find flows that might be clones of presets
    # Pattern: "Copy of {preset_name}" or "Copy of {preset_name} (N)"
    for preset_name, preset in preset_by_name.items():
        # Find flows matching the clone pattern
        pattern_base = f"Copy of {preset_name}"

        # Query flows that start with "Copy of {preset_name}"
        matching_flows = (
            db.query(Flow)
            .filter(
                Flow.name.like(f"{pattern_base}%"),
                Flow.is_preset.is_(False),
                Flow.account_id.isnot(None),
                Flow.source_preset_id.is_(None),  # Not already linked
            )
            .all()
        )

        for flow in matching_flows:
            # Verify the name matches exactly or has suffix like " (2)"
            if flow.name == pattern_base or flow.name.startswith(f"{pattern_base} ("):
                logger.info(
                    f"  Linking flow '{flow.name}' (id={flow.id}) "
                    f"to preset '{preset_name}' (id={preset.id})"
                )

                if not dry_run:
                    # Link the flow to the preset
                    flow.source_preset_id = preset.id

                    # Compute hashes based on current preset content
                    flow.source_prompt_hash = compute_content_hash(
                        preset.prompt_template
                    )
                    flow.source_tools_hash = compute_content_hash(
                        preset.allowed_mcp_tools or []
                    )

                    # Check if flow has been customized (prompt differs from preset)
                    current_flow_prompt_hash = compute_content_hash(
                        flow.prompt_template
                    )
                    current_flow_tools_hash = compute_content_hash(
                        flow.allowed_mcp_tools or []
                    )

                    flow.prompt_customized = (
                        current_flow_prompt_hash != flow.source_prompt_hash
                    )
                    flow.tools_customized = (
                        current_flow_tools_hash != flow.source_tools_hash
                    )

                    # If customized but outdated, set update available flag
                    flow.preset_update_available = (
                        flow.prompt_customized or flow.tools_customized
                    )

                    db.add(flow)

                    if flow.prompt_customized or flow.tools_customized:
                        logger.info(
                            f"    Flow has customizations - "
                            f"prompt: {flow.prompt_customized}, "
                            f"tools: {flow.tools_customized}"
                        )

                linked_count += 1

    if not dry_run:
        db.commit()

    logger.info(f"Linked {linked_count} existing flows by name pattern")

    # Second pass: link renamed-but-unmodified clones by prompt content hash.
    # The name pattern above misses flows that were renamed after cloning;
    # a byte-identical prompt is proof of origin regardless of the name.
    linked_count += link_unlinked_flows_by_content(db, dry_run=dry_run)

    logger.info(f"Linked {linked_count} existing flows to their source presets")
    return linked_count


def sync_derived_flows(db: Session, dry_run: bool = False) -> List[PresetSyncResult]:
    """
    Sync all derived flows with their source presets.

    This propagates preset changes to flows that haven't been customized,
    and sets update notifications for flows that have been customized.

    Args:
        db: Database session
        dry_run: If True, only log what would be done without making changes

    Returns:
        List of sync results per preset
    """
    results = []

    # Before propagating, link any unlinked flows whose prompt is
    # byte-identical to a preset (renamed clones that the name-based
    # one-time migration missed). Safe: only exact-content matches.
    link_unlinked_flows_by_content(db, dry_run=dry_run)

    # Get all global presets
    presets = (
        db.query(Flow)
        .filter(
            Flow.is_preset.is_(True),
            Flow.account_id.is_(None),
        )
        .all()
    )

    logger.info(f"Syncing derived flows for {len(presets)} presets")

    for preset in presets:
        try:
            if dry_run:
                # Count what would be affected
                derived = (
                    db.query(Flow).filter(Flow.source_preset_id == preset.id).count()
                )
                logger.info(
                    f"  Preset '{preset.name}': {derived} derived flows would be checked"
                )
            else:
                result = sync_preset_to_derived_flows(db, preset.id)
                results.append(result)
                logger.info(
                    f"  Preset '{preset.name}': "
                    f"{result.auto_updated} auto-updated, "
                    f"{result.notified} notified, "
                    f"{result.skipped} skipped"
                )
        except Exception as e:
            logger.error(f"  Failed to sync preset '{preset.name}': {e}")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Sync global flow presets and manage derived flows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Sync global presets (create/update) and propagate to derived flows
    python scripts/sync_flow_presets.py

    # Dry run (show what would be done)
    python scripts/sync_flow_presets.py --dry-run

    # Only sync global presets, don't propagate to derived flows
    python scripts/sync_flow_presets.py --no-propagate

    # Link existing flows to their source presets (one-time migration)
    python scripts/sync_flow_presets.py --link-existing

    # Cleanup overlapping account-specific presets
    python scripts/sync_flow_presets.py --cleanup

    # Cleanup with dry run
    python scripts/sync_flow_presets.py --cleanup --dry-run
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete account-specific presets that overlap with global presets (requires confirmation)",
    )
    parser.add_argument(
        "--link-existing",
        action="store_true",
        help="Link existing flows to presets by matching name patterns (one-time migration)",
    )
    parser.add_argument(
        "--no-propagate",
        action="store_true",
        help="Don't propagate preset changes to derived flows",
    )

    args = parser.parse_args()

    # Get database session
    db_gen = get_db_session()
    db = next(db_gen)

    try:
        if args.dry_run:
            logger.info("DRY RUN MODE - No changes will be made")

        if args.cleanup:
            # Cleanup mode: delete overlapping account-specific presets
            deleted = cleanup_overlapping_presets(db, dry_run=args.dry_run)
            if not args.dry_run and deleted > 0:
                logger.info(f"Cleanup complete: deleted {deleted} overlapping presets")

        elif args.link_existing:
            # Link existing flows to their source presets by name pattern
            logger.info("Linking existing flows to presets by name pattern...")
            linked = link_existing_flows_to_presets(db, dry_run=args.dry_run)
            if not args.dry_run:
                logger.info(
                    f"Successfully linked {linked} flows to their source presets"
                )

        else:
            # Normal mode: sync global presets
            changes = sync_global_presets(db, dry_run=args.dry_run)
            if not args.dry_run:
                logger.info(
                    f"Successfully synced global flow presets ({changes} changes)"
                )

            # Propagate changes to derived flows (unless --no-propagate)
            if not args.no_propagate:
                logger.info("\nPropagating preset changes to derived flows...")
                results = sync_derived_flows(db, dry_run=args.dry_run)

                if not args.dry_run:
                    total_updated = sum(r.auto_updated for r in results)
                    total_notified = sum(r.notified for r in results)
                    logger.info(
                        f"Propagation complete: {total_updated} flows auto-updated, "
                        f"{total_notified} flows notified of available updates"
                    )

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
