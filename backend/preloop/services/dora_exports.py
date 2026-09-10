"""DORA agent-slice exports: the ICT asset register and incident candidates.

Two read-only exports over records Preloop already holds. Nothing here starts
collecting anything new; every row is a projection of a table the console
already renders.

**Scope, stated once and repeated in the docs.** Preloop holds the AI-agent
slice of an ICT estate: the agents, the tools and MCP servers they reach, the
models and providers they call through the gateway, and the hosts that run
them. It does not know about the entity's databases, networks, payment rails
or third-party contracts. The asset register feeds an Art. 8 inventory and an
Art. 28 register of information; it is not either of those documents.

**Classification stays with the entity.** The incident export is called
*candidates* on purpose. Art. 17 to 19 make the financial entity classify an
incident and decide whether it is major and reportable. This export carries no
severity, no major/non-major flag and no client-impact field, because Preloop
cannot determine any of them. What it carries is the platform's own technical
vocabulary (a failure category, an upstream error class, a halt scope) under
column names that say so, so nobody mistakes ``platform_category`` for a DORA
classification.

**The manifest is #559's, which is #511's.** Same ``members`` entries, same
``members_digest`` over the same canonical JSON
(:func:`preloop.cra.evidence_pack.canonical_manifest_json`), so one verifier
covers evidence packs, period exports and these two, and signed exports (#558)
have one format to sign. What the digest proves is narrow: the bytes served
are the bytes Preloop built. It says nothing about whether the underlying
records were true when they were written.

**Edition awareness is a column that is present and empty, never a column that
disappears.** A register whose shape changes with the deployment cannot be
diffed across two exports, and an absent column silently reads as "nothing to
report". So the CSV header is identical in every edition, and the manifest's
``edition`` block names each field the running deployment cannot fill and why.
The two that matter: configuration-change history (only the Enterprise audit
plugin writes ``configuration_change`` rows) and policy denies (only that
plugin persists ``policy_deny`` rows, see
``preloop/services/policy_evaluator.py``). In OSS both are empty because
nothing wrote them, which is not the same fact as "no policy ever denied
anything", and the manifest says exactly that.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.models.crud import crud_audit_log
from preloop.models.models.ai_model import AIModel
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.budget import BudgetPolicy
from preloop.models.models.flow import Flow
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.models.flow_runner import FlowRunner
from preloop.models.models.managed_agent import ManagedAgent
from preloop.models.models.mcp_server import MCPServer
from preloop.models.models.runtime_session_activity import RuntimeSessionActivity
from preloop.models.models.tool_access_rule import ToolAccessRule
from preloop.models.models.tool_configuration import ApprovalWorkflow, ToolConfiguration
from preloop.models.models.user import User
from preloop.services.flow_failure_category import FAILURE_STATUSES
from preloop.services.kill_switch import KILL_SWITCH_ERROR_CLASS
from preloop.services.upstream_errors import (
    ERROR_CLASS_NETWORK,
    ERROR_CLASS_STREAM_ABANDONED,
    ERROR_CLASS_UPSTREAM_AUTH,
    ERROR_CLASS_UPSTREAM_DISCONNECT,
    ERROR_CLASS_UPSTREAM_ERROR,
    ERROR_CLASS_UPSTREAM_OVERLOADED,
    ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
    ERROR_CLASS_UPSTREAM_RATE_LIMITED,
)

logger = logging.getLogger(__name__)

ASSET_MANIFEST_SCHEMA = "preloop.dora.asset_register_manifest/v1"
INCIDENT_MANIFEST_SCHEMA = "preloop.dora.incident_candidates_manifest/v1"

ASSET_MEMBER_STEM = "asset-register"
INCIDENT_MEMBER_STEM = "incident-candidates"

AUDIT_ACTION_ASSET_EXPORT = "dora_asset_register_export"
AUDIT_ACTION_INCIDENT_EXPORT = "dora_incident_candidates_export"

#: Per record class. Over it is an error, never a silent truncation: a
#: compliance export missing its tail is worse than no export.
MAX_ROWS_PER_CLASS = 100000

FORMAT_JSON = "json"
FORMAT_CSV = "csv"
FORMATS = (FORMAT_JSON, FORMAT_CSV)

# --- Asset register ------------------------------------------------------

ASSET_AGENT = "agent"
ASSET_TOOL = "tool"
ASSET_MCP_SERVER = "mcp_server"
ASSET_MODEL = "model"
ASSET_PROVIDER = "provider"
ASSET_RUNNER_HOST = "runner_host"

ASSET_RECORD_TYPES = (
    ASSET_AGENT,
    ASSET_TOOL,
    ASSET_MCP_SERVER,
    ASSET_MODEL,
    ASSET_PROVIDER,
    ASSET_RUNNER_HOST,
)

#: One flat table with a ``record_type`` column rather than six sheets: a
#: register is read in a spreadsheet and filtered, and six files are six
#: chances to file five of them.
ASSET_COLUMNS = (
    "record_type",
    "asset_id",
    "name",
    "asset_kind",
    "provider",
    "location",
    "owner_username",
    "owner_user_id",
    "lifecycle_state",
    "first_seen",
    "last_seen",
    "last_seen_source",
    "attached_policies",
    "attached_policy_count",
    "parent_asset_id",
    "last_config_change_at",
    "last_config_change_by",
    "source_table",
)

#: Columns only an Enterprise deployment can fill, with the reason. Present
#: and empty everywhere else (see the module docstring).
ASSET_EE_FIELDS = (
    (
        "last_config_change_at",
        "configuration_change audit rows are written by the Enterprise audit "
        "plugin (preloop/utils/audit.py log_config_change returns early "
        "without it), so this column is empty on this deployment; empty here "
        "means unrecorded, not unchanged",
    ),
    (
        "last_config_change_by",
        "same source as last_config_change_at",
    ),
)

#: config_type values the audit plugin uses, mapped to the record type they
#: describe. Used to attach a change-control timestamp to an asset.
CONFIG_TYPE_RECORD_TYPES = {
    "mcp_server": ASSET_MCP_SERVER,
    "tool": ASSET_TOOL,
    "tool_rule": ASSET_TOOL,
    "tool_configuration": ASSET_TOOL,
    "ai_model": ASSET_MODEL,
    "model": ASSET_MODEL,
    "managed_agent": ASSET_AGENT,
    "agent": ASSET_AGENT,
    "flow_runner": ASSET_RUNNER_HOST,
    "runner": ASSET_RUNNER_HOST,
}

# --- Incident candidates -------------------------------------------------

INCIDENT_EXECUTION_FAILURE = "execution_failure"
INCIDENT_KILL_SWITCH = "kill_switch_activation"
INCIDENT_POLICY_DENY = "policy_deny"
INCIDENT_BUDGET_BREACH = "budget_breach"
INCIDENT_GATEWAY_UPSTREAM_FAILURE = "gateway_upstream_failure"

INCIDENT_RECORD_TYPES = (
    INCIDENT_EXECUTION_FAILURE,
    INCIDENT_KILL_SWITCH,
    INCIDENT_POLICY_DENY,
    INCIDENT_BUDGET_BREACH,
    INCIDENT_GATEWAY_UPSTREAM_FAILURE,
)

INCIDENT_COLUMNS = (
    "record_type",
    "occurred_at",
    "record_id",
    "correlation_id",
    "correlation_source",
    "agent_id",
    "agent_name",
    "flow_id",
    "execution_id",
    "runtime_session_id",
    "subject",
    "provider",
    "platform_category",
    "status_code",
    "actor_user_id",
    "detail",
    "source_table",
)

#: Audit actions read by the incident export.
AUDIT_ACTION_KILL_SWITCH = "kill_switch_activated"
AUDIT_ACTION_POLICY_DENY = "policy_deny"
AUDIT_ACTION_GATEWAY_REQUEST = "model_gateway_request"
AUDIT_STATUS_BUDGET_DENIED = "budget_denied"

#: ``ApiUsage.error_class`` values that mean the upstream provider (or the
#: network to it) failed. Deliberately excludes ``kill_switch`` (that is our
#: own halt, and it is already its own record type), ``client_cancelled``
#: (normal client behaviour) and budget denials (no error class at all).
UPSTREAM_ERROR_CLASSES = (
    ERROR_CLASS_NETWORK,
    ERROR_CLASS_UPSTREAM_OVERLOADED,
    ERROR_CLASS_UPSTREAM_RATE_LIMITED,
    ERROR_CLASS_UPSTREAM_QUOTA_EXHAUSTED,
    ERROR_CLASS_UPSTREAM_AUTH,
    ERROR_CLASS_UPSTREAM_ERROR,
    ERROR_CLASS_UPSTREAM_DISCONNECT,
    ERROR_CLASS_STREAM_ABANDONED,
)

#: Longest free-text detail kept in a row. A register is read in a
#: spreadsheet; a 40 KB stack trace in a cell helps nobody.
MAX_DETAIL_CHARS = 500

CLASSIFICATION_NOTE = (
    "Incident classification under DORA Art. 17 to 19 is the financial "
    "entity's, not Preloop's. These rows carry no severity, no major or "
    "non-major determination and no client-impact field. platform_category "
    "is Preloop's own technical vocabulary (a flow failure category, a "
    "gateway error class, a halt scope), not a DORA classification."
)

SCOPE_NOTE = (
    "Preloop covers the AI-agent slice of the ICT estate only: agents, the "
    "tools and MCP servers they reach, the models and providers they call, "
    "and the hosts that run them. Everything else in the estate is outside "
    "this export."
)


class DoraExportError(ValueError):
    """The export cannot be produced as asked."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DoraExport:
    """One rendered export: the bytes served, and the manifest over them."""

    kind: str
    export_format: str
    columns: tuple[str, ...]
    rows: list[dict[str, Any]]
    body: bytes
    manifest: dict[str, Any]
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        """Digest of the body exactly as served."""
        return hashlib.sha256(self.body).hexdigest()

    @property
    def member_name(self) -> str:
        return f"{self.kind}.{self.export_format}"

    @property
    def media_type(self) -> str:
        return "text/csv" if self.export_format == FORMAT_CSV else "application/json"

    @property
    def filename(self) -> str:
        stamp = str(self.manifest.get("generated_at") or "")[:10]
        return f"preloop-{self.kind}-{stamp}.{self.export_format}"


# --- Small helpers -------------------------------------------------------


def normalize_format(value: Optional[str]) -> str:
    """Accept ``json`` or ``csv``, case-insensitively; refuse anything else."""
    candidate = (value or FORMAT_JSON).strip().lower()
    if candidate not in FORMATS:
        raise DoraExportError(
            "unsupported_format",
            f"format must be one of {', '.join(FORMATS)}",
        )
    return candidate


def _iso(value: Any) -> Optional[str]:
    """UTC ISO 8601, or None. Naive timestamps are read as UTC.

    Several columns in this schema are naive ``DateTime`` (flow_execution,
    audit_log) and several are timezone aware (flow_runner heartbeats). A
    register that mixed the two would sort wrong in a spreadsheet, so every
    value leaves here as an explicit UTC instant.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _clip(value: Any) -> Optional[str]:
    """One-line, bounded free text."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if len(text) > MAX_DETAIL_CHARS:
        return text[: MAX_DETAIL_CHARS - 1] + "…"
    return text


def _str_or_none(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def _latest(current: Optional[datetime], candidate: Optional[datetime]):
    """Max of two timestamps, tolerating naive/aware mixes."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    left = current if current.tzinfo else current.replace(tzinfo=UTC)
    right = candidate if candidate.tzinfo else candidate.replace(tzinfo=UTC)
    return candidate if right > left else current


def _guard(rows: Sequence[Any], record_class: str) -> Sequence[Any]:
    """Refuse an over-size class rather than truncate it."""
    if len(rows) > MAX_ROWS_PER_CLASS:
        raise DoraExportError(
            "export_too_large",
            f"the account holds more than {MAX_ROWS_PER_CLASS} {record_class} "
            "records for this export; narrow the period (incident "
            "candidates) or page the list API (asset register) instead of "
            "taking a truncated file",
        )
    return rows


def edition_snapshot() -> dict[str, Any]:
    """What this deployment can and cannot record, detected, not configured.

    Import probes rather than a settings flag: the question is whether the
    plugin that writes those rows is actually loaded in this process, and a
    flag can be wrong about that.
    """
    try:  # pragma: no cover - the EE plugin is absent in the OSS test matrix
        from plugins.audit.service import get_audit_service  # noqa: F401

        audit_plugin = True
    except ImportError:
        audit_plugin = False
    try:  # pragma: no cover - same
        from preloop.plugins.proprietary.rbac.permissions import (  # noqa: F401
            require_permission,
        )

        rbac_plugin = True
    except ImportError:
        rbac_plugin = False
    return {
        "edition": "enterprise" if audit_plugin else "oss",
        "audit_plugin": audit_plugin,
        "rbac_plugin": rbac_plugin,
    }


def _edition_block(fields: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """The manifest's edition block: what is empty here, and why."""
    snapshot = edition_snapshot()
    absent = (
        []
        if snapshot["audit_plugin"]
        else [{"field": name, "reason": reason} for name, reason in fields]
    )
    return {
        **snapshot,
        "fields_absent": absent,
        "note": (
            "Columns are identical in every edition so two exports can be "
            "diffed. A field listed in fields_absent is empty because "
            "nothing on this deployment writes it, which is not the same as "
            "nothing having happened."
        ),
    }


# --- Rendering -----------------------------------------------------------


def render_rows(
    rows: Sequence[dict[str, Any]],
    columns: Sequence[str],
    export_format: str,
) -> bytes:
    """Serialize rows deterministically for the requested format."""
    if export_format == FORMAT_CSV:
        buffer = io.StringIO(newline="")
        # \r\n is what RFC 4180 asks for and what Excel expects; the digest
        # covers exactly these bytes.
        writer = csv.DictWriter(
            buffer,
            fieldnames=list(columns),
            extrasaction="ignore",
            lineterminator="\r\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_cell(row.get(key)) for key in columns})
        return buffer.getvalue().encode("utf-8")
    return canonical_manifest_json(list(rows))


def _csv_cell(value: Any) -> str:
    """One CSV cell. None is empty, a list is semicolon joined."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item) for item in value)
    return str(value)


def _member_entry(name: str, body: bytes) -> dict[str, Any]:
    return {
        "name": name,
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _build_manifest(
    *,
    schema: str,
    account_id: Any,
    generated_at: datetime,
    member_name: str,
    body: bytes,
    columns: Sequence[str],
    counts: dict[str, int],
    extra: dict[str, Any],
) -> dict[str, Any]:
    """The #559 manifest, over a single member: the file being served."""
    members = [_member_entry(member_name, body)]
    manifest = {
        "schema": schema,
        "account_id": str(account_id),
        "generated_at": generated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "members": members,
        # Same computation as the evidence pack and the period export, so one
        # verifier covers all three and #558 has one format to sign.
        "members_digest": hashlib.sha256(canonical_manifest_json(members)).hexdigest(),
        "columns": list(columns),
        "counts": counts,
        "total_rows": sum(counts.values()),
        "scope": SCOPE_NOTE,
        "note": (
            "sha256 covers the member as served. This export is not signed: "
            "the digest shows the bytes were not altered after Preloop built "
            "them, not that the records were true when they were written."
        ),
    }
    manifest.update(extra)
    return manifest


# --- Asset register ------------------------------------------------------


def _owner_names(db: Session, account_id: Any) -> dict[str, str]:
    """user id -> username, for the owner column."""
    rows = db.execute(
        select(User.id, User.username, User.email).where(User.account_id == account_id)
    ).all()
    return {str(row[0]): (row[1] or row[2] or "") for row in rows}


def _budget_policies(db: Session, account_id: Any) -> list[BudgetPolicy]:
    return list(
        db.execute(
            select(BudgetPolicy).where(BudgetPolicy.account_id == account_id)
        ).scalars()
    )


def _budget_label(policy: BudgetPolicy) -> str:
    """One budget policy as a register cell."""
    period = getattr(policy.period, "value", policy.period)
    limits = []
    if policy.hard_limit_usd:
        limits.append(f"hard ${policy.hard_limit_usd:g}")
    if policy.soft_limit_usd:
        limits.append(f"soft ${policy.soft_limit_usd:g}")
    scope = f"/{policy.model_alias}" if policy.model_alias else ""
    return f"budget:{period}{scope} {' '.join(limits)}".strip()


def _config_change_index(db: Session, account_id: Any) -> dict[tuple[str, str], Any]:
    """(record_type, identifier) -> latest configuration_change row.

    Enterprise only. ``resource_id`` on those rows is the config type, not the
    asset, so the asset is matched through the id or name inside the recorded
    value. A row that names neither is skipped rather than guessed at.
    """
    index: dict[tuple[str, str], Any] = {}
    rows = db.execute(
        select(AuditLog)
        .where(
            AuditLog.account_id == account_id,
            AuditLog.action == "configuration_change",
        )
        .order_by(AuditLog.timestamp)
    ).scalars()
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        record_type = CONFIG_TYPE_RECORD_TYPES.get(
            str(details.get("config_type") or "")
        )
        if record_type is None:
            continue
        for value in (details.get("new_value"), details.get("old_value")):
            if not isinstance(value, dict):
                continue
            for key in ("id", "name", "tool_name"):
                identifier = value.get(key)
                if identifier:
                    # Ordered by timestamp, so the last write wins.
                    index[(record_type, str(identifier))] = row
    return index


def _config_change_cells(
    index: dict[tuple[str, str], Any],
    owners: dict[str, str],
    record_type: str,
    identifiers: Iterable[Any],
) -> tuple[Optional[str], Optional[str]]:
    """Change-control columns for one asset, or (None, None)."""
    best = None
    for identifier in identifiers:
        if identifier is None:
            continue
        row = index.get((record_type, str(identifier)))
        if row is not None and (best is None or row.timestamp > best.timestamp):
            best = row
    if best is None:
        return (None, None)
    actor = owners.get(str(best.user_id)) if best.user_id else None
    return (_iso(best.timestamp), actor or _str_or_none(best.user_id))


def _agent_rows(
    db: Session,
    *,
    account_id: Any,
    owners: dict[str, str],
    budgets: Sequence[BudgetPolicy],
    changes: dict[tuple[str, str], Any],
) -> list[dict[str, Any]]:
    rows = _guard(
        list(
            db.execute(
                select(ManagedAgent)
                .where(ManagedAgent.account_id == account_id)
                .order_by(ManagedAgent.created_at, ManagedAgent.id)
            ).scalars()
        ),
        ASSET_AGENT,
    )
    out = []
    for agent in rows:
        policies = [
            _budget_label(policy)
            for policy in budgets
            if policy.subject_type == "managed_agent"
            and str(policy.subject_id) == str(agent.id)
        ]
        changed_at, changed_by = _config_change_cells(
            changes, owners, ASSET_AGENT, (agent.id, agent.display_name)
        )
        out.append(
            {
                "record_type": ASSET_AGENT,
                "asset_id": str(agent.id),
                "name": agent.display_name,
                "asset_kind": agent.agent_kind,
                "provider": agent.session_source_type,
                "location": agent.enrollment_hostname,
                "owner_username": owners.get(str(agent.owner_user_id)),
                "owner_user_id": _str_or_none(agent.owner_user_id),
                "lifecycle_state": agent.lifecycle_state,
                "first_seen": _iso(agent.created_at),
                "last_seen": _iso(agent.last_seen_at),
                "last_seen_source": "managed_agent.last_seen_at",
                "attached_policies": sorted(policies),
                "attached_policy_count": len(policies),
                "parent_asset_id": None,
                "last_config_change_at": changed_at,
                "last_config_change_by": changed_by,
                "source_table": "managed_agent",
            }
        )
    return out


def _tool_last_seen(
    db: Session, account_id: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(tool name -> last call, MCP server name -> last call) from activity.

    ``runtime_session_activity`` is written by the core, not the audit
    plugin, so a tool's last use is available in every edition.
    """
    tools: dict[str, Any] = {}
    servers: dict[str, Any] = {}
    rows = db.execute(
        select(
            RuntimeSessionActivity.tool_name,
            RuntimeSessionActivity.server_name,
            func.max(RuntimeSessionActivity.timestamp),
        )
        .where(
            RuntimeSessionActivity.account_id == account_id,
            RuntimeSessionActivity.activity_type == "tool_call",
        )
        .group_by(RuntimeSessionActivity.tool_name, RuntimeSessionActivity.server_name)
    ).all()
    for tool_name, server_name, last in rows:
        if tool_name:
            tools[tool_name] = _latest(tools.get(tool_name), last)
        if server_name:
            servers[server_name] = _latest(servers.get(server_name), last)
    return tools, servers


def _tool_rows(
    db: Session,
    *,
    account_id: Any,
    owners: dict[str, str],
    changes: dict[tuple[str, str], Any],
    last_seen: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = _guard(
        list(
            db.execute(
                select(ToolConfiguration)
                .where(ToolConfiguration.account_id == account_id)
                .order_by(ToolConfiguration.created_at, ToolConfiguration.id)
            ).scalars()
        ),
        ASSET_TOOL,
    )
    rules: dict[str, list[ToolAccessRule]] = {}
    for rule in db.execute(
        select(ToolAccessRule)
        .where(ToolAccessRule.account_id == account_id)
        .order_by(ToolAccessRule.priority)
    ).scalars():
        rules.setdefault(str(rule.tool_configuration_id), []).append(rule)
    workflows = {
        str(row.id): row.name
        for row in db.execute(
            select(ApprovalWorkflow).where(ApprovalWorkflow.account_id == account_id)
        ).scalars()
    }
    out = []
    for tool in rows:
        policies = [
            f"rule:{rule.action}"
            + (" (disabled)" if not rule.is_enabled else "")
            + f" priority {rule.priority}"
            for rule in rules.get(str(tool.id), [])
        ]
        workflow_name = workflows.get(str(tool.approval_workflow_id))
        if workflow_name:
            policies.append(f"approval_workflow:{workflow_name}")
        changed_at, changed_by = _config_change_cells(
            changes, owners, ASSET_TOOL, (tool.id, tool.tool_name)
        )
        out.append(
            {
                "record_type": ASSET_TOOL,
                "asset_id": str(tool.id),
                "name": tool.tool_name,
                "asset_kind": tool.tool_source,
                "provider": None,
                "location": None,
                "owner_username": None,
                "owner_user_id": None,
                "lifecycle_state": "enabled" if tool.is_enabled else "disabled",
                "first_seen": _iso(tool.created_at),
                "last_seen": _iso(last_seen.get(tool.tool_name)),
                "last_seen_source": "runtime_session_activity.tool_call",
                "attached_policies": policies,
                "attached_policy_count": len(policies),
                "parent_asset_id": _str_or_none(tool.mcp_server_id),
                "last_config_change_at": changed_at,
                "last_config_change_by": changed_by,
                "source_table": "tool_configuration",
            }
        )
    return out


def _mcp_server_rows(
    db: Session,
    *,
    account_id: Any,
    owners: dict[str, str],
    changes: dict[tuple[str, str], Any],
    last_seen: dict[str, Any],
    rules: dict[str, list[str]],
) -> list[dict[str, Any]]:
    rows = _guard(
        list(
            db.execute(
                select(MCPServer)
                .where(MCPServer.account_id == account_id)
                .order_by(MCPServer.created_at, MCPServer.id)
            ).scalars()
        ),
        ASSET_MCP_SERVER,
    )
    out = []
    for server in rows:
        changed_at, changed_by = _config_change_cells(
            changes, owners, ASSET_MCP_SERVER, (server.id, server.name)
        )
        # An MCP server is an ICT service the agents reach. How Preloop
        # authenticates to it is part of what the asset is, so it rides in
        # asset_kind next to the transport ("http/oauth") rather than being
        # filed under policies, which are attachments, not properties.
        policies = sorted(rules.get(str(server.id), []))
        out.append(
            {
                "record_type": ASSET_MCP_SERVER,
                "asset_id": str(server.id),
                "name": server.name,
                "asset_kind": f"{server.transport}/{server.auth_type}",
                "provider": None,
                "location": server.url,
                "owner_username": None,
                "owner_user_id": None,
                "lifecycle_state": server.status,
                "first_seen": _iso(server.created_at),
                "last_seen": _iso(last_seen.get(server.name) or server.last_scan_at),
                "last_seen_source": "runtime_session_activity.tool_call server_name",
                "attached_policies": policies,
                "attached_policy_count": len(policies),
                "parent_asset_id": None,
                "last_config_change_at": changed_at,
                "last_config_change_by": changed_by,
                "source_table": "mcp_server",
            }
        )
    return out


def _model_usage(db: Session, account_id: Any):
    """Last gateway call per model id and per provider name."""
    by_model: dict[str, Any] = {}
    by_provider: dict[str, Any] = {}
    rows = db.execute(
        select(
            ApiUsage.ai_model_id,
            ApiUsage.provider_name,
            func.max(ApiUsage.timestamp),
            func.min(ApiUsage.timestamp),
        )
        .where(ApiUsage.account_id == account_id)
        .group_by(ApiUsage.ai_model_id, ApiUsage.provider_name)
    ).all()
    for model_id, provider_name, last, first in rows:
        if model_id:
            by_model[str(model_id)] = _latest(by_model.get(str(model_id)), last)
        if provider_name:
            key = provider_name.strip().lower()
            entry = by_provider.setdefault(
                key, {"first": first, "last": last, "name": provider_name}
            )
            entry["last"] = _latest(entry["last"], last)
            if first is not None and (entry["first"] is None or first < entry["first"]):
                entry["first"] = first
    return by_model, by_provider


def _model_rows(
    db: Session,
    *,
    account_id: Any,
    owners: dict[str, str],
    budgets: Sequence[BudgetPolicy],
    changes: dict[tuple[str, str], Any],
    usage: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = _guard(
        list(
            db.execute(
                select(AIModel)
                .where(AIModel.account_id == account_id)
                .order_by(AIModel.created_at, AIModel.id)
            ).scalars()
        ),
        ASSET_MODEL,
    )
    out = []
    for model in rows:
        aliases = {
            value.strip().lower()
            for value in (model.name, model.model_identifier)
            if value
        }
        policies = [
            _budget_label(policy)
            for policy in budgets
            if policy.model_alias and policy.model_alias.strip().lower() in aliases
        ]
        changed_at, changed_by = _config_change_cells(
            changes, owners, ASSET_MODEL, (model.id, model.name, model.model_identifier)
        )
        out.append(
            {
                "record_type": ASSET_MODEL,
                "asset_id": str(model.id),
                "name": model.name,
                "asset_kind": model.model_identifier,
                "provider": model.provider_name,
                "location": model.api_endpoint,
                "owner_username": None,
                "owner_user_id": None,
                "lifecycle_state": "default" if model.is_default else "configured",
                "first_seen": _iso(model.created_at),
                "last_seen": _iso(usage.get(str(model.id))),
                "last_seen_source": "api_usage.timestamp",
                "attached_policies": sorted(policies),
                "attached_policy_count": len(policies),
                "parent_asset_id": _provider_asset_id(model.provider_name),
                "last_config_change_at": changed_at,
                "last_config_change_by": changed_by,
                "source_table": "ai_model",
            }
        )
    return out


def _provider_asset_id(name: Optional[str]) -> Optional[str]:
    """Stable identifier for a provider row.

    A provider is not a table: it is the third-party service the models point
    at, which is exactly the thing an Art. 28 register wants a line for. The
    id is derived from the name so two exports agree.
    """
    if not name:
        return None
    return f"provider:{name.strip().lower()}"


def _provider_rows(
    db: Session,
    *,
    account_id: Any,
    models: Sequence[dict[str, Any]],
    usage: dict[str, Any],
) -> list[dict[str, Any]]:
    """One row per distinct provider, derived from models and gateway usage."""
    providers: dict[str, dict[str, Any]] = {}
    for model in models:
        name = model.get("provider")
        if not name:
            continue
        key = name.strip().lower()
        entry = providers.setdefault(
            key,
            {
                "name": name,
                "models": [],
                "policies": set(),
                "first_seen": model.get("first_seen"),
                "last_seen": model.get("last_seen"),
            },
        )
        entry["models"].append(model["name"])
        entry["policies"].update(model.get("attached_policies") or [])
        if model.get("first_seen") and (
            not entry["first_seen"] or model["first_seen"] < entry["first_seen"]
        ):
            entry["first_seen"] = model["first_seen"]
        if model.get("last_seen") and (
            not entry["last_seen"] or model["last_seen"] > entry["last_seen"]
        ):
            entry["last_seen"] = model["last_seen"]
    for key, entry in usage.items():
        provider = providers.setdefault(
            key,
            {
                "name": entry["name"],
                "models": [],
                "policies": set(),
                "first_seen": None,
                "last_seen": None,
            },
        )
        last = _iso(entry["last"])
        first = _iso(entry["first"])
        if last and (not provider["last_seen"] or last > provider["last_seen"]):
            provider["last_seen"] = last
        if first and (not provider["first_seen"] or first < provider["first_seen"]):
            provider["first_seen"] = first
    out = []
    for key in sorted(providers):
        entry = providers[key]
        out.append(
            {
                "record_type": ASSET_PROVIDER,
                "asset_id": _provider_asset_id(entry["name"]),
                "name": entry["name"],
                "asset_kind": "model_provider",
                "provider": entry["name"],
                "location": None,
                "owner_username": None,
                "owner_user_id": None,
                "lifecycle_state": "in_use" if entry["last_seen"] else "configured",
                "first_seen": entry["first_seen"],
                "last_seen": entry["last_seen"],
                "last_seen_source": "api_usage.timestamp",
                # Budgets that constrain spend at this third party, inherited
                # from its models: the provider line is derived, so its
                # controls are the union of the controls on what points at it.
                "attached_policies": sorted(entry["policies"]),
                "attached_policy_count": len(entry["policies"]),
                "parent_asset_id": None,
                "last_config_change_at": None,
                "last_config_change_by": None,
                "source_table": "ai_model + api_usage (derived)",
            }
        )
    return out


def _runner_rows(
    db: Session,
    *,
    account_id: Any,
    owners: dict[str, str],
    changes: dict[tuple[str, str], Any],
) -> list[dict[str, Any]]:
    rows = _guard(
        list(
            db.execute(
                select(FlowRunner)
                .where(FlowRunner.account_id == account_id)
                .order_by(FlowRunner.created_at, FlowRunner.id)
            ).scalars()
        ),
        ASSET_RUNNER_HOST,
    )
    out = []
    for runner in rows:
        changed_at, changed_by = _config_change_cells(
            changes, owners, ASSET_RUNNER_HOST, (runner.id, runner.name)
        )
        platform = " ".join(part for part in (runner.os, runner.arch) if part)
        out.append(
            {
                "record_type": ASSET_RUNNER_HOST,
                "asset_id": str(runner.id),
                "name": runner.name,
                "asset_kind": platform or None,
                "provider": None,
                "location": runner.hostname,
                "owner_username": owners.get(str(runner.registered_by_user_id)),
                "owner_user_id": _str_or_none(runner.registered_by_user_id),
                "lifecycle_state": runner.status,
                "first_seen": _iso(runner.created_at),
                "last_seen": _iso(runner.last_heartbeat),
                "last_seen_source": "flow_runner.last_heartbeat",
                # Nothing attaches a policy to a runner host today. The cell
                # is empty rather than absent, per the edition rule: it will
                # fill in when runner-scoped policies exist.
                "attached_policies": [],
                "attached_policy_count": 0,
                "parent_asset_id": None,
                "last_config_change_at": changed_at,
                "last_config_change_by": changed_by,
                "source_table": "flow_runner",
            }
        )
    return out


def build_asset_register(
    db: Session,
    *,
    account: Any,
    export_format: str = FORMAT_JSON,
    generated_at: Optional[datetime] = None,
) -> DoraExport:
    """Every AI-agent asset this account holds, as one flat table.

    Feeds the entity's Art. 8 ICT asset inventory and the agent-slice lines of
    its Art. 28 register of information. It is not either document.
    """
    export_format = normalize_format(export_format)
    account_id = account.id
    owners = _owner_names(db, account_id)
    budgets = _budget_policies(db, account_id)
    changes = _config_change_index(db, account_id)
    tool_last_seen, server_last_seen = _tool_last_seen(db, account_id)
    model_usage, provider_usage = _model_usage(db, account_id)

    agents = _agent_rows(
        db, account_id=account_id, owners=owners, budgets=budgets, changes=changes
    )
    tools = _tool_rows(
        db,
        account_id=account_id,
        owners=owners,
        changes=changes,
        last_seen=tool_last_seen,
    )
    # A server's controls are the controls on the tools it exposes: the rule
    # is attached to the tool, but the asset an auditor asks about is the
    # server, so the line carries them with the tool named.
    server_policies: dict[str, list[str]] = {}
    for tool in tools:
        parent = tool.get("parent_asset_id")
        if not parent:
            continue
        server_policies.setdefault(parent, []).extend(
            f"{label} via {tool['name']}" for label in tool["attached_policies"]
        )
    servers = _mcp_server_rows(
        db,
        account_id=account_id,
        owners=owners,
        changes=changes,
        last_seen=server_last_seen,
        rules=server_policies,
    )
    models = _model_rows(
        db,
        account_id=account_id,
        owners=owners,
        budgets=budgets,
        changes=changes,
        usage=model_usage,
    )
    providers = _provider_rows(
        db, account_id=account_id, models=models, usage=provider_usage
    )
    runners = _runner_rows(db, account_id=account_id, owners=owners, changes=changes)

    rows = agents + tools + servers + models + providers + runners
    counts = {
        ASSET_AGENT: len(agents),
        ASSET_TOOL: len(tools),
        ASSET_MCP_SERVER: len(servers),
        ASSET_MODEL: len(models),
        ASSET_PROVIDER: len(providers),
        ASSET_RUNNER_HOST: len(runners),
    }
    body = render_rows(rows, ASSET_COLUMNS, export_format)
    stamp = generated_at or datetime.now(UTC)
    member = f"{ASSET_MEMBER_STEM}.{export_format}"
    manifest = _build_manifest(
        schema=ASSET_MANIFEST_SCHEMA,
        account_id=account_id,
        generated_at=stamp,
        member_name=member,
        body=body,
        columns=ASSET_COLUMNS,
        counts=counts,
        extra={
            "format": export_format,
            "record_types": list(ASSET_RECORD_TYPES),
            "edition": _edition_block(ASSET_EE_FIELDS),
            "account_policies": [
                _budget_label(policy)
                for policy in sorted(
                    (p for p in budgets if p.subject_type == "account"),
                    key=lambda p: (p.model_alias or "", str(p.id)),
                )
            ],
            "feeds": [
                "DORA Art. 8 (ICT asset inventory), agent slice",
                "DORA Art. 28 (register of information), agent slice",
            ],
        },
    )
    return DoraExport(
        kind=ASSET_MEMBER_STEM,
        export_format=export_format,
        columns=ASSET_COLUMNS,
        rows=rows,
        body=body,
        manifest=manifest,
        counts=counts,
    )


# --- Incident candidates -------------------------------------------------


def _agent_index(db: Session, account_id: Any) -> dict[str, tuple[str, str]]:
    """Lookup keys -> (agent id, agent name).

    Four keys because four record shapes name an agent four ways: its own id,
    a runtime session id (gateway usage), a source type + source id pair
    (runtime principals on usage rows), and a bare source id (the agent
    session reference an execution records).
    """
    index: dict[str, tuple[str, str]] = {}
    for agent in db.execute(
        select(ManagedAgent).where(ManagedAgent.account_id == account_id)
    ).scalars():
        entry = (str(agent.id), agent.display_name)
        index[str(agent.id)] = entry
        if agent.runtime_session_id:
            index[f"session:{agent.runtime_session_id}"] = entry
        if agent.session_source_id:
            index[f"source:{agent.session_source_id}"] = entry
        if agent.session_source_type and agent.session_source_id:
            index[
                f"principal:{agent.session_source_type}:{agent.session_source_id}"
            ] = entry
    return index


def _resolve_agent(
    index: dict[str, tuple[str, str]], *keys: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    for key in keys:
        if not key:
            continue
        entry = index.get(str(key))
        if entry:
            return entry
    return (None, None)


def _execution_failure_rows(
    db: Session,
    *,
    account_id: Any,
    start: datetime,
    end: datetime,
    agents: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Executions that ended in a failure status inside the period.

    ``flow_execution`` carries no account id: it is owned through its flow,
    so the join is the ownership check.
    """
    occurred = func.coalesce(FlowExecution.end_time, FlowExecution.start_time)
    rows = _guard(
        list(
            db.execute(
                select(FlowExecution, Flow.name)
                .join(Flow, Flow.id == FlowExecution.flow_id)
                .where(
                    Flow.account_id == account_id,
                    FlowExecution.status.in_(sorted(FAILURE_STATUSES)),
                    occurred >= start,
                    occurred < end,
                )
                .order_by(occurred, FlowExecution.id)
            ).all()
        ),
        INCIDENT_EXECUTION_FAILURE,
    )
    out = []
    for execution, flow_name in rows:
        reference = _str_or_none(execution.agent_session_reference)
        agent_id, agent_name = _resolve_agent(
            agents, f"source:{reference}" if reference else None, reference
        )
        out.append(
            {
                "record_type": INCIDENT_EXECUTION_FAILURE,
                "occurred_at": _iso(execution.end_time or execution.start_time),
                "record_id": str(execution.id),
                # An execution is its own correlation key: the audit rows for
                # its tool calls carry it, and so does its evidence pack.
                "correlation_id": str(execution.id),
                "correlation_source": "flow_execution.id",
                "agent_id": agent_id,
                "agent_name": agent_name,
                "flow_id": str(execution.flow_id),
                "execution_id": str(execution.id),
                "runtime_session_id": None,
                "subject": flow_name,
                "provider": None,
                "platform_category": execution.failure_category or execution.status,
                "status_code": None,
                "actor_user_id": None,
                "detail": _clip(execution.error_message),
                "source_table": "flow_execution",
            }
        )
    return out


def _audit_rows(
    db: Session,
    *,
    account_id: Any,
    start: datetime,
    end: datetime,
    actions: Sequence[str],
) -> list[AuditLog]:
    return list(
        db.execute(
            select(AuditLog)
            .where(
                AuditLog.account_id == account_id,
                AuditLog.action.in_(list(actions)),
                AuditLog.timestamp >= start,
                AuditLog.timestamp < end,
            )
            .order_by(AuditLog.timestamp, AuditLog.id)
        ).scalars()
    )


def _kill_switch_rows(
    db: Session, *, account_id: Any, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Kill-switch activations, one row per scope, as they are audited.

    Deactivations are deliberately out: this export lists candidates for an
    incident, and re-enabling traffic is the recovery, not the event. The
    halt row itself keeps both sides for whoever reads the audit trail.
    """
    rows = _guard(
        _audit_rows(
            db,
            account_id=account_id,
            start=start,
            end=end,
            actions=(AUDIT_ACTION_KILL_SWITCH,),
        ),
        INCIDENT_KILL_SWITCH,
    )
    out = []
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        out.append(
            {
                "record_type": INCIDENT_KILL_SWITCH,
                "occurred_at": _iso(row.timestamp),
                "record_id": str(row.id),
                "correlation_id": None,
                "correlation_source": None,
                "agent_id": None,
                "agent_name": None,
                "flow_id": None,
                "execution_id": None,
                "runtime_session_id": None,
                "subject": details.get("scope"),
                "provider": None,
                "platform_category": f"halt_scope:{details.get('scope')}",
                "status_code": None,
                "actor_user_id": _str_or_none(row.user_id),
                "detail": _clip(details.get("reason")),
                "source_table": "audit_log",
            }
        )
    return out


def _policy_deny_rows(
    db: Session,
    *,
    account_id: Any,
    start: datetime,
    end: datetime,
    agents: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Policy denials, where a deployment persists them.

    Only the Enterprise audit plugin writes ``policy_deny`` rows
    (``preloop/services/policy_evaluator.py`` returns early without it), so
    on OSS this list is empty and the manifest says why rather than letting
    an empty column read as "nothing was ever denied".
    """
    rows = _guard(
        _audit_rows(
            db,
            account_id=account_id,
            start=start,
            end=end,
            actions=(AUDIT_ACTION_POLICY_DENY,),
        ),
        INCIDENT_POLICY_DENY,
    )
    out = []
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        execution_id = details.get("execution_id")
        agent_id, agent_name = _resolve_agent(
            agents,
            details.get("managed_agent_id"),
            f"session:{details.get('runtime_session_id')}"
            if details.get("runtime_session_id")
            else None,
        )
        out.append(
            {
                "record_type": INCIDENT_POLICY_DENY,
                "occurred_at": _iso(row.timestamp),
                "record_id": str(row.id),
                "correlation_id": details.get("correlation_id"),
                "correlation_source": "audit_log.details.correlation_id",
                "agent_id": agent_id,
                "agent_name": agent_name,
                "flow_id": None,
                "execution_id": _str_or_none(execution_id),
                "runtime_session_id": _str_or_none(details.get("runtime_session_id")),
                "subject": details.get("tool_name") or row.resource_id,
                "provider": None,
                "platform_category": "policy_deny",
                "status_code": None,
                "actor_user_id": _str_or_none(row.user_id),
                "detail": _clip(
                    details.get("rule_description") or details.get("condition_matched")
                ),
                "source_table": "audit_log",
            }
        )
    return out


def _budget_breach_rows(
    db: Session,
    *,
    account_id: Any,
    start: datetime,
    end: datetime,
    agents: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Requests the gateway refused because a budget was already spent.

    The audit row, not the usage row: ``budget_denied`` is the audit outcome
    the gateway computes (``openai_gateway._audit_outcome``) and it is
    written in every edition, whereas the usage row for a denial carries no
    error class to filter on.
    """
    rows = [
        row
        for row in _audit_rows(
            db,
            account_id=account_id,
            start=start,
            end=end,
            actions=(AUDIT_ACTION_GATEWAY_REQUEST,),
        )
        if row.status == AUDIT_STATUS_BUDGET_DENIED
    ]
    _guard(rows, INCIDENT_BUDGET_BREACH)
    out = []
    for row in rows:
        details = row.details if isinstance(row.details, dict) else {}
        session_id = details.get("runtime_session_id")
        agent_id, agent_name = _resolve_agent(
            agents,
            f"session:{session_id}" if session_id else None,
            f"principal:{details.get('runtime_principal_type')}:"
            f"{details.get('runtime_principal_id')}"
            if details.get("runtime_principal_id")
            else None,
        )
        out.append(
            {
                "record_type": INCIDENT_BUDGET_BREACH,
                "occurred_at": _iso(row.timestamp),
                "record_id": str(row.id),
                "correlation_id": details.get("api_usage_id") or row.resource_id,
                "correlation_source": "api_usage.id",
                "agent_id": agent_id,
                "agent_name": agent_name,
                "flow_id": _str_or_none(details.get("flow_id")),
                "execution_id": _str_or_none(details.get("flow_execution_id")),
                "runtime_session_id": _str_or_none(session_id),
                "subject": details.get("model_alias") or details.get("requested_model"),
                "provider": details.get("provider_name")
                or details.get("gateway_provider"),
                "platform_category": "budget_denied",
                "status_code": details.get("status_code"),
                "actor_user_id": _str_or_none(row.user_id),
                "detail": _clip(details.get("error_detail")),
                "source_table": "audit_log",
            }
        )
    return out


def _gateway_failure_rows(
    db: Session,
    *,
    account_id: Any,
    start: datetime,
    end: datetime,
    agents: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Gateway calls that failed at (or on the way to) the provider.

    Read from the usage ledger rather than the audit trail because
    ``ApiUsage.error_class`` is the one place the upstream taxonomy is
    stored, and the ledger is written in every edition. The account's own
    halt (``kill_switch``) and a client cancelling its own request are not
    upstream failures and are excluded by the class list.
    """
    rows = _guard(
        list(
            db.execute(
                select(ApiUsage)
                .where(
                    ApiUsage.account_id == account_id,
                    ApiUsage.timestamp >= start,
                    ApiUsage.timestamp < end,
                    or_(
                        ApiUsage.error_class.in_(list(UPSTREAM_ERROR_CLASSES)),
                        ApiUsage.status_code >= 500,
                    ),
                    func.coalesce(ApiUsage.error_class, "") != KILL_SWITCH_ERROR_CLASS,
                )
                .order_by(ApiUsage.timestamp, ApiUsage.id)
            ).scalars()
        ),
        INCIDENT_GATEWAY_UPSTREAM_FAILURE,
    )
    out = []
    for usage in rows:
        agent_id, agent_name = _resolve_agent(
            agents,
            f"session:{usage.runtime_session_id}" if usage.runtime_session_id else None,
            f"principal:{usage.runtime_principal_type}:{usage.runtime_principal_id}"
            if usage.runtime_principal_id
            else None,
        )
        out.append(
            {
                "record_type": INCIDENT_GATEWAY_UPSTREAM_FAILURE,
                "occurred_at": _iso(usage.timestamp),
                "record_id": str(usage.id),
                "correlation_id": usage.upstream_request_id or str(usage.id),
                "correlation_source": (
                    "api_usage.upstream_request_id"
                    if usage.upstream_request_id
                    else "api_usage.id"
                ),
                "agent_id": agent_id,
                "agent_name": agent_name,
                "flow_id": _str_or_none(usage.flow_id),
                "execution_id": _str_or_none(usage.flow_execution_id),
                "runtime_session_id": _str_or_none(usage.runtime_session_id),
                "subject": usage.model_alias,
                "provider": usage.provider_name,
                "platform_category": usage.error_class or f"http_{usage.status_code}",
                "status_code": usage.status_code,
                "actor_user_id": _str_or_none(usage.user_id),
                "detail": _clip(usage.endpoint),
                "source_table": "api_usage",
            }
        )
    return out


def build_incident_candidates(
    db: Session,
    *,
    account: Any,
    start: datetime,
    end: datetime,
    export_format: str = FORMAT_JSON,
    generated_at: Optional[datetime] = None,
) -> DoraExport:
    """Everything in the period that an entity might have to classify.

    ``start`` is inclusive and ``end`` exclusive, so consecutive periods tile
    without a row landing in both files or in neither: the same boundary rule
    the period export uses.
    """
    export_format = normalize_format(export_format)
    if end <= start:
        raise DoraExportError("invalid_period", "end must be after start")
    account_id = account.id
    agents = _agent_index(db, account_id)
    # Every column filtered below is a naive ``DateTime`` holding UTC
    # (flow_execution.start_time, audit_log.timestamp, api_usage.timestamp).
    # Comparing them against an aware bound would make Postgres cast through
    # the session time zone, which silently moves the period boundary, so the
    # bounds are converted to naive UTC once, here.
    lower = start.astimezone(UTC).replace(tzinfo=None)
    upper = end.astimezone(UTC).replace(tzinfo=None)

    failures = _execution_failure_rows(
        db, account_id=account_id, start=lower, end=upper, agents=agents
    )
    halts = _kill_switch_rows(db, account_id=account_id, start=lower, end=upper)
    denies = _policy_deny_rows(
        db, account_id=account_id, start=lower, end=upper, agents=agents
    )
    budgets = _budget_breach_rows(
        db, account_id=account_id, start=lower, end=upper, agents=agents
    )
    upstream = _gateway_failure_rows(
        db, account_id=account_id, start=lower, end=upper, agents=agents
    )

    rows = sorted(
        failures + halts + denies + budgets + upstream,
        key=lambda row: (
            row["occurred_at"] or "",
            row["record_type"],
            row["record_id"],
        ),
    )
    counts = {
        INCIDENT_EXECUTION_FAILURE: len(failures),
        INCIDENT_KILL_SWITCH: len(halts),
        INCIDENT_POLICY_DENY: len(denies),
        INCIDENT_BUDGET_BREACH: len(budgets),
        INCIDENT_GATEWAY_UPSTREAM_FAILURE: len(upstream),
    }
    body = render_rows(rows, INCIDENT_COLUMNS, export_format)
    stamp = generated_at or datetime.now(UTC)
    member = f"{INCIDENT_MEMBER_STEM}.{export_format}"
    edition = _edition_block(())
    if not edition["audit_plugin"]:
        edition["record_types_absent"] = [
            {
                "record_type": INCIDENT_POLICY_DENY,
                "reason": (
                    "policy decisions are persisted by the Enterprise audit "
                    "plugin; this deployment has none, so zero policy_deny "
                    "rows means unrecorded, not that no tool call was denied"
                ),
            }
        ]
    manifest = _build_manifest(
        schema=INCIDENT_MANIFEST_SCHEMA,
        account_id=account_id,
        generated_at=stamp,
        member_name=member,
        body=body,
        columns=INCIDENT_COLUMNS,
        counts=counts,
        extra={
            "format": export_format,
            "record_types": list(INCIDENT_RECORD_TYPES),
            "period": {
                "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "boundary": "start inclusive, end exclusive",
            },
            "edition": edition,
            "classification": CLASSIFICATION_NOTE,
            "feeds": [
                "DORA Art. 17 (ICT-related incident management), agent slice; "
                "classification and reporting remain the entity's"
            ],
        },
    )
    return DoraExport(
        kind=INCIDENT_MEMBER_STEM,
        export_format=export_format,
        columns=INCIDENT_COLUMNS,
        rows=rows,
        body=body,
        manifest=manifest,
        counts=counts,
    )


def audit_dora_export(
    db: Session,
    *,
    account_id: Any,
    user_id: Any,
    export: DoraExport,
    action: str,
) -> None:
    """Record that the register (or the period's candidates) left the platform.

    An export is a bulk read of an account's compliance record. Who took it,
    when, for what period, and the digest of what they got are exactly the
    questions asked afterwards.
    """
    period = export.manifest.get("period") or {}
    try:
        crud_audit_log.log_action(
            db,
            account_id=account_id,
            user_id=user_id,
            action=action,
            resource_type="dora_export",
            resource_id=export.kind,
            status="success",
            details={
                "format": export.export_format,
                "counts": export.counts,
                "total_rows": len(export.rows),
                "period_start": period.get("start"),
                "period_end": period.get("end"),
                "body_sha256": export.sha256,
                "members_digest": export.manifest.get("members_digest"),
                "size_bytes": len(export.body),
            },
        )
    except Exception:
        db.rollback()
        logger.error("Failed to audit DORA export", exc_info=True)


def json_envelope(export: DoraExport) -> dict[str, Any]:
    """The JSON body: manifest first, then the rows it digests.

    The rows are the parsed form of the exact bytes the manifest hashed, so a
    verifier can recompute ``members[0].sha256`` by canonicalising ``rows``
    with the same helper the evidence pack uses.
    """
    return {"manifest": export.manifest, "rows": export.rows}
