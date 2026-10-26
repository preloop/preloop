"""Bounded fleet graph over authorized, tenant-owned source projections."""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.auth.key_scopes import is_mcp_only_api_key, is_single_purpose_api_key
from preloop.models import crud, models
from preloop.models.crud.fleet_map import (
    Source,
    list_decommissioned_agents,
    list_resources,
)
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
from preloop.services.agent_device_resolution import (
    agent_kind_for_harness,
    resolve_hostname_devices,
)
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
# An onboarded agent not seen for this long is shown as inactive.
ACTIVE_WINDOW = timedelta(days=14)
_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_UPPER_WORDS = {"gpt", "glm", "ai", "llm", "r1", "v3", "o1", "o3", "o4"}


def canonical_model(identifier: str) -> tuple[str, str]:
    """Reduce a provider model identifier to a shared reference and label.

    Per-account model rows of the same model (for example an auto-registered
    ``anthropic/claude-opus-5-5`` per agent) collapse into one reference.

    Args:
        identifier: Model identifier as configured on the model row.

    Returns:
        Stable lowercase key and a human label, such as ``Claude Opus 5.5``.
    """
    key = _DATE_SUFFIX.sub("", identifier.strip().rpartition("/")[2].lower())
    key = key.removesuffix(":latest") or identifier.strip().lower()
    words: list[str] = []
    for part in re.split(r"[-_\s]+", key):
        if not part:
            continue
        if part.isdigit() and words and re.fullmatch(r"[\d.]+", words[-1]):
            words[-1] = f"{words[-1]}.{part}"
        elif part in _UPPER_WORDS:
            words.append(part.upper())
        elif part[0].isdigit():
            words.append(part)
        else:
            words.append(part[0].upper() + part[1:])
    return key, " ".join(words) or identifier


def _aware(value: Any) -> datetime | None:
    """Return a timezone-aware timestamp, or None for missing values."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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


def _enrollment_hostname(
    device_agents: list[dict[str, Any]], agents_by_id: dict[str, Any]
) -> str | None:
    """Label a runner-less device by the hostname its linked agents enrolled from.

    The device itself is still keyed by its exact salted fingerprint; the
    hostname only names it, it never merges devices.
    """
    names = sorted(
        {
            name.strip()
            for entry in device_agents
            if (row := agents_by_id.get(str(entry.get("managed_agent_id")))) is not None
            and isinstance(name := getattr(row, "enrollment_hostname", None), str)
            and name.strip()
        }
    )
    return names[0] if len(names) == 1 else None


# Dependencies flow source -> target along these relations.
_DOWNSTREAM = ("uses_model", "uses_account", "account_of_provider", "provided_by")
# Holders flow target -> source: a device hosting, a person owning.
_UPSTREAM = ("hosts", "owns")


def _seen_at(value: Any) -> datetime | None:
    """Read a projection timestamp that may be a datetime or an ISO string."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return _aware(value)


def active_node_ids(
    nodes: dict[str, FleetMapNode],
    edges: dict[str, FleetMapEdge],
    since: datetime,
    evidence: dict[str, datetime] | None = None,
) -> set[str]:
    """Return the nodes with their own activity since ``since``.

    Agents, devices and runners carry their own last-seen evidence; every
    node may also have direct ``evidence`` (gateway requests, sessions).
    Models, provider accounts and providers need their own gateway evidence:
    being configured on, or bound to, an active agent is not activity. People
    and devices are active when something they hold is.

    Args:
        nodes: Graph nodes keyed by id.
        edges: Graph edges keyed by id.
        since: Oldest activity that still counts.
        evidence: Latest direct activity per node id inside the window.

    Returns:
        Ids of every node to keep.
    """
    active = {
        identifier
        for identifier, item in nodes.items()
        if item.type in {"agent", "device", "runner"}
        and item.last_seen_at is not None
        and item.last_seen_at >= since
    }
    active.update(
        identifier
        for identifier, at in (evidence or {}).items()
        if identifier in nodes and at >= since
    )
    for kind in _UPSTREAM:
        for relation in edges.values():
            if relation.kind == kind and relation.target in active:
                active.add(relation.source)
    return active


#: Largest stored avatar the map passes through (manual uploads are data URIs).
AVATAR_LIMIT = 200_000
#: Grouped gateway/session reads per map build.
ACTIVITY_LIMIT = 5000
#: Observed-usage lookback when no window is selected.
ANY_WINDOW = timedelta(days=90)


def _avatar(value: Any) -> str | None:
    """Pass through the stored profile image: an http(s) URL or image data URI."""
    if not isinstance(value, str) or not value or len(value) > AVATAR_LIMIT:
        return None
    if value.startswith(("https://", "http://", "data:image/")):
        return value
    return None


def _naive_utc(value: Any) -> datetime | None:
    """Aware UTC for a naive-UTC column value."""
    return _aware(value)


def build_fleet_map(
    db: Session,
    *,
    user: models.User,
    limit: int = 500,
    active_within_minutes: int | None = None,
    include_idle: bool = False,
    now: datetime | None = None,
    user_ids: list[str] | None = None,
) -> FleetMapResponse:
    """Load each source once, filter before linking, and cap the final graph.

    Args:
        db: Request database session.
        user: Authenticated user in the selected account.
        limit: Maximum visible resource nodes, between 50 and 1000.
        active_within_minutes: Keep only resources with activity in this many
            minutes; None keeps every resource.
        include_idle: With a window, also keep configured resources without
            activity, flagged ``idle``.
        now: Optional UTC clock override for deterministic tests.
        user_ids: Scope the graph to these people, their devices, agents,
            runners and the models, accounts and providers their agents use
            (see ``fleet_scope``); None keeps everyone.

    Returns:
        Allowlisted graph with explicit source and output truncation.

    Every member may call this: each source is read only with its own
    permission (devices need ``view_device_fleet``) and filtered by the
    resource authorizer, and a denied source is listed in ``unavailable``.

    Raises:
        HTTPException: Report-only credentials, or a limit out of range.
    """
    _reject_report_key(user)
    if not 50 <= limit <= 1000:
        raise HTTPException(422, "Fleet map limit must be between 50 and 1000")
    account = crud.crud_account.get(db, id=user.account_id)
    owner = account is not None and str(account.primary_user_id) == str(user.id)
    result = FleetMapResponse(limit=limit, active_within_minutes=active_within_minutes)
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
            "view_device_fleet",
        )
    }

    source_rows: dict[str, list[Any]] = {}
    partial_sources: set[str] = set()

    def read(
        name: Source, permission: str, resource_type: str | None = None
    ) -> list[Any]:
        if not permissions[permission]:
            # Say which sources this caller cannot see, rather than a 403.
            result.unavailable.append(name)
            result.restricted.append(name)
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
        if kind == "uses_model" and kwargs.get("account_id"):
            # Two accounts reaching one canonical model are two relationships.
            identifier = f"{identifier}:{kwargs['account_id']}"
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
            f"/console/settings/users/{quote(str(row.id), safe='')}",
            avatar_url=_avatar(getattr(row, "avatar_url", None)),
        )
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if permissions["view_agents"]:
        # Count only what the caller could see, as the agent source does.
        retired = list_decommissioned_agents(
            db, account_id=account_id, limit=SOURCE_LIMIT + 1
        )
        # A saturated count is a lower bound; say the map is partial.
        result.truncated |= len(retired) > SOURCE_LIMIT
        result.decommissioned_agents = len(
            filter_viewable(db, user, VISIBLE_MANAGED_AGENT, retired[:SOURCE_LIMIT])
        )
    for row in agents:
        owners = [f"user:{row.owner_user_id}"] if row.owner_user_id else []
        seen = _aware(getattr(row, "last_seen_at", None))
        recent = seen is None or now - seen <= ACTIVE_WINDOW
        item = node(
            "agent",
            row.id,
            row.display_name or row.agent_kind,
            f"/console/agents/{quote(str(row.id), safe='')}",
            detail=row.agent_kind,
            status=row.lifecycle_state,
            owner_ids=[uid for uid in owners if uid in nodes],
            gaps=["unknown_device"],
            agent_state=(
                "onboarded"
                if row.lifecycle_state == "active" and recent
                else "inactive"
            ),
            last_seen_at=seen,
        )
        if permissions["manage_agents"]:
            item.actions.append(
                FleetMapAction(label="Manage agent access", href=item.href)
            )
        for uid in item.owner_ids:
            edge(uid, item.id, "owns")
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
            last_seen_at=_aware(getattr(row, "last_heartbeat", None)),
        )

        for uid in item.owner_ids:
            edge(uid, item.id, "owns")

    # Model rows are per-account (often per-agent) instances of one model.
    # The map shows one node per canonical model and keeps the account each
    # agent reaches it through on the agent's edge.
    model_accounts: dict[str, str] = {}
    model_nodes: dict[str, str] = {}
    model_rows: dict[str, list[Any]] = {}
    for row in ai_models:
        provider = node(
            "provider",
            row.provider_name,
            row.provider_name,
            f"{MODELS_HREF}?provider={quote(str(row.provider_name), safe='')}",
        )
        provider.provider_ids = [provider.id]
        model_href = f"{MODELS_HREF}/{quote(str(row.id), safe='')}"
        key, label = canonical_model(
            getattr(row, "model_identifier", None) or row.name or str(row.id)
        )
        item = node("model", key, label, model_href, detail=key)
        model_rows.setdefault(item.id, []).append(row)
        if provider.id not in item.provider_ids:
            item.provider_ids = sorted(item.provider_ids + [provider.id])
        model_nodes[str(row.id)] = item.id
        # One canonical model can be reached through several providers, so a
        # provider's impact flows through its accounts. Only a model row with
        # no account links the model to its provider directly.
        if row.account_id is None:
            # Hosted routing and operator credentials are not tenant resources.
            edge(item.id, provider.id, "provided_by")
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
            edge(item.id, provider.id, "provided_by")
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
            parent_id=provider.id,
        )
        if provider.id not in account_node.provider_ids:
            account_node.provider_ids.append(provider.id)
        action = FleetMapAction(label="Manage credentials", href=model_href)
        if permissions["edit_ai_models"] and not account_node.actions:
            account_node.actions.append(action)
        model_accounts[str(row.id)] = account_node.id
        edge(account_node.id, provider.id, "account_of_provider")
    for mid, rows in model_rows.items():
        if len(rows) > 1:
            nodes[mid].href = MODELS_HREF
            nodes[mid].detail = f"{nodes[mid].detail} · {len(rows)} configurations"

    def model_link(
        agent_id: str, model_id: str, provenance: str, gaps: list[str]
    ) -> None:
        aid, mid = f"agent:{agent_id}", model_nodes.get(model_id, "")
        if aid not in nodes or mid not in nodes:
            return
        via = model_accounts.get(model_id)
        edge(aid, mid, "uses_model", provenance, gaps=gaps, account_id=via)
        nodes[aid].gaps = sorted(set(nodes[aid].gaps + gaps))
        # A canonical model lists every provider exposing it; the account is
        # the provider this agent actually reaches.
        if via:
            edge(aid, via, "uses_account", provenance, gaps=gaps)
            account_node = nodes[via]
            account_node.gaps = sorted(set(account_node.gaps + gaps))
            nodes[aid].provider_ids = sorted(
                set(nodes[aid].provider_ids + account_node.provider_ids)
            )
        else:
            nodes[aid].provider_ids = sorted(
                set(nodes[aid].provider_ids + nodes[mid].provider_ids)
            )

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

    # Devices are the device fleet's inventory: only its viewers see them.
    if not permissions["view_device_fleet"]:
        result.restricted.append("devices")
    service = (
        get_plugin_manager().get_service("device_fleet")
        if permissions["view_device_fleet"]
        else None
    )
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
        discovered_seen: set[tuple[str, str]] = set()
        agents_by_id = {str(row.id): row for row in agents}
        # Agents with no fingerprint link are placed by enrollment hostname,
        # only on an unambiguous, owner-consistent match.
        by_hostname: dict[str, list[str]] = {}
        # A partial agent or device window cannot prove a match is unique.
        complete = (
            "agents" not in partial_sources
            and not projection["truncated"]
            and len(devices) <= SOURCE_LIMIT
        )
        for agent_id, device_key in (
            resolve_hostname_devices(devices, agents) if complete else {}
        ).items():
            by_hostname.setdefault(device_key, []).append(agent_id)
        for device in devices[:SOURCE_LIMIT]:
            key = device["device_key"]
            owners = [
                f"user:{u['id']}"
                for u in device["users"]
                if u.get("id") and f"user:{u['id']}" in nodes
            ]
            count = sum(f["state"] == "open" for f in device["findings"])
            hostname = device.get("hostname") or _enrollment_hostname(
                device["agents"], agents_by_id
            )
            item = node(
                "device",
                key,
                hostname or "Unknown device",
                f"{FLEET_HREF}?device={quote(key, safe='')}",
                status=device["runner_state"],
                owner_ids=owners,
                finding_count=count,
                gaps=(["open_finding"] if count else [])
                + (["unknown_device"] if not hostname else []),
                last_seen_at=_seen_at(device.get("last_seen_at")),
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
                linked = nodes.get(f"agent:{discovered.get('managed_agent_id')}")
                if linked is not None:
                    discovered_seen.add((key, str(linked.detail)))
            for agent_id in sorted(by_hostname.get(key, [])):
                aid = f"agent:{agent_id}"
                if aid not in nodes:
                    continue
                # The same harness reported by inventory or discovery is
                # this managed agent, not a second, discovered one.
                discovered_seen.add((key, str(nodes[aid].detail)))
                nodes[aid].gaps = [
                    gap for gap in nodes[aid].gaps if gap != "unknown_device"
                ]
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
                    # Inventory and discovery report the same harness; a
                    # dismissed candidate is a decision, not a gap.
                    if discovered.get("status") in {"ignored", "dismissed"}:
                        continue
                    kind = agent_kind_for_harness(discovered["kind"])
                    if (key, kind) in discovered_seen:
                        continue
                    discovered_seen.add((key, kind))
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
                        agent_state="discovered",
                        last_seen_at=_aware(discovered.get("last_seen_at")),
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

    since = now - (
        timedelta(minutes=active_within_minutes)
        if active_within_minutes is not None
        else ANY_WINDOW
    )
    evidence = _activity_evidence(
        db,
        account_id=account_id,
        since=since,
        until=now,
        agents=agents,
        model_rows=source_rows.get("models", []),
        model_nodes=model_nodes,
        model_accounts=model_accounts,
        nodes=nodes,
        result=result,
        link=model_link,
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

    keep = active_node_ids(nodes, edges, since, evidence)
    for key, item in nodes.items():
        stamps: list[datetime] = [
            at
            for at in (evidence.get(key), item.last_seen_at)
            if at is not None and at >= since
        ]
        item.last_active_at = max(stamps) if stamps else None
        item.idle = key not in keep
    # A person kept for what they hold is as recent as the latest of it.
    for relation in edges.values():
        if relation.kind != "owns":
            continue
        holder, held = nodes[relation.source], nodes[relation.target]
        if held.last_active_at and (
            holder.last_active_at is None or held.last_active_at > holder.last_active_at
        ):
            holder.last_active_at = held.last_active_at
    if user_ids is not None:
        from preloop.services.fleet_scope import scope_nodes

        # Ownership is not windowed: scope on the full graph, then window it.
        scope, result.users = scope_nodes(nodes, edges.values(), user_ids)
        nodes = {key: item for key, item in nodes.items() if key in scope}
        edges = {
            key: relation
            for key, relation in edges.items()
            if relation.source in scope and relation.target in scope
        }
    if active_within_minutes is not None and not include_idle:
        nodes = {key: item for key, item in nodes.items() if key in keep}
        edges = {
            key: relation
            for key, relation in edges.items()
            if relation.source in keep and relation.target in keep
        }

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
    _contain(result.nodes, visible, model_rows, model_nodes, model_accounts)
    visible_edges = [
        edge
        for _, edge in sorted(edges.items())
        if edge.source in visible and edge.target in visible
    ]
    result.truncated |= len(visible_edges) > EDGE_LIMIT
    result.edges = visible_edges[:EDGE_LIMIT]
    return result


def _contain(
    returned: list[FleetMapNode],
    visible: set[str],
    model_rows: dict[str, list[Any]],
    model_nodes: dict[str, str],
    model_accounts: dict[str, str],
) -> None:
    """Fill provider > account > model containment ids on returned nodes.

    A canonical model merges configurations, so it can sit under several
    accounts and even several providers; ``provider_id`` names the provider
    of most of its configurations (ties by id), ``provider_ids`` keeps all.
    Only returned node ids are listed.

    Args:
        returned: Nodes in the response, updated in place.
        visible: Ids of the returned nodes.
        model_rows: Model configuration rows per model node id.
        model_nodes: Model node id per configuration id.
        model_accounts: Provider account node id per configuration id.
    """
    accounts_of: dict[str, set[str]] = {}
    for config_id, account in model_accounts.items():
        model = model_nodes.get(config_id)
        if model and model in visible and account in visible:
            accounts_of.setdefault(model, set()).add(account)
    by_id = {item.id: item for item in returned}
    for item in returned:
        if item.type == "provider_account":
            item.provider_id = item.parent_id if item.parent_id in visible else None
        elif item.type == "model":
            votes = Counter(
                f"provider:{row.provider_name}" for row in model_rows.get(item.id, [])
            )
            ranked = sorted(
                (pid for pid in votes if pid in visible),
                key=lambda pid: (-votes[pid], pid),
            )
            item.provider_id = ranked[0] if ranked else None
            item.provider_account_ids = sorted(accounts_of.get(item.id, set()))
    for item in returned:
        if item.type == "model":
            if item.provider_id in by_id:
                by_id[item.provider_id].model_ids.append(item.id)
            for account in item.provider_account_ids:
                by_id[account].model_ids.append(item.id)
        elif item.type == "provider_account" and item.provider_id in by_id:
            by_id[item.provider_id].account_ids.append(item.id)
    for item in returned:
        item.model_ids = sorted(set(item.model_ids))
        item.account_ids = sorted(set(item.account_ids))


def _activity_evidence(
    db: Session,
    *,
    account_id: str,
    since: datetime,
    until: datetime,
    agents: list[Any],
    model_rows: list[Any],
    model_nodes: dict[str, str],
    model_accounts: dict[str, str],
    nodes: dict[str, FleetMapNode],
    result: FleetMapResponse,
    link: Any,
) -> dict[str, datetime]:
    """Direct activity per node in the window, and observed model edges.

    Two grouped reads: gateway ledger rows by model, principal, flow and user,
    and runtime sessions by principal. A ledger row marks its model, the
    provider account and the provider active, and draws an observed
    ``uses_model`` edge from the agent whose runtime principal made it.
    """
    from preloop.models.crud import fleet_activity

    evidence: dict[str, datetime] = {}

    def mark(node_id: str | None, at: Any) -> None:
        stamp = _aware(at)
        if node_id and node_id in nodes and stamp is not None:
            evidence[node_id] = max(evidence.get(node_id, stamp), stamp)

    from preloop.services.fleet_principals import agent_index, resolve_principal

    principal_index = agent_index(agents, lambda row: f"agent:{row.id}")
    agent_by_id = {str(row.id): f"agent:{row.id}" for row in agents}

    def agent_of(principal_type: Any, principal_id: Any) -> str | None:
        return resolve_principal(
            principal_index, principal_type, principal_id, by_id=agent_by_id
        )

    providers = {str(row.id): f"provider:{row.provider_name}" for row in model_rows}
    rows = fleet_activity.gateway_edges(
        db, account_id=account_id, since=since, until=until, limit=ACTIVITY_LIMIT + 1
    )
    result.truncated |= len(rows) > ACTIVITY_LIMIT
    for row in rows[:ACTIVITY_LIMIT]:
        model_id = str(row.ai_model_id) if row.ai_model_id else ""
        mark(model_nodes.get(model_id), row.last_at)
        mark(model_accounts.get(model_id), row.last_at)
        mark(providers.get(model_id), row.last_at)
        agent_id = agent_of(row.principal_type, row.principal_id)
        if agent_id and not row.flow_id:
            mark(agent_id, row.last_at)
            if model_id:
                link(agent_id.removeprefix("agent:"), model_id, "observed", [])
        elif row.user_id and not row.principal_id and not row.flow_id:
            mark(f"user:{row.user_id}", row.last_at)
    sessions = fleet_activity.principal_sessions(
        db, account_id=account_id, since=since, until=until, limit=ACTIVITY_LIMIT + 1
    )
    result.truncated |= len(sessions) > ACTIVITY_LIMIT
    for row in sessions[:ACTIVITY_LIMIT]:
        mark(agent_of(row.principal_type, row.principal_id), row.last_at)
    return evidence
