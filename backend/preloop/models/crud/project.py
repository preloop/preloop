"""CRUD operations for Project model."""

from typing import Any, List, Optional

from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_

from ..models.project import Project
from ..models.organization import Organization
from ..models.tracker import Tracker
from .base import CRUDBase


class CRUDProject(CRUDBase[Project]):
    """CRUD operations for Project model."""

    def get_all_active_by_identifier_or_name_globally(
        self,
        db: Session,
        *,
        identifier_or_name: str,
        account_id: Optional[str] = None,
    ) -> List[Project]:
        """
        Get all active projects by identifier or name across all active organizations.
        The search is case-insensitive for names.
        Eager loads the organization for each project.
        """
        query = (
            db.query(Project)
            .join(Project.organization)
            .options(joinedload(Project.organization))
            .filter(
                or_(
                    Project.identifier == identifier_or_name,
                    func.lower(Project.name) == func.lower(identifier_or_name),
                    Project.slug == identifier_or_name,
                )
            )
            .filter(Project.is_active.is_(True))
            .filter(Organization.is_active.is_(True))
        )

        if account_id:
            query = query.join(Organization.tracker).filter(
                Tracker.account_id == account_id
            )

        return query.order_by(Project.updated_at.desc()).all()

    def get_by_slug_or_identifier(
        self,
        db: Session,
        *,
        slug_or_identifier: str,
        organization_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> Optional[Project]:
        """
        Get a project by slug or identifier, optionally filtered by organization.
        """
        query = db.query(Project).filter(
            (Project.slug == slug_or_identifier)
            | (Project.identifier == slug_or_identifier)
            | (func.lower(Project.name) == func.lower(slug_or_identifier))
        )

        if organization_id:
            query = query.filter(Project.organization_id == organization_id)

        if account_id:
            query = (
                query.join(Project.organization)
                .join(Organization.tracker)
                .filter(Tracker.account_id == account_id)
            )

        return query.order_by(Project.updated_at.desc()).first()

    def get_by_name(
        self,
        db: Session,
        *,
        name: str,
        organization_id: Optional[str] = None,
        tracker_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> Optional[Project]:
        """
        Get a project by name, optionally filtered by organization, tracker, and account.
        """
        query = db.query(Project).filter(func.lower(Project.name) == func.lower(name))

        if organization_id:
            query = query.filter(Project.organization_id == organization_id)

        if tracker_id:
            query = query.join(Project.organization).filter(
                Organization.tracker_id == tracker_id
            )

        if account_id:
            query = (
                query.join(Project.organization)
                .join(Organization.tracker)
                .filter(Tracker.account_id == account_id)
            )

        return query.order_by(Project.updated_at.desc()).first()

    def get_by_identifier(
        self,
        db: Session,
        *,
        identifier: str,
        organization_id: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> Optional[Project]:
        """
        Get a project by identifier or slug, optionally scoped to an organization.

        For trackers like Jira that use both numeric IDs and human-readable keys,
        this method checks both the identifier field and the slug field.
        """
        query = db.query(Project).filter(
            (Project.identifier == identifier) | (Project.slug == identifier)
        )
        if organization_id:
            query = query.filter(Project.organization_id == organization_id)
        if account_id:
            query = (
                query.join(Project.organization)
                .join(Organization.tracker)
                .filter(Tracker.account_id == account_id)
            )
        return query.first()

    def get_for_tracker(
        self,
        db: Session,
        *,
        tracker_id: str,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[Project]:
        """Get projects for a tracker."""
        query = (
            db.query(Project)
            .join(Organization)
            .join(Tracker)
            .filter(Tracker.id == tracker_id)
        )
        if account_id:
            query = query.filter(Tracker.account_id == account_id)
        return query.offset(skip).limit(limit).all()

    def get_for_tracker_by_path(
        self,
        db: Session,
        *,
        tracker_id: str,
        account_id: str,
        path: str,
    ) -> Optional[Project]:
        """Get the active repository project ``path`` synced from a tracker.

        Code hosts store the repository path (``owner/name``,
        ``group/subgroup/name``) as the project slug. The match is
        case-insensitive because GitHub and GitLab treat paths that way.

        Args:
            db: Database session.
            tracker_id: Tracker the project must belong to.
            account_id: Account that must own the tracker.
            path: Repository path.

        Returns:
            The project, or None when the tracker has not synced it.
        """
        lowered = path.strip().lower()
        return (
            db.query(Project)
            .join(Organization)
            .join(Tracker)
            .filter(Tracker.id == tracker_id)
            .filter(Tracker.account_id == account_id)
            .filter(Project.is_active.is_(True))
            .filter(
                or_(
                    func.lower(Project.slug) == lowered,
                    func.lower(Project.identifier) == lowered,
                )
            )
            .order_by(Project.updated_at.desc())
            .first()
        )

    def get_for_tracker_by_key(
        self,
        db: Session,
        *,
        tracker_id: str,
        key: Optional[str],
        external_id: Optional[str],
    ) -> Optional[Project]:
        """Get the active project an issue-tracker webhook names.

        Jira sync stores the project key as the slug (older rows used it as
        the identifier) and the numeric project id as the identifier. Only
        those are compared, never display names.

        Args:
            db: Database session.
            tracker_id: Tracker the webhook arrived on.
            key: Project key (``PROJ``), matched case-insensitively.
            external_id: Numeric project id from the tracker.

        Returns:
            The project, or None when neither value matches.
        """
        conditions = []
        if key:
            lowered = key.strip().lower()
            conditions.append(func.lower(Project.slug) == lowered)
            conditions.append(func.lower(Project.identifier) == lowered)
        if external_id:
            conditions.append(Project.identifier == external_id.strip())
        if not conditions:
            return None
        return (
            db.query(Project)
            .join(Organization)
            .filter(Organization.tracker_id == tracker_id)
            .filter(Project.is_active.is_(True))
            .filter(or_(*conditions))
            .order_by(Project.updated_at.desc())
            .first()
        )

    def get_for_organization(
        self,
        db: Session,
        *,
        organization_id: str,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[Project]:
        """Get projects for an organization."""
        query = db.query(Project).filter(Project.organization_id == organization_id)
        if account_id:
            query = (
                query.join(Project.organization)
                .join(Organization.tracker)
                .filter(Tracker.account_id == account_id)
            )
        return query.offset(skip).limit(limit).all()

    def count_for_organization(
        self, db: Session, *, organization_id: str, account_id: Optional[str] = None
    ) -> int:
        """Count total number of projects for an organization."""
        query = db.query(Project).filter(Project.organization_id == organization_id)
        if account_id:
            query = (
                query.join(Project.organization)
                .join(Organization.tracker)
                .filter(Tracker.account_id == account_id)
            )
        return query.count()

    def get_active(
        self,
        db: Session,
        *,
        skip: int = 0,
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> List[Project]:
        """Get active projects."""
        query = db.query(Project).filter(Project.is_active.is_(True))
        if account_id:
            query = (
                query.join(Project.organization)
                .join(Organization.tracker)
                .filter(Tracker.account_id == account_id)
            )
        return query.offset(skip).limit(limit).all()

    def deactivate(
        self, db: Session, *, id: str, account_id: Optional[str] = None
    ) -> Optional[Project]:
        """Deactivate a project."""
        project = self.get(db, id=id, account_id=account_id)
        if project:
            project.is_active = False
            db.add(project)
            db.commit()
            db.refresh(project)
        return project

    def get_by_identifier_or_name_across_orgs(
        self,
        db: Session,
        *,
        identifier_or_name: str,
        account_id: Optional[str] = None,
    ) -> Optional[Project]:
        """Get the most recently updated project by identifier or name across all organizations."""
        query = db.query(Project).filter(
            (Project.identifier == identifier_or_name)
            | (func.lower(Project.name) == func.lower(identifier_or_name))
        )
        if account_id:
            query = (
                query.join(Project.organization)
                .join(Organization.tracker)
                .filter(Tracker.account_id == account_id)
            )
        return query.order_by(Project.updated_at.desc()).first()

    def get_accessible_for_user(
        self,
        db: Session,
        *,
        account_id: str,
        project_ids: Optional[List[str]] = None,
    ) -> List[Project]:
        """
        Get projects accessible to a user based on their account.
        Eager loads organization and tracker relationships.

        Args:
            db: Database session
            account_id: Account ID to filter by
            project_ids: Optional list of project IDs to filter by

        Returns:
            List of projects with organization and tracker eager loaded
        """
        query = (
            db.query(Project)
            .options(joinedload(Project.organization).joinedload(Organization.tracker))
            .join(Project.organization)
            .join(Organization.tracker)
            .filter(Tracker.account_id == account_id)
            .filter(Tracker.is_active)
            .filter(Tracker.is_deleted.is_(False))
        )

        if project_ids:
            query = query.filter(Project.id.in_(project_ids))

        return query.all()


# Tracker types whose project identifier is a host-wide, stable repository ID
# that survives renames and moves between owners. GitHub's repository ID is the
# identity of a repository project (#1159).
REPOSITORY_ID_TRACKER_TYPES = frozenset({"github"})


def repository_host(tracker: Tracker) -> str:
    """Return the host that scopes a tracker's repository IDs.

    Repository IDs are unique per GitHub host: github.com and each GitHub
    Enterprise Server have separate ID spaces. An empty URL and the API host
    both mean github.com.

    Args:
        tracker: Tracker row.

    Returns:
        Lower-case host name.
    """
    from urllib.parse import urlparse

    raw = (getattr(tracker, "url", None) or "").strip()
    if not raw:
        return "github.com"
    host = (urlparse(raw if "://" in raw else f"https://{raw}").hostname or "").lower()
    if host in ("", "api.github.com", "www.github.com"):
        return "github.com"
    return host


def find_repository_projects(
    db: Session,
    *,
    identifier: str,
    account_id: Any,
    tracker_type: str,
    host: str,
    exclude_organization_id: Any = None,
) -> List[Project]:
    """Find projects for one repository on one host within an account.

    A repository identifier only names the same repository on the same
    tracker type and host: GitHub repository 21 and GitLab project 21 are
    unrelated, as are github.com and a GitHub Enterprise Server.

    Args:
        db: Database session.
        identifier: Repository identifier (for GitHub, the repository ID).
        account_id: Account to search.
        tracker_type: Tracker type the identifier belongs to.
        host: Host from :func:`repository_host`.
        exclude_organization_id: Organization to leave out, if any.

    Returns:
        Matching projects, most recently updated first.
    """
    query = (
        db.query(Project)
        .join(Project.organization)
        .join(Organization.tracker)
        .filter(
            Project.identifier == str(identifier),
            Tracker.account_id == account_id,
            Tracker.tracker_type == tracker_type,
            Tracker.is_deleted.is_(False),
        )
    )
    if exclude_organization_id is not None:
        query = query.filter(Project.organization_id != exclude_organization_id)
    candidates = query.order_by(Project.updated_at.desc()).all()
    return [p for p in candidates if repository_host(p.organization.tracker) == host]


def find_same_repository_projects(
    db: Session,
    *,
    organization: Organization,
    identifier: str,
    account_id: Any,
) -> List[Project]:
    """Find this repository registered under another organization.

    Matches projects in the same account whose tracker has the same type and
    host as ``organization``'s tracker and whose identifier is the same
    repository ID. Projects in ``organization`` itself are excluded, as are
    tracker types whose identifiers are not stable repository IDs.

    Args:
        db: Database session.
        organization: Organization the caller wants to register the repo in.
        identifier: Repository ID.
        account_id: Account that owns both organizations.

    Returns:
        Matching projects, most recently updated first.
    """
    tracker = organization.tracker
    if tracker is None or tracker.tracker_type not in REPOSITORY_ID_TRACKER_TYPES:
        return []
    return find_repository_projects(
        db,
        identifier=identifier,
        account_id=account_id,
        tracker_type=tracker.tracker_type,
        host=repository_host(tracker),
        exclude_organization_id=organization.id,
    )


def repository_identity_lock_key(account_id: Any, host: str, identifier: str) -> int:
    """Advisory lock key for one repository identity in one account.

    Args:
        account_id: Account the repository is registered in.
        host: Host from :func:`repository_host`.
        identifier: Repository ID.

    Returns:
        A signed 64-bit integer, as ``pg_advisory_xact_lock`` expects.
    """
    import hashlib

    digest = hashlib.sha256(
        f"preloop:repository:{account_id}:{host}:{identifier}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def lock_repository_identity(
    db: Session, *, account_id: Any, host: str, identifier: str
) -> None:
    """Serialize writers that register or move one repository.

    There is no unique constraint that can express "one project per
    repository ID per host per account" (the host lives on the tracker, two
    joins away), so the duplicate check and the write that follows it hold a
    transaction-scoped advisory lock. It is released when the writer commits
    or rolls back. Do not hold it across network calls.

    A no-op on databases other than PostgreSQL.
    """
    from sqlalchemy import text

    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": repository_identity_lock_key(account_id, host, identifier)},
    )
