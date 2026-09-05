"""Resolve a preset flow for an account and run it on a tracker target.

Used by ``POST /flows/run-preset``. Issue runs are supported here. Pull
request targets return a clear 400 until PR3 adds them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.auth.permissions import has_permission
from preloop.flow_presets import PRESET_SLUGS
from preloop.models.crud import crud_flow, crud_issue, crud_project, crud_tracker
from preloop.models.models.user import User
from preloop.services.flow_presets_service import clone_preset_for_account

IMPLEMENTER_SLUG = "automated-issue-implementation"
REVIEWER_SLUG = "pull-request-reviewer"
ISSUE_PRESET_SLUGS = {IMPLEMENTER_SLUG}


class PresetRunnerError(Exception):
    """Structured failure from resolve-or-create or payload build."""

    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def _http(status_code: int, detail: Any) -> PresetRunnerError:
    return PresetRunnerError(status_code, detail)


def _label_names(labels: Any) -> List[str]:
    """Flatten issue labels to strings (objects or plain names)."""
    names: List[str] = []
    if not isinstance(labels, list):
        return names
    for label in labels:
        if isinstance(label, dict):
            names.append(str(label.get("title") or label.get("name") or label))
        elif isinstance(label, str):
            names.append(label)
        else:
            names.append(str(label))
    return names


def _default_branch(project: Any) -> str:
    settings = (
        project.settings if isinstance(getattr(project, "settings", None), dict) else {}
    )
    meta = (
        project.meta_data
        if isinstance(getattr(project, "meta_data", None), dict)
        else {}
    )
    return str(settings.get("default_branch") or meta.get("default_branch") or "main")


def _tracker_host(tracker: Any, fallback: str) -> str:
    raw = getattr(tracker, "url", None) or ""
    if not raw:
        return fallback
    parsed = urlparse(raw)
    return parsed.netloc or parsed.path or fallback


def _issue_number(issue: Any) -> int:
    external_id = getattr(issue, "external_id", None)
    if external_id is not None:
        try:
            return int(str(external_id))
        except (TypeError, ValueError):
            pass
    key = getattr(issue, "key", "") or ""
    digits = "".join(ch for ch in str(key) if ch.isdigit())
    return int(digits) if digits else 0


def _issue_url(issue: Any) -> str:
    meta = (
        issue.meta_data if isinstance(getattr(issue, "meta_data", None), dict) else {}
    )
    return str(meta.get("url") or getattr(issue, "external_url", None) or "")


def _issue_assignee(issue: Any) -> str:
    meta = (
        issue.meta_data if isinstance(getattr(issue, "meta_data", None), dict) else {}
    )
    assignee = meta.get("assignee")
    if isinstance(assignee, dict):
        return str(assignee.get("login") or assignee.get("name") or "")
    return str(assignee or "")


def _issue_labels(issue: Any) -> List[str]:
    meta = (
        issue.meta_data if isinstance(getattr(issue, "meta_data", None), dict) else {}
    )
    return _label_names(meta.get("labels", []))


def _repository_clone_fields(project: Any, tracker: Any) -> Dict[str, Any]:
    """Clone keys the orchestrator/container read when repositories is empty.

    ``_extract_repo_url_from_trigger`` (container.py) prefers
    ``payload.repository.clone_url`` / ``html_url`` on GitHub and
    ``payload.project.http_url_to_repo`` / ``web_url`` on GitLab. The primary
    clone path uses ``trigger_project_id`` -> ``_get_repo_url_from_project``,
    which builds ``https://{host}/{slug}.git``.
    """
    slug = project.slug or project.name or ""
    name = project.name or slug.split("/")[-1] if slug else ""
    tracker_type = (getattr(tracker, "tracker_type", "") or "").lower()
    default_branch = _default_branch(project)
    if "gitlab" in tracker_type:
        host = _tracker_host(tracker, "gitlab.com")
        scheme = (
            urlparse(getattr(tracker, "url", None) or "https://gitlab.com").scheme
            or "https"
        )
        web_url = f"{scheme}://{host}/{slug}"
        clone_url = web_url if slug.endswith(".git") else f"{web_url}.git"
        return {
            "name": name,
            "path_with_namespace": slug,
            "full_name": slug,
            "html_url": web_url,
            "web_url": web_url,
            "clone_url": clone_url,
            "git_http_url": clone_url,
            "http_url_to_repo": clone_url,
            "default_branch": default_branch,
        }
    host = _tracker_host(tracker, "github.com")
    scheme = (
        urlparse(getattr(tracker, "url", None) or "https://github.com").scheme
        or "https"
    )
    html_url = f"{scheme}://{host}/{slug}"
    clone_url = html_url if slug.endswith(".git") else f"{html_url}.git"
    return {
        "name": name,
        "full_name": slug,
        "html_url": html_url,
        "web_url": html_url,
        "clone_url": clone_url,
        "git_http_url": clone_url,
        "http_url_to_repo": clone_url,
        "default_branch": default_branch,
    }


def resolve_or_create_flow(
    db: Session,
    *,
    account_id: UUID,
    preset_slug: str,
    confirm_create: bool,
    current_user: User,
    flow_crud: Any = None,
) -> Tuple[Any, bool]:
    """Find the account flow for ``preset_slug``, or create it when confirmed.

    Returns:
        ``(flow, created)``.

    Raises:
        PresetRunnerError: 404 unknown slug/preset, 409 missing/disabled,
            403 without ``create_flows``, or 422 from model binding.
    """
    crud = flow_crud if flow_crud is not None else crud_flow
    preset_name = PRESET_SLUGS.get(preset_slug)
    if not preset_name:
        raise _http(404, "Preset not found")

    preset = crud.get_global_preset_by_name(db, name=preset_name)
    if not preset:
        raise _http(404, "Preset not found")

    existing = crud.get_by_source_preset(
        db, account_id=account_id, source_preset_id=preset.id
    )
    if existing is None:
        named = crud.get_by_name_and_account(
            db, name=preset.name, account_id=account_id
        )
        if named is not None and not named.is_preset:
            existing = named

    if existing is not None:
        if not existing.is_enabled:
            raise _http(
                409,
                {"code": "flow_disabled", "flow_id": str(existing.id)},
            )
        return existing, False

    if not confirm_create:
        raise _http(
            409,
            {"code": "flow_missing", "flow_name": preset.name},
        )

    if not has_permission(current_user, "create_flows", db):
        raise _http(
            403,
            (
                f"You can run flows but not create them. Ask an admin to add "
                f"the {preset.name} flow."
            ),
        )

    try:
        created = clone_preset_for_account(
            db, preset, account_id, name=preset.name, flow_crud=crud
        )
    except HTTPException as exc:
        raise _http(exc.status_code, exc.detail) from exc
    return created, True


def build_issue_trigger_payload(
    issue: Any, project: Any, tracker: Any
) -> Dict[str, Any]:
    """Build ``trigger_event_data`` for an implementer run on ``issue``."""
    tracker_type = (getattr(tracker, "tracker_type", "") or "github").lower()
    if tracker_type not in ("github", "gitlab"):
        tracker_type = "github" if "gitlab" not in tracker_type else "gitlab"

    repo = _repository_clone_fields(project, tracker)
    issue_url = _issue_url(issue)
    number = _issue_number(issue)
    title = getattr(issue, "title", None) or ""
    description = getattr(issue, "description", None) or ""
    state = getattr(issue, "status", None) or ""
    payload: Dict[str, Any] = {
        "project_id": str(project.id),
        "repository": repo,
    }

    if "gitlab" in tracker_type:
        project_id = getattr(project, "identifier", None) or str(project.id)
        try:
            gitlab_id: Any = int(str(project_id))
        except (TypeError, ValueError):
            gitlab_id = str(project_id)
        payload["object_kind"] = "issue"
        payload["object_attributes"] = {
            "iid": number,
            "number": number,
            "title": title,
            "description": description,
            "url": issue_url,
            "state": state,
        }
        payload["project"] = {
            "id": gitlab_id,
            "name": project.name,
            "web_url": repo.get("web_url"),
            "path_with_namespace": repo.get("path_with_namespace"),
            "default_branch": repo.get("default_branch"),
            "http_url_to_repo": repo.get("http_url_to_repo"),
            "git_http_url": repo.get("git_http_url"),
        }
        source = "gitlab"
    else:
        payload["issue"] = {
            "number": number,
            "title": title,
            "body": description,
            "html_url": issue_url,
            "state": state,
            "user": {"login": _issue_assignee(issue)},
            "labels": _issue_labels(issue),
        }
        source = "github"

    return {
        "type": "issue_run",
        "source": source,
        "payload": payload,
    }


def _load_visible_issue(
    db: Session, *, issue_id: UUID, account_id: UUID
) -> Tuple[Any, Any, Any]:
    """Return ``(issue, project, tracker)`` if the issue is in this account."""
    issue = crud_issue.get(db, id=issue_id)
    if issue is None:
        raise _http(404, "Issue not found")

    trackers = crud_tracker.get_for_account(db, account_id=account_id)
    tracker_ids = {str(tracker.id) for tracker in trackers}
    if str(issue.tracker_id) not in tracker_ids:
        raise _http(404, "Issue not found")

    project = crud_project.get(db, id=str(issue.project_id), account_id=str(account_id))
    if project is None:
        raise _http(404, "Project not found")

    tracker = next(
        (item for item in trackers if str(item.id) == str(issue.tracker_id)),
        None,
    )
    if tracker is None:
        raise _http(404, "Issue not found")
    return issue, project, tracker


async def run_preset_on_target(
    db: Session,
    *,
    current_user: User,
    preset_slug: str,
    target: Any,
    confirm_create: bool,
    triggered_by: str,
    flow_crud: Any = None,
) -> Dict[str, Any]:
    """Resolve the preset flow and, when confirmed, trigger it on ``target``."""
    kind = getattr(target, "kind", None) or (
        target.get("kind") if isinstance(target, dict) else None
    )
    if kind == "pull_request":
        raise _http(
            400,
            (
                "Running a preset on a pull request is not supported yet. "
                "Use an issue target."
            ),
        )
    if kind != "issue":
        raise _http(400, "target.kind must be issue or pull_request")

    if preset_slug not in ISSUE_PRESET_SLUGS:
        if preset_slug == REVIEWER_SLUG:
            raise _http(
                400,
                (
                    "The pull-request-reviewer preset cannot run on an issue. "
                    "Use automated-issue-implementation."
                ),
            )
        if preset_slug not in PRESET_SLUGS:
            raise _http(404, "Preset not found")
        raise _http(
            400,
            f"Preset {preset_slug} does not match an issue target.",
        )

    issue_id = getattr(target, "issue_id", None) or (
        target.get("issue_id") if isinstance(target, dict) else None
    )
    if issue_id is None:
        raise _http(400, "target.issue_id is required when kind is issue")

    issue, project, tracker = _load_visible_issue(
        db, issue_id=issue_id, account_id=current_user.account_id
    )

    flow, created = resolve_or_create_flow(
        db,
        account_id=current_user.account_id,
        preset_slug=preset_slug,
        confirm_create=confirm_create,
        current_user=current_user,
        flow_crud=flow_crud,
    )

    if not confirm_create:
        # Probe only: the console shows "Run {flow} on {key}?" then repeats
        # with confirm_create true. Starting the run here would skip that
        # dialog. Spec 2.3 200-with-execution applies to the confirmed call.
        return {
            "execution_id": None,
            "flow_id": str(flow.id),
            "flow_name": flow.name,
            "flow_created": False,
            "execution_url": None,
        }

    trigger_event_data = build_issue_trigger_payload(issue, project, tracker)

    from preloop.services.flow_trigger_service import FlowTriggerService

    trigger_service = FlowTriggerService(db)
    result = await trigger_service.trigger_flow(
        flow_id=flow.id,
        test_mode=True,
        trigger_event_data=trigger_event_data,
        triggered_by=triggered_by,
    )
    execution_id = str(result.get("id") or result.get("execution_id"))
    return {
        "execution_id": execution_id,
        "flow_id": str(flow.id),
        "flow_name": flow.name,
        "flow_created": created,
        "execution_url": f"/console/flows/executions/{execution_id}",
    }
