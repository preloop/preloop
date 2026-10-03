"""Service for managing policy version snapshots.

This service provides version control functionality for policy configurations:
- Create snapshots of the current policy state
- Compare versions to see differences
- Rollback to previous versions
- Manage version tags
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud.policy_snapshot import crud_policy_snapshot
from preloop.models.models.policy_snapshot import PolicySnapshot
from preloop.models.schemas.mcp_server import redact_snapshot_credentials
from preloop.services.policy import (
    PolicyApplier,
    PolicyDiffResult,
    PolicyDocument,
    compute_policy_diff,
    export_current_policy,
)

logger = logging.getLogger(__name__)


class PolicyVersionService:
    """Service for managing policy version snapshots.

    This service handles:
    - Creating snapshots from current state
    - Applying snapshots (rollback)
    - Version numbering
    - Tag management
    """

    def __init__(self, db: Session, account_id: str):
        """Initialize the service.

        Args:
            db: SQLAlchemy database session.
            account_id: The account ID to manage versions for.
        """
        self.db = db
        self.account_id = account_id

    def create_snapshot(
        self,
        description: Optional[str] = None,
        tag: Optional[str] = None,
        user_id: Optional[UUID] = None,
        set_active: bool = True,
    ) -> PolicySnapshot:
        """Create a snapshot of the current policy state.

        Exports the current MCP servers, approval workflows, tool configurations,
        and defaults into a versioned snapshot.

        Args:
            description: Optional description of the snapshot.
            tag: Optional tag for the snapshot (e.g., "production", "v1.0").
            user_id: The user creating the snapshot.
            set_active: Whether to set this as the active version.

        Returns:
            The created PolicySnapshot.
        """
        # Export current policy state
        # IMPORTANT: include_credentials=True for internal snapshots
        # so rollbacks preserve MCP server credentials
        policy = export_current_policy(
            self.db,
            account_id=self.account_id,
            policy_name=f"Snapshot {datetime.now(timezone.utc).isoformat()}",
            include_credentials=True,
        )

        # Get counts for summary
        mcp_servers_count = len(policy.mcp_servers or [])
        policies_count = len(policy.approval_workflows or [])
        tools_count = len(policy.tools or [])

        # Convert to JSON-serializable dict
        snapshot_data = policy.model_dump(exclude_none=True, mode="json")

        # Get next version number
        version_number = crud_policy_snapshot.get_next_version_number(
            self.db, self.account_id
        )

        # Clear tag from other snapshots if provided
        if tag:
            crud_policy_snapshot.clear_tag(self.db, self.account_id, tag)

        # Create the snapshot
        snapshot = PolicySnapshot(
            account_id=self.account_id,
            version_number=version_number,
            tag=tag,
            description=description,
            snapshot_data=snapshot_data,
            created_by_user_id=user_id,
            is_active=set_active,
            mcp_servers_count=mcp_servers_count,
            policies_count=policies_count,
            tools_count=tools_count,
        )

        # If setting as active, deactivate others
        if set_active:
            crud_policy_snapshot.set_active(self.db, self.account_id, snapshot.id)
            # The above won't work since snapshot doesn't have id yet
            # We'll handle this after add/flush
            self.db.query(PolicySnapshot).filter(
                PolicySnapshot.account_id == self.account_id,
                PolicySnapshot.is_active == True,  # noqa: E712
            ).update({"is_active": False})

        self.db.add(snapshot)
        self.db.flush()

        logger.info(
            f"Created policy snapshot v{version_number} for account {self.account_id}"
        )

        return snapshot

    def get_snapshot(self, snapshot_id: UUID) -> Optional[PolicySnapshot]:
        """Get a specific snapshot by ID.

        Args:
            snapshot_id: The ID of the snapshot.

        Returns:
            The PolicySnapshot if found, otherwise None.
        """
        return crud_policy_snapshot.get(self.db, snapshot_id, self.account_id)

    def list_snapshots(
        self,
        limit: int = 100,
        offset: int = 0,
        include_snapshots: bool = False,
    ) -> List[PolicySnapshot]:
        """List all snapshots for the account.

        Args:
            limit: Maximum number of snapshots to return.
            offset: Number of snapshots to skip.
            include_snapshots: Whether to include full snapshot data.

        Returns:
            List of PolicySnapshot objects.
        """
        return crud_policy_snapshot.get_multi_by_account(
            self.db,
            account_id=self.account_id,
            skip=offset,
            limit=limit,
            include_snapshots=include_snapshots,
        )

    def compute_rollback_diff(
        self, snapshot_id: UUID
    ) -> Tuple[Optional[PolicyDiffResult], Optional[str]]:
        """Compute the diff between current state and a snapshot.

        Args:
            snapshot_id: The ID of the snapshot to compare against.

        Returns:
            Tuple of (PolicyDiffResult, error_message).
            If successful, error_message is None.
        """
        snapshot = crud_policy_snapshot.get(self.db, snapshot_id, self.account_id)
        if not snapshot:
            return None, "Snapshot not found"

        # Get current policy
        current_policy = export_current_policy(
            self.db,
            account_id=self.account_id,
            policy_name="Current Configuration",
        )

        # Load snapshot policy. The diff is returned to callers, so diff a
        # copy with MCP server credentials masked; rollback reloads the
        # stored snapshot itself.
        snapshot_policy = PolicyDocument.model_validate(
            redact_snapshot_credentials(snapshot.snapshot_data)
        )

        # Compute diff (from current to snapshot, showing what would change)
        diff = compute_policy_diff(current_policy, snapshot_policy)

        return diff, None

    def rollback_to_snapshot(
        self,
        snapshot_id: UUID,
        preview_only: bool = False,
    ) -> Tuple[Optional[PolicyDiffResult], bool, Optional[str]]:
        """Rollback to a previous snapshot.

        Args:
            snapshot_id: The ID of the snapshot to rollback to.
            preview_only: If True, only compute the diff without applying.

        Returns:
            Tuple of (diff, success, error_message).
        """
        snapshot = crud_policy_snapshot.get(self.db, snapshot_id, self.account_id)
        if not snapshot:
            return None, False, "Snapshot not found"

        # Compute diff first
        diff, error = self.compute_rollback_diff(snapshot_id)
        if error:
            return None, False, error

        if preview_only:
            return diff, True, None

        # Apply the snapshot
        try:
            # Load the snapshot as a PolicyDocument
            snapshot_policy = PolicyDocument.model_validate(snapshot.snapshot_data)

            # Apply using PolicyApplier
            applier = PolicyApplier(self.db, account_id=self.account_id)
            result = applier.apply(snapshot_policy, dry_run=False, resolve_env=False)

            if not result.success:
                return diff, False, f"Failed to apply snapshot: {result.errors}"

            # Set this snapshot as active
            crud_policy_snapshot.set_active(self.db, self.account_id, snapshot_id)

            logger.info(
                f"Rolled back to snapshot v{snapshot.version_number} "
                f"for account {self.account_id}"
            )

            return diff, True, None

        except Exception as e:
            logger.error(f"Failed to rollback to snapshot: {e}", exc_info=True)
            return diff, False, str(e)

    def update_tag(
        self, snapshot_id: UUID, tag: Optional[str]
    ) -> Tuple[Optional[PolicySnapshot], Optional[str]]:
        """Update the tag on a snapshot.

        Args:
            snapshot_id: The ID of the snapshot.
            tag: The new tag value (or None to clear).

        Returns:
            Tuple of (updated_snapshot, error_message).
        """
        snapshot = crud_policy_snapshot.update_tag(
            self.db, snapshot_id, self.account_id, tag
        )
        if not snapshot:
            return None, "Snapshot not found"

        return snapshot, None

    def remove_tag(
        self, snapshot_id: UUID
    ) -> Tuple[Optional[PolicySnapshot], Optional[str]]:
        """Remove the tag from a snapshot.

        Args:
            snapshot_id: The ID of the snapshot.

        Returns:
            Tuple of (updated_snapshot, error_message).
        """
        return self.update_tag(snapshot_id, None)

    def delete_snapshot(self, snapshot_id: UUID) -> Tuple[bool, Optional[str]]:
        """Delete a snapshot.

        Cannot delete the active snapshot.

        Args:
            snapshot_id: The ID of the snapshot to delete.

        Returns:
            Tuple of (success, error_message).
        """
        snapshot = crud_policy_snapshot.get(self.db, snapshot_id, self.account_id)
        if not snapshot:
            return False, "Snapshot not found"

        if snapshot.is_active:
            return False, "Cannot delete the active snapshot"

        crud_policy_snapshot.remove(self.db, id=snapshot_id, account_id=self.account_id)
        logger.info(
            f"Deleted snapshot v{snapshot.version_number} for account {self.account_id}"
        )

        return True, None

    def prune_snapshots(
        self,
        older_than_days: int = 90,
        keep_tagged: bool = True,
        keep_count: int = 10,
    ) -> int:
        """Delete old unused snapshots.

        Args:
            older_than_days: Delete snapshots older than this many days.
            keep_tagged: If True, never delete tagged snapshots.
            keep_count: Always keep at least this many snapshots.

        Returns:
            The count of deleted snapshots.
        """
        count = crud_policy_snapshot.prune_old_versions(
            self.db,
            account_id=self.account_id,
            older_than_days=older_than_days,
            keep_tagged=keep_tagged,
            keep_count=keep_count,
        )

        logger.info(f"Pruned {count} old snapshots for account {self.account_id}")

        return count

    def get_active_snapshot(self) -> Optional[PolicySnapshot]:
        """Get the currently active snapshot.

        Returns:
            The active PolicySnapshot if found, otherwise None.
        """
        return crud_policy_snapshot.get_active(self.db, self.account_id)

    def get_snapshot_by_tag(self, tag: str) -> Optional[PolicySnapshot]:
        """Get a snapshot by its tag.

        Args:
            tag: The tag to search for.

        Returns:
            The PolicySnapshot if found, otherwise None.
        """
        return crud_policy_snapshot.get_by_tag(self.db, self.account_id, tag)
