"""Scope the Fleet Map reads to selected people (the ``users=`` parameter).

The scope is the selected users, the devices they own, the agents and runners
they own or that run on those devices, the models and provider accounts those
agents use and the providers of those. Flows are never in a person's scope:
a flow run stores no triggering user. Work items are in scope when an in-scope
agent acted on them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.fleet_map import FleetMapEdge, FleetMapNode

#: Most people one request can be scoped to.
MAX_SCOPE_USERS = 50


@dataclass
class FleetScope:
    """Node ids in a people scope and the user ids actually applied."""

    users: list[str]
    node_ids: set[str] = field(default_factory=set)
    #: Managed agent rows in scope (for principal and ledger filters).
    agents: list[Any] = field(default_factory=list)

    def has(self, node_id: str) -> bool:
        """Whether a map node id is in the scope."""
        return node_id in self.node_ids


def parse_users(raw: str | None) -> list[str] | None:
    """Parse ``users=<uuid>,<uuid>``; None or blank means no scope.

    Args:
        raw: Comma separated user ids.

    Returns:
        Sorted distinct canonical ids, or None.

    Raises:
        HTTPException: 422 for a malformed id or more than ``MAX_SCOPE_USERS``.
    """
    if raw is None or not raw.strip():
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) > MAX_SCOPE_USERS:
        raise HTTPException(422, f"At most {MAX_SCOPE_USERS} users can be selected")
    ids: set[str] = set()
    for part in parts:
        try:
            ids.add(str(UUID(part)))
        except ValueError as exc:
            raise HTTPException(422, f"Invalid user id: {part[:64]}") from exc
    return sorted(ids)


def scope_nodes(
    nodes: Mapping[str, FleetMapNode],
    edges: Iterable[FleetMapEdge],
    user_ids: Iterable[str],
) -> tuple[set[str], list[str]]:
    """Expand selected users to the node ids of their part of the graph.

    Args:
        nodes: The account's full graph nodes, keyed by id.
        edges: The graph's edges.
        user_ids: Selected user ids (no prefix).

    Returns:
        The node ids in scope, and the user node ids that exist in the graph.
    """
    relations = list(edges)
    users = sorted(node_id for uid in user_ids if (node_id := f"user:{uid}") in nodes)
    scope: set[str] = set(users)

    def targets(kinds: set[str], sources: set[str]) -> set[str]:
        return {
            relation.target
            for relation in relations
            if relation.kind in kinds and relation.source in sources
        }

    owned = targets({"owns"}, scope)
    devices = {node for node in owned if node.startswith("device:")}
    hosted = targets({"hosts"}, devices)
    holders = owned | hosted
    agents = {node for node in holders if node.startswith("agent:")}
    runners = {node for node in holders if node.startswith("runner:")}
    models_used = targets({"uses_model"}, agents)
    accounts = targets({"uses_account"}, agents)
    providers = targets({"account_of_provider"}, accounts) | targets(
        {"provided_by"}, models_used
    )
    scope |= devices | agents | runners | models_used | accounts | providers
    return scope, users


def resolve_scope(
    db: Session,
    *,
    user: models.User,
    user_ids: list[str],
    now: datetime | None = None,
) -> FleetScope:
    """Build the account's graph once and expand the selected users.

    Ownership does not depend on the activity window, so the scope comes from
    the unwindowed graph; each endpoint intersects it with its own window.

    Args:
        db: Request database session.
        user: Authenticated caller (the graph's visibility applies).
        user_ids: Selected user ids.
        now: Clock override for tests.

    Returns:
        The scope with the in-scope managed agent rows.
    """
    from preloop.models.crud.fleet_map import list_resources
    from preloop.services.fleet_map import build_fleet_map

    graph = build_fleet_map(db, user=user, limit=1000, now=now, user_ids=user_ids)
    node_ids = {node.id for node in graph.nodes}
    agent_ids = {
        node.removeprefix("agent:")
        for node in node_ids
        if node.startswith("agent:") and not node.startswith("agent:discovered:")
    }
    agents = (
        [
            row
            for row in list_resources(
                db, source="agents", account_id=str(user.account_id), limit=5001
            )
            if str(row.id) in agent_ids
        ]
        if agent_ids
        else []
    )
    return FleetScope(users=graph.users or [], node_ids=node_ids, agents=agents)
