"""CRUD operations for Tracker model."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..models.tracker import Tracker, TrackerType
from .base import CRUDBase

TRACKER_API_KEY_SECRET_KIND = "tracker_api_key"
TRACKER_WEBHOOK_SECRET_KIND = "tracker_webhook_secret"

# Key under Tracker.meta_data holding per-project unknown-project failure
# state: {"<project_identifier>": {"attempts": int, "last_attempt_at": iso,
# "first_seen_at": iso, "last_seen_at": iso, "degraded": bool}}.
UNKNOWN_PROJECTS_META_KEY = "unknown_projects"

# Backoff schedule for re-syncing a tracker after a webhook named a project we
# do not know about. Doubles per attempt: 5m, 10m, 20m, 40m, capped at 1h.
UNKNOWN_PROJECT_BASE_BACKOFF = timedelta(minutes=5)
UNKNOWN_PROJECT_MAX_BACKOFF = timedelta(hours=1)

# After this many failed sync attempts the project is considered degraded and
# we stop triggering syncs, surfacing it to the user instead.
UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED = 5


@dataclass(frozen=True)
class UnknownProjectState:
    """Outcome of recording a webhook for a project we cannot resolve.

    Attributes:
        should_sync: True when the caller should trigger a tracker sync now.
        attempts: Number of sync attempts made for this project so far.
        degraded: True once retries are exhausted and the project needs user
            attention (repo outside the integration's scope).
        retry_after_seconds: Backoff before the next attempt is due.
    """

    should_sync: bool
    attempts: int
    degraded: bool
    retry_after_seconds: int


def _parse_timestamp(raw: Any) -> Optional[datetime]:
    """Parse an ISO timestamp from meta_data, tolerating junk/naive values."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def store_tracker_secret(
    db: Session,
    *,
    account_id: Optional[str],
    name: str,
    secret_value: str,
    secret_kind: str,
    existing_secret_id: Optional[Any] = None,
) -> Any:
    """Encrypt a tracker credential into a SecretReference and return its id.

    Shared by the CRUD layer and the tracker API endpoints (which construct
    trackers via the ORM directly) so credentials are never persisted as
    plaintext regardless of the write path.
    """
    from preloop.services.secret_service import get_secret_service

    secret_ref = get_secret_service().create_local_secret_reference(
        db,
        account_id=account_id,
        name=name,
        secret_kind=secret_kind,
        secret_value=str(secret_value),
        existing_secret_id=existing_secret_id,
    )
    return secret_ref.id


class CRUDTracker(CRUDBase[Tracker]):
    """CRUD operations for Tracker model."""

    @staticmethod
    def _encrypt_credential_fields(
        db: Session,
        *,
        obj_data: Dict[str, Any],
        account_id: Optional[str],
        tracker_name: str,
        existing_api_secret_id: Optional[Any] = None,
        existing_webhook_secret_id: Optional[Any] = None,
    ) -> None:
        """Route incoming credential fields into encrypted SecretReferences.

        Mirrors the AIModel credential path so tracker tokens are never stored
        as plaintext at rest. Mutates ``obj_data`` in place: the plaintext
        columns are cleared and the ``*_secret_id`` FKs are set.
        """
        if "api_key" in obj_data:
            if obj_data.get("api_key"):
                obj_data["credentials_secret_id"] = store_tracker_secret(
                    db,
                    account_id=account_id,
                    name=f"{tracker_name} API key",
                    secret_value=obj_data["api_key"],
                    secret_kind=TRACKER_API_KEY_SECRET_KIND,
                    existing_secret_id=existing_api_secret_id,
                )
            else:
                obj_data["credentials_secret_id"] = None
            obj_data["api_key"] = None

        if "jira_webhook_secret" in obj_data:
            if obj_data.get("jira_webhook_secret"):
                obj_data["webhook_secret_id"] = store_tracker_secret(
                    db,
                    account_id=account_id,
                    name=f"{tracker_name} webhook secret",
                    secret_value=obj_data["jira_webhook_secret"],
                    secret_kind=TRACKER_WEBHOOK_SECRET_KIND,
                    existing_secret_id=existing_webhook_secret_id,
                )
            else:
                obj_data["webhook_secret_id"] = None
            obj_data["jira_webhook_secret"] = None

    def create(self, db: Session, *, obj_in: Dict[str, Any]) -> Tracker:
        """Create new tracker with initialized timestamp fields."""
        obj_data = dict(obj_in)

        # Initialize timestamp fields
        current_time = datetime.now(timezone.utc)
        obj_data.setdefault("created", current_time)
        obj_data.setdefault("last_updated", current_time)

        self._encrypt_credential_fields(
            db,
            obj_data=obj_data,
            account_id=obj_data.get("account_id"),
            tracker_name=str(obj_data.get("name") or "tracker"),
        )

        return super().create(db=db, obj_in=obj_data)

    def update(
        self, db: Session, *, db_obj: Tracker, obj_in: Dict[str, Any]
    ) -> Tracker:
        """Update tracker and its last_updated timestamp."""
        obj_data = dict(obj_in)
        # Update last_updated field
        obj_data["last_updated"] = datetime.now(timezone.utc)

        self._encrypt_credential_fields(
            db,
            obj_data=obj_data,
            account_id=db_obj.account_id,
            tracker_name=str(obj_data.get("name") or db_obj.name or "tracker"),
            existing_api_secret_id=db_obj.credentials_secret_id,
            existing_webhook_secret_id=db_obj.webhook_secret_id,
        )

        return super().update(db=db, db_obj=db_obj, obj_in=obj_data)

    def list_plaintext_credential_candidates(self, db: Session) -> List[Tracker]:
        """Return trackers that still store plaintext credentials to migrate."""
        return (
            db.query(Tracker)
            .filter(
                (
                    (Tracker.api_key.isnot(None) & (Tracker.api_key != ""))
                    & Tracker.credentials_secret_id.is_(None)
                )
                | (
                    (
                        Tracker.jira_webhook_secret.isnot(None)
                        & (Tracker.jira_webhook_secret != "")
                    )
                    & Tracker.webhook_secret_id.is_(None)
                )
            )
            .all()
        )

    def migrate_plaintext_credentials(
        self, db: Session, *, tracker: Tracker, commit: bool = True
    ) -> dict[str, bool]:
        """Encrypt one tracker's plaintext credentials via Secret Service.

        Returns flags indicating which credential kinds were migrated.
        """
        migrated_api = False
        migrated_webhook = False
        if tracker.api_key and not tracker.credentials_secret_id:
            tracker.credentials_secret_id = store_tracker_secret(
                db,
                account_id=tracker.account_id,
                name=f"{tracker.name} API key",
                secret_value=tracker.api_key,
                secret_kind=TRACKER_API_KEY_SECRET_KIND,
            )
            tracker.api_key = None
            migrated_api = True
        if tracker.jira_webhook_secret and not tracker.webhook_secret_id:
            tracker.webhook_secret_id = store_tracker_secret(
                db,
                account_id=tracker.account_id,
                name=f"{tracker.name} webhook secret",
                secret_value=tracker.jira_webhook_secret,
                secret_kind=TRACKER_WEBHOOK_SECRET_KIND,
            )
            tracker.jira_webhook_secret = None
            migrated_webhook = True
        if migrated_api or migrated_webhook:
            tracker.last_updated = datetime.now(timezone.utc)
            if commit:
                db.commit()
                db.refresh(tracker)
        return {
            "migrated_api_key": migrated_api,
            "migrated_webhook_secret": migrated_webhook,
        }

    def get_for_account(
        self, db: Session, *, account_id: str, skip: int = 0, limit: int = 100
    ) -> List[Tracker]:
        """Get trackers for an account."""
        return (
            db.query(Tracker)
            .filter(Tracker.account_id == str(account_id))
            .filter(Tracker.is_deleted.is_(False))  # Exclude soft-deleted trackers
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_active(
        self,
        db: Session,
        *,
        account_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Tracker]:
        """Get active trackers, optionally filtered by account."""
        query = db.query(Tracker).filter(Tracker.is_active.is_(True))
        if account_id:
            query = query.filter(Tracker.account_id == account_id)
        return query.offset(skip).limit(limit).all()

    def get_by_type(
        self,
        db: Session,
        *,
        tracker_type: TrackerType,
        account_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Tracker]:
        """Get trackers by type, optionally filtered by account."""
        query = db.query(Tracker).filter(Tracker.tracker_type == tracker_type.value)
        if account_id:
            query = query.filter(Tracker.account_id == account_id)
        return query.offset(skip).limit(limit).all()

    def get_by_id(self, db: Session, *, id: str) -> Optional[Tracker]:
        """Get tracker by ID."""
        return db.query(Tracker).filter(Tracker.id == id).first()

    def validate(
        self, db: Session, *, id: str, is_valid: bool, message: Optional[str] = None
    ) -> Optional[Tracker]:
        """Update tracker validation status."""
        tracker = self.get(db, id=id)
        if tracker:
            tracker.is_valid = is_valid
            current_time = datetime.now(timezone.utc)
            tracker.last_validation = current_time
            tracker.last_updated = current_time
            tracker.validation_message = message
            db.add(tracker)
            db.commit()
            db.refresh(tracker)
        return tracker

    def record_unknown_project(
        self,
        db: Session,
        *,
        id: str,
        project_identifier: str,
        now: Optional[datetime] = None,
    ) -> UnknownProjectState:
        """Record a webhook referencing a project we do not have, with backoff.

        A webhook can name a project the sync has never imported (repo outside
        the GitHub App's selected-repositories scope, or excluded by a
        ``TrackerScopeRule``). Re-syncing on every such event is useless work:
        the scan cannot return a repo the installation cannot see, so the next
        webhook fails the same lookup and triggers another scan.

        This tracks one failure counter per (tracker, project) in
        ``Tracker.meta_data`` and returns whether the caller should trigger a
        sync now. Retries use exponential backoff
        (:data:`UNKNOWN_PROJECT_BASE_BACKOFF` doubling up to
        :data:`UNKNOWN_PROJECT_MAX_BACKOFF`); after
        :data:`UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED` failed attempts the
        project is marked degraded so it is surfaced to the user instead of
        being retried forever.

        Args:
            db: Database session.
            id: Tracker id.
            project_identifier: Provider-side project/repo id from the webhook.
            now: Injectable clock for tests.

        Returns:
            An :class:`UnknownProjectState` describing whether to sync, the
            attempt count, and whether the project is degraded.
        """
        current_time = now or datetime.now(timezone.utc)
        tracker = self.get(db, id=id)
        if tracker is None:
            return UnknownProjectState(
                should_sync=False, attempts=0, degraded=False, retry_after_seconds=0
            )

        meta_data = dict(tracker.meta_data or {})
        unknown_projects = dict(meta_data.get(UNKNOWN_PROJECTS_META_KEY) or {})
        entry = dict(unknown_projects.get(project_identifier) or {})

        attempts = int(entry.get("attempts") or 0)
        last_attempt = _parse_timestamp(entry.get("last_attempt_at"))

        backoff = min(
            UNKNOWN_PROJECT_BASE_BACKOFF * (2 ** max(attempts - 1, 0)),
            UNKNOWN_PROJECT_MAX_BACKOFF,
        )
        due = last_attempt is None or (current_time - last_attempt) >= backoff
        degraded = attempts >= UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED
        should_sync = due and not degraded

        if should_sync:
            attempts += 1
            entry["attempts"] = attempts
            entry["last_attempt_at"] = current_time.isoformat()
            entry.setdefault("first_seen_at", current_time.isoformat())
            degraded = attempts >= UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED

        entry["degraded"] = degraded
        entry["last_seen_at"] = current_time.isoformat()
        unknown_projects[project_identifier] = entry
        meta_data[UNKNOWN_PROJECTS_META_KEY] = unknown_projects
        tracker.meta_data = meta_data
        flag_modified(tracker, "meta_data")
        tracker.last_updated = current_time
        db.add(tracker)
        db.commit()

        next_backoff = min(
            UNKNOWN_PROJECT_BASE_BACKOFF * (2 ** max(attempts - 1, 0)),
            UNKNOWN_PROJECT_MAX_BACKOFF,
        )
        return UnknownProjectState(
            should_sync=should_sync,
            attempts=attempts,
            degraded=degraded,
            retry_after_seconds=int(next_backoff.total_seconds()),
        )

    def clear_unknown_project(
        self, db: Session, *, id: str, project_identifier: str
    ) -> None:
        """Drop the unknown-project failure state once the project resolves."""
        tracker = self.get(db, id=id)
        if tracker is None:
            return
        meta_data = dict(tracker.meta_data or {})
        unknown_projects = dict(meta_data.get(UNKNOWN_PROJECTS_META_KEY) or {})
        if project_identifier not in unknown_projects:
            return
        unknown_projects.pop(project_identifier)
        if unknown_projects:
            meta_data[UNKNOWN_PROJECTS_META_KEY] = unknown_projects
        else:
            meta_data.pop(UNKNOWN_PROJECTS_META_KEY, None)
        tracker.meta_data = meta_data
        flag_modified(tracker, "meta_data")
        db.add(tracker)
        db.commit()

    def deactivate(self, db: Session, *, id: str) -> Optional[Tracker]:
        """Deactivate a tracker."""
        tracker = self.get(db, id=id)
        if tracker:
            tracker.is_active = False
            tracker.last_updated = datetime.now(timezone.utc)
            db.add(tracker)
            db.commit()
            db.refresh(tracker)
        return tracker

    def get_by_name(
        self,
        db: Session,
        *,
        name: str,
        account_id: str,
        include_deleted: bool = False,
    ) -> Optional[Tracker]:
        """Get tracker by name for a specific account."""
        query = db.query(Tracker).filter(
            Tracker.name == name, Tracker.account_id == account_id
        )
        if not include_deleted:
            query = query.filter(Tracker.is_deleted.is_(False))
        return query.first()

    def get_by_id_and_account(
        self,
        db: Session,
        *,
        id: str,
        account_id: str,
        include_deleted: bool = False,
    ) -> Optional[Tracker]:
        """Get tracker by ID for a specific account."""
        query = db.query(Tracker).filter(
            Tracker.id == str(id), Tracker.account_id == account_id
        )
        if not include_deleted:
            query = query.filter(Tracker.is_deleted.is_(False))
        return query.first()

    def delete_scope_rules(self, db: Session, *, tracker_id: str) -> None:
        """Delete all scope rules for a tracker."""
        from ..models.tracker_scope_rule import TrackerScopeRule

        db.query(TrackerScopeRule).filter(
            TrackerScopeRule.tracker_id == tracker_id
        ).delete(synchronize_session=False)

    def has_tracker(self, db: Session, *, account_id: str) -> bool:
        """Check if an account has at least one tracker.

        Args:
            db: Database session
            account_id: Account ID

        Returns:
            True if account has at least one tracker, False otherwise
        """
        return (
            db.query(Tracker)
            .filter(Tracker.account_id == str(account_id))
            .filter(Tracker.is_deleted.is_(False))
            .limit(1)
            .first()
        ) is not None

    def count_by_oauth_installation(
        self, db: Session, *, oauth_installation_id: Any
    ) -> int:
        """Count trackers using a specific OAuth installation.

        Args:
            db: Database session
            oauth_installation_id: OAuth installation ID

        Returns:
            Number of trackers using this installation
        """
        return (
            db.query(Tracker)
            .filter(Tracker.oauth_installation_id == oauth_installation_id)
            .count()
        )

    def get_by_oauth_installation(
        self,
        db: Session,
        *,
        oauth_installation_id: Any,
        active_only: bool = True,
    ) -> List[Tracker]:
        """Get trackers using a specific OAuth installation.

        Args:
            db: Database session
            oauth_installation_id: OAuth installation ID
            active_only: Only return active, non-deleted trackers

        Returns:
            List of trackers using this installation
        """
        query = db.query(Tracker).filter(
            Tracker.oauth_installation_id == oauth_installation_id
        )
        if active_only:
            query = query.filter(
                Tracker.is_active.is_(True),
                Tracker.is_deleted.is_(False),
            )
        return query.all()


# Create instance
crud_tracker = CRUDTracker(Tracker)
