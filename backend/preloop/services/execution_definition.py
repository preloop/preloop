"""Produce the inspectable copy of a selected flow, before writing history."""

import json
from typing import Any

from preloop.models import models
from preloop.services.model_gateway_events import redact_sensitive_text
from preloop.utils.redaction import redact_agent_config, redact_dict
from preloop.utils.secret_scrubbing import scrub_secrets

# Explicit columns so adding a credential or an internal flow field does not
# automatically retain it. Ingress secrets and custom shell bodies are omitted.
_FIELDS = (
    "name",
    "description",
    "prompt_template",
    "review_instructions",
    "agent_type",
    "ai_model_id",
    "agent_config",
    "allowed_mcp_servers",
    "allowed_mcp_tools",
    "callable_flows",
    "git_clone_config",
    "result_schema",
    "validate_result",
    "runner_pool",
    "timeout_seconds",
    "approval_window_seconds",
    "notifications",
    "trigger_event_source",
    "trigger_event_types",
    "trigger_organization_id",
    "trigger_project_ids",
    "trigger_config",
    "schedule_config",
    "is_enabled",
    "source_preset_id",
)
# These open-ended containers may carry opaque values under arbitrary names.
_WITHHELD = {
    "env",
    "env_vars",
    "environment",
    "environment_variables",
    "headers",
    "workspace_files",
    "files",
    "commands",
    "args",
    "setup_commands",
    "env_file",
    "extra_env",
    "http_headers",
    "cookies",
    "config_files",
}


def public_text(value: str) -> str:
    """Mask credential literals, assignments and URL credentials in historical text."""
    return redact_sensitive_text(scrub_secrets(value) or "")[0]


def _scrub_text(value: Any) -> Any:
    """Scrub both arbitrary JSON keys and string values."""
    if isinstance(value, dict):
        return {public_text(k): _scrub_text(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_text(item) for item in value]
    return public_text(value) if isinstance(value, str) else value


def definition_json(flow: models.Flow) -> str:
    """Serialize the selected flow's redacted settings as pageable ASCII JSON."""
    fields = {name: getattr(flow, name) for name in _FIELDS}
    fields["custom_commands_enabled"] = bool(
        isinstance(flow.custom_commands, dict) and flow.custom_commands.get("enabled")
    )
    return json.dumps(
        _scrub_text(redact_agent_config(redact_dict(fields, field_names=_WITHHELD))),
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )


def published_destination(publication: dict[str, Any]) -> dict[str, Any] | None:
    """Minimize a successful controller publication to one target and its count."""
    from preloop.services.review_publication import parse_pull_request_url

    repositories = publication.get("repositories")
    if isinstance(repositories, list):
        if publication.get("complete") is not True or not repositories:
            return None
        first = repositories[0]
        count = len(repositories)
    else:
        first = publication
        count = 1
    if not isinstance(first, dict) or first.get("provider") not in {
        "github",
        "gitlab",
        "bitbucket",
    }:
        return None
    repository, number = parse_pull_request_url(first.get("url"))
    if not repository or not number or len(repository) > 512 or len(number) > 64:
        return None
    return {
        "provider": first["provider"],
        "repository": public_text(repository),
        "pull_request": number,
        "destination_count": count,
    }
