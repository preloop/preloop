"""Same-named MCP tools across servers: first-wins, shadowing, optional prefix.

Two active MCP servers in one account can expose a tool with the same name.
Tools are never renamed automatically, because agents are prompted with tool
names and policies and tool configuration are keyed by them (#1135). Instead:

* The oldest active server (by ``created_at``, then ``id``) owns a name. It is
  listed once, calls route to it and its tool configuration applies.
* The newer server's same-named tools are marked ``shadowed`` on their
  ``mcp_tool`` row. They are not listed to agents and not callable. Create,
  update, scan and delete recompute the marks, so removing or disabling the
  owner hands the name to the next oldest server.
* A server may set an explicit ``tool_prefix``. Its tools are then exposed as
  ``<prefix>_<tool>`` everywhere (listing, routing, policy, configuration).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from preloop.models.crud import crud_mcp_server, crud_mcp_tool

logger = logging.getLogger(__name__)

#: Allowed explicit tool prefix: lowercase letters, digits, underscore.
TOOL_PREFIX_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")
TOOL_PREFIX_MAX_LENGTH = 32

#: MCP tool-name constraint (spec 2025-06-18 onwards): 1 to 128 characters
#: from ``A-Z a-z 0-9 _ - .``.
MCP_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")

PREFIX_HINT = "Set a tool prefix on this server to expose both."


def validate_tool_prefix(prefix: Optional[str]) -> Optional[str]:
    """Return the normalised prefix, ``None`` to clear it.

    Args:
        prefix: Requested prefix. ``None`` or an empty string clears it.

    Returns:
        The prefix unchanged, or ``None``.

    Raises:
        ValueError: If the prefix is not lowercase ``[a-z0-9_]``, 1 to 32
            characters.
    """
    if prefix is None or prefix == "":
        return None
    if not isinstance(prefix, str) or not TOOL_PREFIX_PATTERN.match(prefix):
        raise ValueError(
            "tool_prefix must be 1 to 32 characters of lowercase letters, "
            "digits and underscores ([a-z0-9_])"
        )
    return prefix


def exposed_tool_name(prefix: Optional[str], tool_name: str) -> str:
    """Name agents see for an upstream tool: ``<prefix>_<tool>`` or the name."""
    return f"{prefix}_{tool_name}" if prefix else tool_name


def upstream_tool_name(prefix: Optional[str], exposed_name: str) -> str:
    """Upstream tool name for an exposed name (inverse of ``exposed_tool_name``)."""
    if prefix and exposed_name.startswith(f"{prefix}_"):
        return exposed_name[len(prefix) + 1 :]
    return exposed_name


def is_valid_mcp_tool_name(name: str) -> bool:
    """Whether *name* satisfies the MCP tool-name constraint."""
    return isinstance(name, str) and bool(MCP_TOOL_NAME_PATTERN.match(name))


def server_order_key(server: Any) -> Tuple[Any, str]:
    """First-wins order: ``created_at``, then ``id``."""
    return (server.created_at, str(server.id))


def first_wins(
    pairs: Iterable[Tuple[Any, Any]],
) -> List[Tuple[Any, Any, str]]:
    """Keep the first ``(server, tool)`` per exposed name, in the given order.

    Callers pass servers already in owner order (own servers by
    ``server_order_key``, then shared ones). Returns
    ``(server, tool, exposed_name)``.
    """
    seen: set[str] = set()
    kept: List[Tuple[Any, Any, str]] = []
    for server, tool in pairs:
        name = exposed_tool_name(getattr(server, "tool_prefix", None), tool.name)
        if name in seen:
            continue
        seen.add(name)
        kept.append((server, tool, name))
    return kept


def shadowed_warning(tool_name: str, server_name: str, owner_name: str) -> str:
    """The warning text for one shadowed tool (API, console and CLI)."""
    return (
        f"Tool '{tool_name}' on MCP server '{server_name}' is shadowed by MCP "
        f"server '{owner_name}', which was added earlier and exposes the same "
        f"name. Agents see and call only the tool from '{owner_name}'. "
        f"{PREFIX_HINT}"
    )


def invalid_name_warning(tool_name: str, server_name: str) -> str:
    """The warning text for a tool whose exposed name breaks MCP constraints."""
    return (
        f"Tool '{tool_name}' on MCP server '{server_name}' is not exposed: the "
        "name is not a valid MCP tool name (1 to 128 characters from "
        "A-Z, a-z, 0-9, '_', '-', '.')."
    )


@dataclass
class CollisionSet:
    """Tools of one server that changed shadowing against one owner."""

    owner_server_id: str
    owner_server_name: str
    shadowed_server_id: str
    shadowed_server_name: str
    tool_names: List[str] = field(default_factory=list)

    def as_audit_value(self) -> Dict[str, Any]:
        """Serialisable form for the ``configuration_change`` audit event."""
        return {
            "owner_server_id": self.owner_server_id,
            "owner_server_name": self.owner_server_name,
            "shadowed_server_id": self.shadowed_server_id,
            "shadowed_server_name": self.shadowed_server_name,
            "tool_names": sorted(self.tool_names),
        }


@dataclass
class ShadowingChanges:
    """What ``recompute_shadowing`` changed."""

    shadowed: List[CollisionSet] = field(default_factory=list)
    unshadowed: List[Dict[str, Any]] = field(default_factory=list)


def _owners(
    db: Session, account_id: str
) -> Tuple[List[Any], Dict[str, List[Any]], Dict[str, Any]]:
    """Own servers in first-wins order, their tools, and the owner per name."""
    servers = sorted(
        crud_mcp_server.get_all_by_account(db, account_id=str(account_id)),
        key=server_order_key,
    )
    tools_by_server: Dict[str, List[Any]] = {}
    for tool in crud_mcp_tool.get_by_servers_for_account(
        db, account_id=str(account_id), server_ids=[s.id for s in servers]
    ):
        tools_by_server.setdefault(str(tool.mcp_server_id), []).append(tool)
    owner_by_name: Dict[str, Any] = {}
    for server in servers:
        if server.status != "active":
            continue
        for tool in sorted(tools_by_server.get(str(server.id), []), key=_tool_key):
            name = exposed_tool_name(server.tool_prefix, tool.name)
            owner_by_name.setdefault(name, server)
    return servers, tools_by_server, owner_by_name


def _tool_key(tool: Any) -> str:
    return tool.name


def recompute_shadowing(db: Session, account_id: str) -> ShadowingChanges:
    """Recompute ``mcp_tool.shadowed`` for every own server of an account.

    Only active servers compete for a name. Tools of inactive servers are
    never marked, so enabling them again recomputes from scratch. Commits
    when anything changed.

    Args:
        db: Database session.
        account_id: Account whose servers are recomputed.

    Returns:
        The tools newly shadowed (grouped per owner and shadowed server) and
        the tools un-shadowed (grouped per server).
    """
    servers, tools_by_server, owner_by_name = _owners(db, account_id)
    changes = ShadowingChanges()
    sets: Dict[Tuple[str, str], CollisionSet] = {}
    unshadowed: Dict[str, Dict[str, Any]] = {}
    for server in servers:
        for tool in tools_by_server.get(str(server.id), []):
            name = exposed_tool_name(server.tool_prefix, tool.name)
            owner = owner_by_name.get(name)
            should = (
                server.status == "active"
                and owner is not None
                and owner.id != server.id
            )
            if bool(tool.shadowed) == should:
                continue
            tool.shadowed = should
            if should:
                key = (str(owner.id), str(server.id))
                if key not in sets:
                    sets[key] = CollisionSet(
                        owner_server_id=str(owner.id),
                        owner_server_name=owner.name,
                        shadowed_server_id=str(server.id),
                        shadowed_server_name=server.name,
                    )
                sets[key].tool_names.append(name)
            else:
                entry = unshadowed.setdefault(
                    str(server.id),
                    {
                        "server_id": str(server.id),
                        "server_name": server.name,
                        "tool_names": [],
                    },
                )
                entry["tool_names"].append(name)
    changes.shadowed = list(sets.values())
    for entry in unshadowed.values():
        entry["tool_names"].sort()
    changes.unshadowed = list(unshadowed.values())
    if changes.shadowed or changes.unshadowed:
        db.commit()
        logger.info(
            "MCP tool shadowing recomputed for account %s: %d set(s) shadowed, "
            "%d server(s) un-shadowed",
            account_id,
            len(changes.shadowed),
            len(changes.unshadowed),
        )
    return changes


def server_warnings(db: Session, server: Any) -> List[str]:
    """Warnings for one server: its shadowed tools and invalid exposed names."""
    return server_warnings_map(db, str(server.account_id)).get(str(server.id), [])


def server_warnings_map(db: Session, account_id: str) -> Dict[str, List[str]]:
    """Warnings for every own server of an account, keyed by server id.

    Two queries for the whole account, so a server list does not re-read
    every tool once per server.
    """
    servers, tools_by_server, owner_by_name = _owners(db, account_id)
    result: Dict[str, List[str]] = {}
    for server in servers:
        warnings: List[str] = []
        for tool in sorted(tools_by_server.get(str(server.id), []), key=_tool_key):
            warnings.extend(_tool_warnings(server, tool, owner_by_name))
        result[str(server.id)] = warnings
    return result


def tool_warnings_by_id(db: Session, server: Any) -> Dict[str, List[str]]:
    """Per-tool warnings for one server, keyed by ``mcp_tool.id``."""
    return {
        tool_id: warnings
        for tool_id, (server_id, warnings) in _account_tool_warnings(
            db, str(server.account_id)
        ).items()
        if server_id == str(server.id)
    }


def warnings_from_loaded(
    servers: List[Any], tools_by_server: Dict[str, List[Any]]
) -> Dict[str, List[str]]:
    """Per-tool warnings from servers and tools the caller already loaded.

    Avoids a second account-wide tool read when ``/tools`` has just batched
    the same rows.
    """
    owner_by_name: Dict[str, Any] = {}
    for server in sorted(servers, key=server_order_key):
        if getattr(server, "status", None) != "active":
            continue
        for tool in sorted(tools_by_server.get(str(server.id), []), key=_tool_key):
            owner_by_name.setdefault(
                exposed_tool_name(server.tool_prefix, tool.name), server
            )
    result: Dict[str, List[str]] = {}
    for server in servers:
        for tool in tools_by_server.get(str(server.id), []):
            result[str(tool.id)] = _tool_warnings(server, tool, owner_by_name)
    return result


def _account_tool_warnings(
    db: Session, account_id: str
) -> Dict[str, Tuple[str, List[str]]]:
    servers, tools_by_server, owner_by_name = _owners(db, account_id)
    result: Dict[str, Tuple[str, List[str]]] = {}
    for server in servers:
        for tool in tools_by_server.get(str(server.id), []):
            result[str(tool.id)] = (
                str(server.id),
                _tool_warnings(server, tool, owner_by_name),
            )
    return result


def _tool_warnings(server: Any, tool: Any, owner_by_name: Dict[str, Any]) -> List[str]:
    name = exposed_tool_name(server.tool_prefix, tool.name)
    out: List[str] = []
    if not is_valid_mcp_tool_name(name):
        out.append(invalid_name_warning(name, server.name))
    if tool.shadowed:
        owner = owner_by_name.get(name)
        owner_name = owner.name if owner is not None else "another server"
        out.append(shadowed_warning(name, server.name, owner_name))
    return out


def log_shadowing_audit(db: Session, user: Any, changes: ShadowingChanges) -> None:
    """One ``configuration_change`` audit event per collision set.

    ``config_type`` is ``mcp_tool_collision``; ``action`` is ``shadowed``
    (with owner and shadowed server ids and the tool names) or
    ``unshadowed`` (server id and tool names).
    """
    if user is None:
        return
    from preloop.utils.audit import log_config_change

    for collision in changes.shadowed:
        log_config_change(
            db,
            user=user,
            config_type="mcp_tool_collision",
            action="shadowed",
            new_value=collision.as_audit_value(),
        )
    for entry in changes.unshadowed:
        log_config_change(
            db,
            user=user,
            config_type="mcp_tool_collision",
            action="unshadowed",
            new_value=entry,
        )


def recompute_and_audit(
    db: Session, account_id: str, user: Any = None
) -> ShadowingChanges:
    """``recompute_shadowing`` plus the audit events. Never raises."""
    try:
        changes = recompute_shadowing(db, account_id)
    except Exception:
        logger.warning("Could not recompute MCP tool shadowing", exc_info=True)
        db.rollback()
        return ShadowingChanges()
    try:
        log_shadowing_audit(db, user, changes)
    except Exception:
        logger.warning("Could not audit MCP tool shadowing", exc_info=True)
    return changes
