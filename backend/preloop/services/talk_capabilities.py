"""Features announced by the currently connected native Talk sidecar."""

from typing import Any

from sqlalchemy.orm import Session

from preloop.models.crud import crud_managed_agent_enrollment, crud_project
from preloop.models.crud.project import repository_host


class TalkCapabilityError(ValueError):
    """The connected sidecar cannot honor the requested Talk operation."""


def sidecar_features(db: Session, agent: Any) -> dict[str, Any]:
    """Read generation-bound features from the existing enrollment record."""
    for kind in ("cli_managed_config", "runtime_plugin_control"):
        enrollment = crud_managed_agent_enrollment.get_latest_for_agent_by_type(
            db,
            account_id=str(agent.account_id),
            agent_id=str(agent.id),
            enrollment_type=kind,
        )
        validation = getattr(enrollment, "validation_result", None)
        if not isinstance(validation, dict):
            continue
        if validation.get("talk_connection_id") != str(
            getattr(agent, "control_connection_id", None)
        ):
            continue
        return {
            "runtime": agent.session_source_type,
            "capabilities": validation.get("talk_capabilities", {})
            if isinstance(validation.get("talk_capabilities"), dict)
            else {},
            "version": validation.get("talk_version"),
            "default_directory": validation.get("talk_default_directory"),
            "checkouts": clean_checkouts(validation.get("talk_checkouts")),
        }
    return {
        "runtime": getattr(agent, "session_source_type", "claude_code"),
        "capabilities": {},
        "version": None,
        "checkouts": [],
    }


def require_features(features: dict[str, Any], required: list[str]) -> None:
    """Refuse unsupported requests before persisting or sending a command."""
    capabilities = features.get("capabilities") or {}
    labels = {
        "start_directory": "working directories",
        "command_session": "session binding",
        "followup_prompts": "follow-up prompts",
    }
    missing = [
        labels.get(name, name) for name in required if not capabilities.get(name)
    ]
    if missing:
        plugin = (
            "codex-plugin@0.1.1"
            if features.get("runtime") == "codex"
            else "claude-plugin@0.1.2"
        )
        raise TalkCapabilityError(
            "Update the connected sidecar for "
            + ", ".join(missing)
            + f". Run preloop update and npm install -g @preloop-ai/{plugin}, then restart the sidecar."
        )


def clean_checkouts(value: Any) -> list[dict[str, str]]:
    """Bound untrusted host inventory before comparing tracker identities."""
    if not isinstance(value, list):
        return []
    return [
        {"path": entry["path"], "remote": entry["remote"]}
        for entry in value[:100]
        if isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("remote"), str)
        and len(entry["path"]) <= 1024
        and len(entry["remote"]) <= 512
    ]


def project_choices(
    db: Session, account_id: Any, checkouts: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Match only host-observed checkouts to this account's tracker projects."""
    projects = crud_project.list_active_for_tracker_types(
        db,
        account_id=account_id,
        tracker_types=["github", "gitlab", "bitbucket", "bitbucket_dc", "azure_devops"],
    )
    choices = []
    seen: set[tuple[str, str]] = set()
    for project in projects:
        tracker = project.organization.tracker
        host = repository_host(tracker)
        kind = getattr(tracker, "tracker_type", "github")
        if not getattr(tracker, "url", None):
            host = {
                "gitlab": "gitlab.com",
                "bitbucket": "bitbucket.org",
                "azure_devops": "dev.azure.com",
            }.get(kind, host)
        if host == "api.bitbucket.org":
            host = "bitbucket.org"
        identity = f"{host}/{project.slug}".lower()
        for checkout in checkouts:
            directory = checkout.get("path", "")
            if (
                not directory.startswith("/")
                or checkout.get("remote", "").lower() != identity
            ):
                continue
            key = (str(project.id), directory)
            if key not in seen:
                seen.add(key)
                choices.append(
                    {
                        "project_id": str(project.id),
                        "name": project.name,
                        "path": directory,
                    }
                )
    return choices
