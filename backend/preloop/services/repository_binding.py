"""Repository binding for flows triggered by an issue-only tracker.

A Jira project tracks issues but hosts no git repository. When a flow with
git clone enabled is triggered from such a tracker and declares no
repository of its own, the repository binding says which code-host
repository to clone, push to and open the pull request on.

Bindings live in two places:

* ``git_clone_config.repository_bindings`` on the flow, and
* ``settings["repository_bindings"]`` on the triggering (Jira) project, a
  default every flow on that project inherits.

A non-empty flow-level list overrides the project default. The binding only
fills ``git_clone_config.repositories`` for this execution; the flow row is
never modified. The code-host tracker is resolved through the tracker
factory (``hosts_repositories``). Issue-only triggers are gated on
``hosts_issues``, so registering a code host that has not yet set
``hosts_repositories`` does not treat it as Jira-like.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_project, crud_tracker
from preloop.models.schemas.flow import (
    RepositoryBinding,
    validate_repository_bindings,
)
from preloop.sync.trackers.factory import (
    tracker_hosts_issues,
    tracker_hosts_repositories,
)

logger = logging.getLogger(__name__)

REPOSITORY_BINDINGS_KEY = "repository_bindings"
BOUND_CLONE_PATH = "/workspace"

_BINDINGS_ADAPTER = TypeAdapter(List[RepositoryBinding])


class RepositoryBindingError(ValueError):
    """The binding cannot be applied; the execution must not guess."""


@dataclass(frozen=True)
class AppliedRepositoryBinding:
    """The repository an execution was bound to, and the config it produced.

    Attributes:
        source: ``"flow"`` or ``"project"``: where the binding came from.
        tracker_id: Code-host tracker id.
        tracker_type: Code-host tracker type (``github``, ``gitlab``, ...).
        project_id: Synced project id of the repository on the code host.
        repository: Repository path (``owner/name``).
        base_branch: Branch the clone checks out and the PR targets.
        git_clone_config: Effective clone config for this execution.
    """

    source: str
    tracker_id: str
    tracker_type: str
    project_id: str
    repository: str
    base_branch: str
    git_clone_config: Dict[str, Any]

    def summary(self) -> Dict[str, Any]:
        """Credential-free description for the execution context and logs."""
        return {
            "source": self.source,
            "tracker_id": self.tracker_id,
            "tracker_type": self.tracker_type,
            "project_id": self.project_id,
            "repository": self.repository,
            "base_branch": self.base_branch,
        }


def parse_repository_bindings(raw: Any, *, where: str) -> List[RepositoryBinding]:
    """Parse a stored binding list.

    Args:
        raw: Value stored in the flow config or project settings.
        where: Human-readable location for the error message.

    Returns:
        Parsed bindings; empty when nothing is stored.

    Raises:
        RepositoryBindingError: The stored value is malformed.
    """
    if raw is None or raw == []:
        return []
    try:
        return validate_repository_bindings(_BINDINGS_ADAPTER.validate_python(raw))
    except (ValidationError, ValueError) as exc:
        raise RepositoryBindingError(
            f"Invalid repository binding in {where}: {exc}"
        ) from exc


def select_repository_binding(
    bindings: List[RepositoryBinding], *, where: str
) -> RepositoryBinding:
    """Pick the one repository this execution works on.

    A single entry is the default. With several entries exactly one must be
    marked ``default``; the repository is never guessed from issue text.

    Args:
        bindings: Non-empty binding list.
        where: Human-readable location for the error message.

    Returns:
        The selected binding.

    Raises:
        RepositoryBindingError: No entry can be selected.
    """
    if not bindings:
        raise RepositoryBindingError(f"No repository binding in {where}")
    if len(bindings) == 1:
        return bindings[0]
    defaults = [binding for binding in bindings if binding.default]
    if len(defaults) == 1:
        return defaults[0]
    raise RepositoryBindingError(
        f"{where} binds {len(bindings)} repositories and marks none as default. "
        "Mark one entry with default: true."
    )


def _trigger_tracker_type(
    db: Session, trigger_tracker_id: Optional[str], trigger_source: Optional[str]
) -> Optional[str]:
    """Tracker type of the tracker that fired the event."""
    if trigger_tracker_id:
        tracker = crud_tracker.get_by_id(db, id=str(trigger_tracker_id))
        if tracker is not None and tracker.tracker_type:
            return str(tracker.tracker_type).lower()
    return (trigger_source or "").lower() or None


def trigger_needs_binding(tracker_type: Optional[str]) -> bool:
    """Whether a trigger of ``tracker_type`` has no repository of its own.

    Args:
        tracker_type: Triggering tracker type.

    Returns:
        True for known issue-only trackers (Jira). Code hosts and unknown
        sources (webhooks, schedules) keep their existing behaviour. Gated on
        ``hosts_issues``, not on "registered and not a repository host", so a
        code host that is registered but has not set ``hosts_repositories``
        yet is not mistaken for an issue tracker.
    """
    return tracker_hosts_issues(tracker_type)


def _load_bindings(
    db: Session,
    config: Dict[str, Any],
    trigger_project_id: Optional[str],
) -> Tuple[List[RepositoryBinding], str, str]:
    """Return (bindings, source, where): flow level first, then project."""
    flow_bindings = parse_repository_bindings(
        config.get(REPOSITORY_BINDINGS_KEY), where="the flow's git_clone_config"
    )
    if flow_bindings:
        return flow_bindings, "flow", "the flow's git_clone_config"
    if not trigger_project_id:
        return [], "project", "the triggering project"
    project = crud_project.get(db, id=str(trigger_project_id))
    settings = getattr(project, "settings", None) if project is not None else None
    if not isinstance(settings, dict):
        return [], "project", "the triggering project"
    where = f"project {getattr(project, 'slug', None) or trigger_project_id} settings"
    return (
        parse_repository_bindings(settings.get(REPOSITORY_BINDINGS_KEY), where=where),
        "project",
        where,
    )


def resolve_repository_binding(
    db: Session,
    *,
    account_id: Optional[str],
    git_clone_config: Any,
    trigger_tracker_id: Optional[str],
    trigger_source: Optional[str],
    trigger_project_id: Optional[str],
) -> Optional[AppliedRepositoryBinding]:
    """Apply the repository binding for one execution, if it applies.

    The binding applies only when all of these hold: git clone is enabled,
    the flow declares no ``repositories`` of its own, the trigger came from
    an issue-only tracker, and a flow or project binding exists. Otherwise
    None is returned and the existing clone behaviour is untouched.

    Args:
        db: Database session.
        account_id: Account owning the flow; the code-host tracker must
            belong to it.
        git_clone_config: The flow's stored clone config.
        trigger_tracker_id: Tracker that fired the event.
        trigger_source: Event source, used when the tracker id is missing.
        trigger_project_id: Project that fired the event (the Jira project).

    Returns:
        The applied binding with the effective clone config, or None.

    Raises:
        RepositoryBindingError: A binding exists but cannot be applied (no
            default among several, unknown or deleted tracker, a tracker that
            hosts no repositories, a repository the tracker has not synced).
    """
    if not isinstance(git_clone_config, dict) or not git_clone_config.get("enabled"):
        return None
    if git_clone_config.get("repositories"):
        return None
    tracker_type = _trigger_tracker_type(db, trigger_tracker_id, trigger_source)
    if not trigger_needs_binding(tracker_type):
        return None

    bindings, source, where = _load_bindings(db, git_clone_config, trigger_project_id)
    if not bindings:
        return None
    binding = select_repository_binding(bindings, where=where)

    if not account_id:
        raise RepositoryBindingError("Repository binding requires an account")
    host = crud_tracker.get_by_id_and_account(
        db, id=str(binding.tracker_id), account_id=str(account_id)
    )
    if host is None or not getattr(host, "is_active", True):
        raise RepositoryBindingError(
            f"Repository binding in {where} names tracker {binding.tracker_id}, "
            "which is not an active tracker of this account"
        )
    host_type = str(host.tracker_type or "").lower()
    if not tracker_hosts_repositories(host_type):
        raise RepositoryBindingError(
            f"Repository binding in {where} names a {host_type or 'unknown'} "
            "tracker, which does not host git repositories"
        )
    project = crud_project.get_for_tracker_by_path(
        db,
        tracker_id=str(host.id),
        account_id=str(account_id),
        path=binding.repository,
    )
    if project is None:
        raise RepositoryBindingError(
            f"Repository {binding.repository} is not synced under the bound "
            f"{host_type} tracker. Sync the tracker or fix the binding in {where}."
        )

    base_branch = binding.base_branch or git_clone_config.get("source_branch") or "main"
    effective = copy.deepcopy(git_clone_config)
    effective["repositories"] = [
        {
            "tracker_id": str(host.id),
            "project_id": str(project.id),
            "clone_path": BOUND_CLONE_PATH,
            "branch": base_branch,
        }
    ]
    effective["source_branch"] = base_branch
    applied = AppliedRepositoryBinding(
        source=source,
        tracker_id=str(host.id),
        tracker_type=host_type,
        project_id=str(project.id),
        repository=binding.repository,
        base_branch=base_branch,
        git_clone_config=effective,
    )
    logger.info(
        "Bound %s trigger to %s repository %s (base %s, from %s)",
        tracker_type,
        host_type,
        binding.repository,
        base_branch,
        source,
    )
    return applied
