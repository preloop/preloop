"""Bounded fleet graph over authorized, tenant-owned source projections."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime
from itertools import zip_longest
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.auth.key_scopes import is_mcp_only_api_key, is_single_purpose_api_key
from preloop.models import crud, models
from preloop.models.crud.fleet_map import Source, list_resources
from preloop.models.models.managed_agent_ai_model_binding import (
    AUTOREGISTER_CONFIG_KEY_PREFIX,
)
from preloop.plugins.account_hooks import (
    AuthorizationContext,
    VISIBLE_AI_MODEL,
    VISIBLE_MANAGED_AGENT,
    VISIBLE_RUNNER,
    authorize,
    filter_viewable,
)
from preloop.plugins.base import get_plugin_manager
from preloop.schemas.fleet_map import (
    FleetMapAction,
    FleetMapCluster,
    FleetMapEdge,
    FleetMapNode,
    FleetMapResponse,
    NodeType,
)
from preloop.services.built_in_models import built_in_models_enabled
from preloop.services.runner_presence import derive_state
from preloop.utils.permissions import user_holds_permission

logger = logging.getLogger(__name__)
SOURCE_LIMIT = 1000
BINDING_LIMIT = 10000
EDGE_LIMIT = 10000
FLEET_HREF = "/console/settings/device-fleet"
MODELS_HREF = "/console/ai-models"


def _allowed(db: Session, user: models.User, permission: str, owner: bool) -> bool:
    """Apply both endpoint authorizers and the role ceiling to each source."""
    decision = authorize(
        AuthorizationContext(account_id=user.account_id, db=db, user=user),
        permission,
        None,
    )
    return decision.allowed and (
        bool(user.is_superuser) or owner or user_holds_permission(db, user, permission)
    )


def _reject_report_key(user: models.User) -> None:
    """Do not widen discovery or device-report credentials into fleet reads."""
    key = getattr(user, "_auth_api_key", None)
    context = getattr(key, "context_data", None)
    if (
        is_single_purpose_api_key(key)
        or is_mcp_only_api_key(key)
        or (isinstance(context, dict) and "device_report" in context)
    ):
        raise HTTPException(403, "Report-only credentials cannot read the fleet map")


def build_fleet_map(
    db: Session, *, user: models.User, limit: int = 500
) -> FleetMapResponse:
    """Load each source once, filter before linking, and cap the final graph.

    Args:
        db: Request database session.
        user: Authenticated user in the selected account.
        limit: Maximum visible resource nodes, between 50 and 1000.

    Returns:
        Allowlisted graph with explicit source and output truncation.

    Raises:
        HTTPException: The caller cannot view the account's fleet.
    """
    _reject_report_key(user)
    if not 50 <= limit <= 1000:
        raise HTTPException(422, "Fleet map limit must be between 50 and 1000")
    account = crud.crud_account.get(db, id=user.account_id)
    owner = account is not None and str(account.primary_user_id) == str(user.id)
    if not _allowed(db, user, "view_device_fleet", owner):
        raise HTTPException(
            403, "Insufficient permissions. Required: view_device_fleet"
        )
    result = FleetMapResponse(limit=limit)
    account_id = str(user.account_id)
    permissions = {
        name: _allowed(db, user, name, owner)
        for name in (
            "view_agents",
            "view_ai_models",
            "view_flows",
            "view_users",
            "edit_ai_models",
            "manage_agents",
        )
    }

    source_rows: dict[str, list[Any]] = {}
    partial_sources: set[str] = set()

    def read(
        name: Source, permission: str, resource_type: str | None = None
    ) -> list[Any]:
        if not permissions[permission]:
            return []
        rows = list_resources(
            db,
            source=name,
            account_id=account_id,
            limit=SOURCE_LIMIT + 1,
            include_built_in=name == "models" and built_in_models_enabled(),
        )
        result.truncated |= len(rows) > SOURCE_LIMIT
        if len(rows) > SOURCE_LIMIT:
            partial_sources.add(name)
        rows = rows[:SOURCE_LIMIT]
        source_rows[name] = rows
        if not resource_type:
            return rows
        if name == "models":
            # Match the Models console: available built-ins are public catalog
            # entries, not tenant resources governed by resource access rules.
            owned = [row for row in rows if row.account_id is not None]
            visible_ids = {
                row.id for row in filter_viewable(db, user, resource_type, owned)
            }
            return [
                row for row in rows if row.account_id is None or row.id in visible_ids
            ]
        return filter_viewable(db, user, resource_type, rows)

    agents = read("agents", "view_agents", VISIBLE_MANAGED_AGENT)
    ai_models = read("models", "view_ai_models", VISIBLE_AI_MODEL)
    runners = read("runners", "view_flows", VISIBLE_RUNNER)
    users = read("users", "view_users")
    bindings = (
        crud.crud_managed_agent_ai_model_binding.list_for_fleet_map(
            db,
            account_id=account_id,
            agent_ids=[str(a.id) for a in agents],
            limit=BINDING_LIMIT + 1,
        )
        if agents and ai_models
        else []
    )
    result.truncated |= len(bindings) > BINDING_LIMIT
    nodes: dict[str, FleetMapNode] = {}
    edges: dict[str, FleetMapEdge] = {}

    def node(
        kind: NodeType, resource_id: Any, label: str, href: str, **kwargs: Any
    ) -> FleetMapNode:
        identifier = f"{kind}:{resource_id}"
        if identifier not in nodes:
            nodes[identifier] = FleetMapNode(
                id=identifier,
                type=kind,
                resource_id=str(resource_id),
                label=label,
                href=href,
                **kwargs,
            )
        return nodes[identifier]

    def edge(
        source: str,
        target: str,
        kind: str,
        provenance: str = "configured",
        **kwargs: Any,
    ) -> None:
        if source not in nodes or target not in nodes:
            return
        identifier = f"{kind}:{source}:{target}:{provenance}"
        edges[identifier] = FleetMapEdge(
            id=identifier,
            source=source,
            target=target,
            kind=kind,
            provenance=provenance,
            impact_source=target,
            impact_target=source,
            **kwargs,
        )

    for row in users:
        node(
            "user",
            row.id,
            row.full_name or row.username,
            f"/console/settings/users?user={row.id}",
        )
    for row in agents:
        owners = [f"user:{row.owner_user_id}"] if row.owner_user_id else []
        item = node(
            "agent",
            row.id,
            row.display_name or row.agent_kind,
            f"/console/agents/{quote(str(row.id), safe='')}",
            detail=row.agent_kind,
            status=row.lifecycle_state,
            owner_ids=[uid for uid in owners if uid in nodes],
            gaps=["unknown_device"],
        )
        if permissions["manage_agents"]:
            item.actions.append(
                FleetMapAction(label="Manage agent access", href=item.href)
            )
        for uid in item.owner_ids:
            edge(uid, item.id, "owns")
    now = datetime.now(UTC)
    for row in runners:
        owners = (
            [f"user:{row.registered_by_user_id}"] if row.registered_by_user_id else []
        )
        status = derive_state(row, now)
        item = node(
            "runner",
            row.id,
            row.name,
            f"/console/settings/runners?runner={row.id}",
            status=status,
            owner_ids=[uid for uid in owners if uid in nodes],
            gaps=["offline_runner"] if status == "offline" else [],
        )

        for uid in item.owner_ids:
            edge(uid, item.id, "owns")

    model_accounts: dict[str, str] = {}
    for row in ai_models:
        provider = node("provider", row.provider_name, row.provider_name, MODELS_HREF)
        provider.provider_ids = [provider.id]
        model_href = f"{MODELS_HREF}/{quote(str(row.id), safe='')}"
        item = node("model", row.id, row.name, model_href, provider_ids=[provider.id])
        edge(item.id, provider.id, "provided_by")
        # Hosted model routing and operator credentials are not tenant resources.
        if row.account_id is None:
            continue
        if row.runner_id:
            edge(item.id, f"runner:{row.runner_id}", "runs_on")
        # UUID references establish sharing. Legacy/ambient credentials do not
        # establish that two model rows use the same provider account.
        if (
            not row.credentials_secret_id
            and not row.api_key
            and not row.uses_ambient_credentials
        ):
            continue
        reference = (
            str(row.credentials_secret_id)
            if row.credentials_secret_id
            else f"model:{row.id}"
        )
        account_node = node(
            "provider_account",
            reference,
            f"{row.provider_name} account {reference[-6:]}",
            model_href,
            detail="Credential reference",
            provider_ids=[provider.id],
        )
        if provider.id not in account_node.provider_ids:
            account_node.provider_ids.append(provider.id)
        action = FleetMapAction(label="Manage credentials", href=model_href)
        if permissions["edit_ai_models"] and not account_node.actions:
            account_node.actions.append(action)
        model_accounts[item.id] = account_node.id
        edge(item.id, account_node.id, "uses_account")
        edge(account_node.id, provider.id, "account_of_provider")

    def model_link(
        agent_id: str, model_id: str, provenance: str, gaps: list[str]
    ) -> None:
        aid, mid = f"agent:{agent_id}", f"model:{model_id}"
        if aid not in nodes or mid not in nodes:
            return
        edge(aid, mid, "uses_model", provenance, gaps=gaps)
        if mid in model_accounts:
            edge(aid, model_accounts[mid], "uses_account", provenance, gaps=gaps)
        nodes[aid].gaps = sorted(set(nodes[aid].gaps + gaps))
        nodes[aid].provider_ids = sorted(
            set(nodes[aid].provider_ids + nodes[mid].provider_ids)
        )
        if mid in model_accounts:
            account_node = nodes[model_accounts[mid]]
            account_node.gaps = sorted(set(account_node.gaps + gaps))

    for row in bindings[:BINDING_LIMIT]:
        if row.status == "inactive":
            continue
        provenance = (
            "configured"
            if row.binding_type == "configured"
            and not row.config_key.startswith(AUTOREGISTER_CONFIG_KEY_PREFIX)
            else "observed"
        )
        # Missing credentials or unknown status are not evidence of bypass.
        gaps = (
            ["not_via_gateway"]
            if row.status in {"direct", "external", "not_via_gateway"}
            else []
        )
        model_link(str(row.managed_agent_id), str(row.ai_model_id), provenance, gaps)

    # Older enrollments predate explicit binding rows. An inactive row still
    # suppresses fallback, and an incomplete binding window cannot prove absence.
    bound_agents = {str(row.managed_agent_id) for row in bindings}
    legacy_ids = [str(row.id) for row in agents if str(row.id) not in bound_agents]
    if (
        legacy_ids
        and ai_models
        and len(bindings) <= BINDING_LIMIT
        and "models" not in partial_sources
    ):
        from preloop.api.endpoints.account import (
            _ai_model_meta_lookup,
            _managed_agent_configured_model_alias,
            _normalize_gateway_model_alias,
        )

        enrollments = crud.crud_managed_agent_enrollment.list_latest_for_fleet_map(
            db, account_id=account_id, agent_ids=legacy_ids, limit=BINDING_LIMIT + 1
        )
        result.truncated |= len(enrollments) > BINDING_LIMIT
        priority = {"cli_managed_config": 0, "runtime_plugin_control": 1}
        latest: dict[str, Any] = {}
        for enrollment in sorted(
            enrollments[:BINDING_LIMIT],
            key=lambda row: (
                priority.get(row.enrollment_type, 2),
                -row.created_at.timestamp(),
            ),
        ):
            latest.setdefault(str(enrollment.managed_agent_id), enrollment)
        aliases: dict[str, list[Any]] = {}
        owned_aliases: dict[tuple[str, str], list[Any]] = {}
        for row in source_rows["models"]:
            model_alias = _normalize_gateway_model_alias(
                _ai_model_meta_lookup(row, "gateway", "model_alias")
            )
            if model_alias is None:
                continue
            aliases.setdefault(model_alias, []).append(row)
            owner_id = _ai_model_meta_lookup(row, "managed_agent_id")
            if isinstance(owner_id, str):
                owned_aliases.setdefault((model_alias, owner_id), []).append(row)
        for aid, enrollment in latest.items():
            alias = _managed_agent_configured_model_alias(
                {"managed_config": enrollment.managed_config}
            )
            if alias is None:
                continue
            matches = owned_aliases.get((alias, aid)) or aliases.get(alias, [])
            if len(matches) == 1:
                model_link(aid, str(matches[0].id), "configured", [])

    service = get_plugin_manager().get_service("device_fleet")
    projection: dict[str, Any] | None = None
    if service is not None:
        try:
            projection = service.read(db, account_id=user.account_id)
        except Exception:
            logger.exception("Optional device projection unavailable for fleet map")
    if projection is None:
        result.unavailable.append("devices")
    else:
        result.truncated |= projection["truncated"]
        devices = projection["items"]
        result.truncated |= len(devices) > SOURCE_LIMIT
        discovered_count = 0
        for device in devices[:SOURCE_LIMIT]:
            key = device["device_key"]
            owners = [
                f"user:{u['id']}"
                for u in device["users"]
                if u.get("id") and f"user:{u['id']}" in nodes
            ]
            count = sum(f["state"] == "open" for f in device["findings"])
            item = node(
                "device",
                key,
                device.get("hostname") or "Unknown device",
                f"{FLEET_HREF}?device={quote(key, safe='')}",
                status=device["runner_state"],
                owner_ids=owners,
                finding_count=count,
                gaps=(["open_finding"] if count else [])
                + (["unknown_device"] if not device.get("hostname") else []),
            )
            for uid in owners:
                edge(uid, item.id, "owns", "observed")
            for runner in device["runners"]:
                rid = f"runner:{runner['id']}"
                if rid in nodes:
                    nodes[rid].status = runner["state"]
                    nodes[rid].gaps = (
                        ["offline_runner"] if runner["state"] == "offline" else []
                    )
                    edge(
                        item.id,
                        rid,
                        "hosts",
                        "observed",
                        gaps=["offline_runner"] if runner["state"] == "offline" else [],
                    )
            for discovered in device["agents"]:
                managed_id = discovered.get("managed_agent_id")
                if managed_id:
                    aid = f"agent:{managed_id}"
                    if aid not in nodes:
                        continue
                    nodes[aid].gaps = [
                        gap for gap in nodes[aid].gaps if gap != "unknown_device"
                    ]
                elif permissions["view_agents"]:
                    discovered_count += 1
                    if discovered_count > SOURCE_LIMIT:
                        result.truncated = True
                        continue
                    aid = node(
                        "agent",
                        f"discovered:{discovered['id']}",
                        discovered.get("name") or discovered["kind"],
                        f"{FLEET_HREF}?device={quote(key, safe='')}",
                        detail=discovered["kind"],
                        status=discovered["status"],
                        owner_ids=owners,
                        gaps=["discovered_agent"],
                    ).id
                else:
                    continue
                nodes[aid].owner_ids = sorted(set(nodes[aid].owner_ids + owners))
                edge(
                    item.id,
                    aid,
                    "hosts",
                    "observed",
                    gaps=["open_finding"] if count else [],
                )
                item.provider_ids = sorted(
                    set(item.provider_ids + nodes[aid].provider_ids)
                )

    # Propagate facets in dependency order. Do not spread owner identity back
    # through a shared provider into other accounts using that provider.
    for kind in ("uses_model", "uses_account", "account_of_provider", "provided_by"):
        for relation in edges.values():
            if relation.kind == kind:
                source, target = nodes[relation.source], nodes[relation.target]
                target.owner_ids = sorted(set(target.owner_ids + source.owner_ids))
    for relation in edges.values():
        if relation.kind == "owns":
            nodes[relation.source].provider_ids = sorted(
                set(
                    nodes[relation.source].provider_ids
                    + nodes[relation.target].provider_ids
                )
            )

    # Round-robin types so a large agent inventory cannot evict every provider
    # and owner. Summaries count only visible, loaded resources omitted here.
    groups = [
        [n for n in sorted(nodes.values(), key=lambda n: n.id) if n.type == kind]
        for kind in (
            "user",
            "device",
            "runner",
            "agent",
            "model",
            "provider_account",
            "provider",
        )
    ]
    ordered = [n for row in zip_longest(*groups) for n in row if n is not None]
    result.nodes = ordered[:limit]
    omitted = Counter(n.type for n in ordered[limit:])
    result.clusters = [
        FleetMapCluster(type=kind, count=count)
        for kind, count in sorted(omitted.items())
    ]
    result.truncated |= bool(omitted)
    visible = {n.id for n in result.nodes}
    for item in result.nodes:
        item.owner_ids = [uid for uid in item.owner_ids if uid in visible]
        item.provider_ids = [pid for pid in item.provider_ids if pid in visible]
    visible_edges = [
        edge
        for _, edge in sorted(edges.items())
        if edge.source in visible and edge.target in visible
    ]
    result.truncated |= len(visible_edges) > EDGE_LIMIT
    result.edges = visible_edges[:EDGE_LIMIT]
    return result
