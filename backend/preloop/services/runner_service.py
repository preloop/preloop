"""Lease and heartbeat helpers for self-hosted flow runners."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account
from preloop.models.crud.flow_runner import ONLINE_HEARTBEAT_TTL, crud_flow_runner
from preloop.models.crud.user import crud_user
from preloop.models.models.flow import Flow
from preloop.models.models.flow_runner import FlowRunner
from preloop.services.harness_inventory import redact_harness_inventory
from preloop.services.runner_presence import (
    PRESENCE_ASLEEP,
    PRESENCE_OFFLINE,
    derive_presence,
    derive_state,
)
from preloop.services.host_exec import (
    BILLING_MODES,
    BILLING_MODE_UNKNOWN,
    HOST_EXEC_AGENT_TYPE,
    HOST_EXEC_WORKSPACE_KEY,
    ROUTING_HARNESS_DISABLED,
    ROUTING_HARNESS_SIGNED_OUT,
    ROUTING_MODEL_NOT_AVAILABLE,
    ROUTING_NO_RUNNER_WITH_HARNESS,
    ROUTING_OWNER_HAS_NO_RUNNER,
    ROUTING_RUNNER_ASLEEP,
    ROUTING_RUNNER_OFFLINE,
    host_exec_harness,
    host_exec_profile_name,
    runner_has_host_exec_profile,
    runner_offers_authorized_directory,
)
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_RUNNERS,
    build_account_event,
    emit_account_event,
)

logger = logging.getLogger(__name__)

RUNNER_OVERRIDE_KEY = "_runner"
DEFAULT_QUEUE_TIMEOUT = timedelta(minutes=15)
SERVER_RUNNER_POOL = "server"
AUTO_RUNNER_POOL = "auto"
HOSTED_RUNNER_NAME = "Preloop hosted"
PRIVATE_RUNNER_FALLBACK_NAME = "Private runner"

#: Every runtime reference that names a private runner starts with this.
RUNNER_REFERENCE_PREFIX = "runner:"

#: ``runner:queued:{pool}:{execution_id}`` has no runner yet.
QUEUED_RUNNER_REFERENCE_PREFIX = "runner:queued:"


def hash_runner_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_runner_token() -> str:
    return secrets.token_urlsafe(32)


def _explicit_pool(value: Any) -> Optional[str]:
    """Return a stripped pool string, or None when unset."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _is_server_pool(pool: Optional[str]) -> bool:
    return pool is not None and pool.lower() == SERVER_RUNNER_POOL


def _is_auto_pool(pool: Optional[str]) -> bool:
    return pool is not None and pool.lower() == AUTO_RUNNER_POOL


def _account_default_runner_pool(flow: Flow, db: Optional[Session]) -> Optional[str]:
    """Read account.default_runner_pool from the flow or the database."""
    account = getattr(flow, "account", None)
    if account is not None:
        value = getattr(account, "default_runner_pool", None)
        return value if isinstance(value, str) or value is None else None
    if db is None:
        return None
    account_id = getattr(flow, "account_id", None)
    if account_id is None:
        return None
    row = crud_account.get(db, id=account_id)
    if row is None:
        return None
    value = getattr(row, "default_runner_pool", None)
    return value if isinstance(value, str) or value is None else None


def _has_online_private_runner(
    db: Session, account_id: Any, *, agent_type: Any = None, agent_config: Any = None
) -> bool:
    """True when the account has at least one private runner with a free slot.

    ``find_matching(..., online_only=True)`` includes runners that are full.
    Lease only claims a runner with a free slot, so a fully loaded fleet must
    not count as available capacity: otherwise a new unpinned flow queues for
    15 minutes and fails instead of using hosted compute.
    """
    if account_id is None:
        return False
    try:
        if not isinstance(account_id, UUID):
            UUID(str(account_id))
    except (TypeError, ValueError):
        return False
    matches = crud_flow_runner.find_matching(
        db, account_id=account_id, pool=AUTO_RUNNER_POOL, online_only=True
    )
    from preloop.services.host_exec import host_exec_effective_profile

    profile = host_exec_effective_profile(agent_type, agent_config)
    return any(
        (
            runner_has_host_exec_profile(row, profile, agent_type=agent_type)
            if profile
            else (
                getattr(row, "accept_untagged", False) is True
                and (getattr(row, "capabilities", None) or {}).get("docker_available")
                is True
            )
        )
        and getattr(row, "flow_execution_consent", None) != "never"
        and (
            getattr(row, "flow_execution_consent", None) == "always"
            or getattr(row, "registered_by_user_id", None) is not None
        )
        and getattr(row, "status", None) in ("online", "busy")
        and int(getattr(row, "free_slots", 0)) > 0
        for row in (matches or [])
    )


def runner_id_from_session_reference(ref: Optional[str]) -> Optional[UUID]:
    """Parse ``runner:{runner_id}:{execution_id}`` assigned-lease references.

    Queued forms (``runner:queued:{pool}:{execution_id}``) have no runner yet
    and return None. Non-strings are ignored so mocked endpoint tests stay
    hosted rather than querying the database.
    """
    if not isinstance(ref, str) or not ref.startswith(RUNNER_REFERENCE_PREFIX):
        return None
    parts = ref.split(":")
    if len(parts) >= 3 and parts[1] != "queued":
        try:
            return UUID(parts[1])
        except ValueError:
            return None
    return None


def pool_from_session_reference(ref: Optional[str]) -> Optional[str]:
    """Pool string from ``runner:queued:{pool}:{execution_id}``."""
    if not isinstance(ref, str) or not ref.startswith(RUNNER_REFERENCE_PREFIX):
        return None
    parts = ref.split(":")
    if len(parts) >= 4 and parts[1] == "queued" and parts[2].strip():
        return parts[2].strip()
    return None


def is_runner_assigned_reference(ref: Optional[str]) -> bool:
    """True when a private runner already holds this execution.

    Assigned only: a queued reference is still waiting for a runner and
    holds hosted-side state (a monitor, a queue timeout), so it is not a
    private-runner assignment.
    """
    return runner_id_from_session_reference(ref) is not None


def runner_assigned_execution_clause(
    *,
    reference_column: Any,
    runner_id_column: Any,
) -> Any:
    """SQL form of :func:`is_runner_assigned_reference` for count queries.

    Kept next to the string parser so the reference shape is written down
    once. Two columns say the same thing from different ends: the runtime
    reference is set by the executor, ``flow_execution.runner_id`` by the
    lease, and a row with either is running on the account's own compute.

    Args:
        reference_column: ``FlowExecution.agent_session_reference`` column.
        runner_id_column: ``FlowExecution.runner_id`` column.

    Returns:
        A SQLAlchemy boolean clause, true for runner-assigned executions.
    """
    from sqlalchemy import and_, or_

    return or_(
        runner_id_column.isnot(None),
        and_(
            # An unset reference must compare FALSE rather than NULL, or the
            # negation of this clause would drop every hosted execution.
            reference_column.isnot(None),
            reference_column.like(f"{RUNNER_REFERENCE_PREFIX}%"),
            ~reference_column.like(f"{QUEUED_RUNNER_REFERENCE_PREFIX}%"),
        ),
    )


def derive_execution_runner(
    *,
    runner_id: Optional[UUID] = None,
    agent_session_reference: Optional[str] = None,
    runner_name: Optional[str] = None,
    pool: Optional[str] = None,
) -> Dict[str, Any]:
    """Where an execution ran, derived from the row rather than a new column.

    Private when ``runner_id`` is set or ``agent_session_reference`` uses a
    ``runner:...`` form (assigned or queued). A non-runner runtime reference
    identifies the built-in hosted executor. An execution
    without an assignment is unknown, including newly created retries.
    """
    resolved_id = runner_id or runner_id_from_session_reference(agent_session_reference)
    resolved_pool = pool or pool_from_session_reference(agent_session_reference)
    is_private = resolved_id is not None or (
        isinstance(agent_session_reference, str)
        and agent_session_reference.startswith("runner:")
    )
    if is_private:
        name = runner_name.strip() if isinstance(runner_name, str) else ""
        return {
            "kind": "private",
            "id": resolved_id,
            "name": name or PRIVATE_RUNNER_FALLBACK_NAME,
            "pool": resolved_pool,
        }
    if (
        not isinstance(agent_session_reference, str)
        or not agent_session_reference.strip()
    ):
        return {"kind": "unknown", "id": None, "name": "Not recorded", "pool": None}
    return {
        "kind": "hosted",
        "id": None,
        "name": HOSTED_RUNNER_NAME,
        "pool": None,
    }


def resolve_runner_pool(
    flow: Flow,
    execution_context: Optional[Dict[str, Any]] = None,
    *,
    db: Optional[Session] = None,
) -> Optional[str]:
    """Resolve the runner pool for one execution.

    Precedence: trigger override > flow.runner_pool >
    account.default_runner_pool > any online private runner ("auto") >
    hosted executor (None). The literal ``server`` at any explicit level
    opts into the hosted executor.
    """
    details = (execution_context or {}).get("trigger_event_data") or {}
    override = details.get(RUNNER_OVERRIDE_KEY)
    payload = details.get("payload")
    if not override and isinstance(payload, dict):
        override = payload.get(RUNNER_OVERRIDE_KEY)
    chosen = _explicit_pool(override)
    if chosen is None:
        chosen = _explicit_pool(getattr(flow, "runner_pool", None))
    if chosen is None:
        chosen = _explicit_pool(_account_default_runner_pool(flow, db))
    if _is_server_pool(chosen):
        return None
    if chosen and not _is_auto_pool(chosen):
        return chosen
    account_id = getattr(flow, "account_id", None)
    if account_id is None:
        account_id = (execution_context or {}).get("account_id")
    if db is not None and _has_online_private_runner(
        db,
        account_id,
        agent_type=getattr(flow, "agent_type", None),
        agent_config=getattr(flow, "agent_config", None),
    ):
        return AUTO_RUNNER_POOL
    return None


class RunnerCapabilityError(ValueError):
    """An explicitly selected runner pool cannot execute the requested job."""


def validate_runner_capabilities(
    db: Session, flow: Flow, execution_context: Dict[str, Any]
) -> None:
    """Refuse container triggers explicitly routed to a pool without Docker."""
    from preloop.services.host_exec import host_exec_effective_profile

    agent_type = getattr(flow, "agent_type", None)
    agent_config = getattr(flow, "agent_config", None)
    if host_exec_effective_profile(agent_type, agent_config):
        return
    pool = resolve_runner_pool(flow, execution_context, db=db)
    if not pool or pool.lower() == AUTO_RUNNER_POOL:
        return
    matches = list(
        crud_flow_runner.find_matching(
            db, account_id=flow.account_id, pool=pool, online_only=False
        )
    )
    if (matches or pool.lower() == "docker") and not any(
        (runner.capabilities or {}).get("docker_available") is True
        for runner in matches
    ):
        raise RunnerCapabilityError(
            f"Runner pool '{pool}' cannot run container flows: no runner reports a "
            "working Docker daemon. Start Docker and verify docker ps as the runner "
            "user, or select a Docker-capable runner pool."
        )


_PERSISTED_SECRET_KEYS = ("account_api_token", "launch", "credential")


def persistable_job_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a lease payload without credentials that must not hit JSONB.

    The live WebSocket push still receives the original payload, including
    ``account_api_token``, for that lease only. A session checkout
    credential (``workspace.credential`` or any nested ``credential`` key)
    is removed at every depth.
    """
    from preloop.config import settings
    from preloop.services.runner_workspace_credentials import (
        without_checkout_credentials,
    )

    stored = without_checkout_credentials(dict(payload))
    for key in _PERSISTED_SECRET_KEYS:
        stored.pop(key, None)
    if (
        settings.flow_artifact_direct_upload
        and stored.get("completion_protocol") != "host_exec"
    ):
        stored["evidence_direct_upload"] = True
    return stored


def unwrap_agent_config(config: Any) -> Any:
    """Strip the doubly wrapped ``{"agent_config": {...}}`` storage shape.

    Some stored flow configurations nest the configuration inside a single
    ``agent_config`` key. Every reader has to agree on one unwrapping: a
    reader that misses it sees no ``runner`` section, and a host-bound
    continuation then reads as free to run anywhere.

    Args:
        config: A stored or payload agent configuration, of any type.

    Returns:
        The inner configuration when the wrapper is present, else ``config``.
    """
    if (
        isinstance(config, dict)
        and set(config) == {"agent_config"}
        and isinstance(config["agent_config"], dict)
    ):
        return config["agent_config"]
    return config


def workspace_owner_runner_id(
    db: Session, *, payload: Dict[str, Any]
) -> Optional[UUID]:
    """The runner that holds the local workspace this job wants to resume.

    A private runner keeps a persisted workspace in its own configuration
    directory and never uploads it. Resuming that work on a second machine
    would start from a cold clone and quietly drop the unpushed commits, so
    the continuation is pinned to the host that owns the directory.

    ``None`` means "any matching runner will do": either this is not a
    resume, the flow does not persist its workspace, or the prior execution
    was never assigned to a private runner.

    Args:
        db: Database session.
        payload: The lease payload built for this execution.

    Returns:
        The owning runner id, or None when the job is not host bound.
    """
    from preloop.models.crud import crud_flow_execution

    resume_from = payload.get("resume_from")
    host_resume = payload.get("host_exec_resume")
    if isinstance(host_resume, dict):
        # A Copilot continuation resumes a session that exists only on the
        # originating runner (#1069): always pinned.
        resume_from = host_resume.get("execution_id")
    if not isinstance(resume_from, str) or not resume_from.strip():
        return None
    config = unwrap_agent_config(payload.get("agent_config"))
    runner_config = config.get("runner") if isinstance(config, dict) else None
    host_bound = isinstance(host_resume, dict) or bool(host_exec_profile_name(config))
    if not host_bound and (
        not isinstance(runner_config, dict)
        or not _truthy(runner_config.get("persist_workspace"))
    ):
        return None
    try:
        prior_id = UUID(resume_from.strip())
    except (ValueError, AttributeError, TypeError):
        return None
    prior = crud_flow_execution.get(db, id=prior_id)
    owner = getattr(prior, "runner_id", None) if prior is not None else None
    if owner is None and prior is not None:
        owner = runner_id_from_session_reference(
            getattr(prior, "agent_session_reference", None)
        )
    return owner


def _truthy(value: Any) -> bool:
    """Match the private runner CLI's reading of ``persist_workspace``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def runner_wait_notice(runner: Optional[FlowRunner], execution_id: Any) -> str:
    """Operator-visible reason a host-bound continuation is still queued."""
    name = getattr(runner, "name", None) or PRIVATE_RUNNER_FALLBACK_NAME
    return (
        f"Waiting for private runner {name}: it holds the local workspace for "
        f"execution {execution_id}. The workspace is never uploaded, so this "
        "continuation cannot move to another host."
    )


def runner_blocked_notice(runner: Optional[FlowRunner], execution_id: Any) -> str:
    """Operator action required once a host-bound continuation times out."""
    name = getattr(runner, "name", None) or PRIVATE_RUNNER_FALLBACK_NAME
    return (
        f"Private runner {name} did not come online within {DEFAULT_QUEUE_TIMEOUT}. "
        f"The recovery workspace for execution {execution_id} is still on that "
        "host, under the runner configuration directory. Bring the runner back "
        "and retry, or start a fresh conversation from the published branch. "
        "Preloop does not upload private workspaces or move them to another host."
    )


def _runner_may_accept(
    db: Session,
    *,
    account_id: UUID,
    runner: FlowRunner,
    pool: str,
    execution_id: UUID,
    flow_id: Optional[str] = None,
    pinned_runner_id: Optional[UUID] = None,
    harness: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> bool:
    """Whether the account authorizer (hook H4) lets ``runner`` take the job.

    Asked with the ``runner:accept`` action before a slot is claimed, so a
    denied runner is skipped without a claim to roll back. True when no
    authorizer is registered.
    """
    from preloop.plugins.account_hooks import (
        ACTION_RUNNER_ACCEPT,
        AuthorizationContext,
        authorize,
        get_authorizer,
    )

    if get_authorizer() is None:
        return True
    if flow_id is None:
        from preloop.models.crud import crud_flow_execution

        execution = crud_flow_execution.get(
            db, id=str(execution_id), account_id=account_id
        )
        flow_id = str(execution.flow_id) if execution else None
    ctx = AuthorizationContext(
        account_id=account_id,
        db=db,
        attributes={
            "pool": pool,
            "execution_id": str(execution_id),
            "flow_id": flow_id,
            "pinned_runner_id": (
                str(pinned_runner_id) if pinned_runner_id is not None else None
            ),
            "harness": harness,
            "workspace_id": workspace_id,
        },
    )
    return authorize(ctx, ACTION_RUNNER_ACCEPT, runner).allowed


#: Lease payload key carrying the harness selection (contract B). Data only:
#: the runner resolves the named profile locally, never an executable.
HOST_EXEC_LEASE_KEY = "host_exec"

#: How far a runner got through the eligibility checks. The routing reason
#: shown for a queued run comes from the runner that got furthest.
_REASON_RANK = {
    ROUTING_OWNER_HAS_NO_RUNNER: 0,
    ROUTING_NO_RUNNER_WITH_HARNESS: 1,
    ROUTING_HARNESS_DISABLED: 2,
    ROUTING_HARNESS_SIGNED_OUT: 3,
    ROUTING_MODEL_NOT_AVAILABLE: 4,
    ROUTING_RUNNER_OFFLINE: 5,
    ROUTING_RUNNER_ASLEEP: 5,
}


def runner_away_reason(runner: Any) -> Optional[str]:
    """``runner_asleep`` or ``runner_offline`` when presence says so (#1508).

    Presence comes from :func:`derive_state`, the single source of truth
    for ``asleep`` and ``offline``. A runner that is reachable (online,
    busy or idle) returns None.

    Args:
        runner: Runner row, or None for a runner that no longer exists.

    Returns:
        The routing reason, or None when the runner is reachable or missing.
    """
    if runner is None:
        return None
    state = derive_state(runner)
    if state == PRESENCE_ASLEEP:
        return ROUTING_RUNNER_ASLEEP
    if state == PRESENCE_OFFLINE:
        return ROUTING_RUNNER_OFFLINE
    return None


def runner_harness_entries(runner: Any) -> Optional[List[Mapping[str, Any]]]:
    """Inventory entries a runner published, or None for "inventory unknown".

    Reads ``flow_runners.harness_inventory`` (contract A). A runner that
    predates the inventory has none and is never eligible for harness
    routing; profile-pinned flows do not read this.
    """
    inventory = getattr(runner, "harness_inventory", None)
    dump = getattr(inventory, "model_dump", None)
    if callable(dump):
        inventory = dump(mode="json", by_alias=True)
    if not isinstance(inventory, Mapping):
        return None
    entries = inventory.get("entries")
    if not isinstance(entries, list):
        return None
    return [entry for entry in entries if isinstance(entry, Mapping)]


def runner_harness_entry(runner: Any, harness: str) -> Optional[Mapping[str, Any]]:
    """The runner's inventory entry for ``harness``, if it reported one."""
    for entry in runner_harness_entries(runner) or []:
        if entry.get("harness") == harness:
            return entry
    return None


def harness_entry_lists_model(entry: Mapping[str, Any], model: Optional[str]) -> bool:
    """Whether ``model`` may run on this harness entry.

    True when no model is requested, the entry lists it, or the harness
    cannot list models (no ``probed`` source): Copilot has no listing, so an
    unknown model is allowed and fails fast on the host with the existing
    "--model not available" mapping.
    """
    if not model:
        return True
    models = entry.get("models") or []
    if not isinstance(models, list):
        return True
    ids = {item.get("id") for item in models if isinstance(item, Mapping)}
    if model in ids:
        return True
    return not any(
        isinstance(item, Mapping) and item.get("source") == "probed" for item in models
    )


def harness_entry_reason(
    entry: Optional[Mapping[str, Any]], model: Optional[str]
) -> Optional[str]:
    """Why an inventory entry cannot take the run, or None when it can."""
    if entry is None or entry.get("support_level") in (None, "presence_only"):
        return ROUTING_NO_RUNNER_WITH_HARNESS
    if entry.get("enabled") is False:
        return ROUTING_HARNESS_DISABLED
    if entry.get("login_state") != "signed_in":
        return ROUTING_HARNESS_SIGNED_OUT
    if not harness_entry_lists_model(entry, model):
        return ROUTING_MODEL_NOT_AVAILABLE
    return None


def harness_entry_billing(entry: Optional[Mapping[str, Any]]) -> str:
    """``seat | metered | unknown`` from the inventory entry."""
    value = entry.get("billing") if entry is not None else None
    return value if value in BILLING_MODES else BILLING_MODE_UNKNOWN


def _runner_registered_by_admin(
    db: Optional[Session], runner: Any, cache: Optional[Dict[Any, bool]] = None
) -> bool:
    """Whether the runner's registrant is an account admin.

    Superuser, the account's primary user, or a holder of ``manage_account``
    (the same rule as the console's admin checks).
    """
    user_id = getattr(runner, "registered_by_user_id", None)
    if user_id is None or db is None:
        return False
    if cache is not None and user_id in cache:
        return cache[user_id]
    allowed = _user_is_account_admin(db, crud_user.get(db, id=user_id))
    if cache is not None:
        cache[user_id] = allowed
    return allowed


def _user_is_account_admin(db: Session, user: Any) -> bool:
    """Superuser, the account's primary user, or a ``manage_account`` holder."""
    from preloop.utils.permissions import user_holds_permission

    if user is None:
        return False
    if getattr(user, "is_superuser", False):
        return True
    account = crud_account.get(db, id=getattr(user, "account_id", None))
    if account is not None and str(getattr(account, "primary_user_id", "")) == str(
        user.id
    ):
        return True
    return bool(user_holds_permission(db, user, "manage_account"))


def _flow_author_may_route_to(
    db: Session,
    runner: Any,
    *,
    account_id: UUID,
    pool: Optional[str],
    flow_id: Optional[str],
    pinned_runner_id: Optional[UUID],
    harness: Optional[str],
    workspace_id: Optional[str],
    admin_cache: Dict[Any, bool],
) -> bool:
    """Whether an explicit ``runner_pool`` may route the flow to ``runner``.

    A pool can name any runner by id, name or label. For a personal runner
    (registered by a member who is not an account admin) the flow author
    must be allowed to use it for flows, the same rule as pinning it with
    ``agent_config.runner_id``: its registrant, an account admin, or a
    ``runner:flow_use`` grant from the authorizer (#2176).

    Runners registered by an admin or by nobody stay routable by pool, the
    ``auto`` and ``server`` pools are unaffected, and pinned leases are
    checked when the flow is saved. Flows with no recorded author keep the
    runner's flow consent as their gate: legacy rows that predate authorship,
    and flows whose author was hard deleted (``created_by_user_id`` is
    ``ON DELETE SET NULL``, so the two cannot be told apart). A deactivated
    author is refused.
    """
    if pinned_runner_id is not None:
        return True
    explicit = _explicit_pool(pool)
    if not explicit or _is_auto_pool(explicit) or _is_server_pool(explicit):
        return True
    return _flow_author_may_use_runner(
        db,
        runner,
        account_id=account_id,
        pool=explicit,
        flow_id=flow_id,
        harness=harness,
        workspace_id=workspace_id,
        admin_cache=admin_cache,
    )


def _flow_author_may_use_runner(
    db: Session,
    runner: Any,
    *,
    account_id: UUID,
    pool: Optional[str],
    flow_id: Optional[str],
    harness: Optional[str],
    workspace_id: Optional[str],
    admin_cache: Dict[Any, bool],
    require_author: bool = False,
) -> bool:
    """Whether the flow's author may use ``runner`` for flows (#2176).

    Runners registered by an admin or by nobody are usable; a personal
    runner needs its registrant as author, or a ``runner:flow_use`` grant.

    Args:
        db: Database session.
        runner: Candidate runner.
        account_id: Account the flow runs in.
        pool: Pool string recorded on the authorization context.
        flow_id: The flow, whose ``created_by_user_id`` is the author.
        harness: Harness of the run, for the authorizer.
        workspace_id: Authorized directory id, for the authorizer.
        admin_cache: Memo of "registered by an admin" per user id.
        require_author: Refuse personal runners when the flow has no known
            author. Reroute (#1508) sets it: a legacy authorless flow may
            keep its own pinned runner but is not moved to someone else's.

    Returns:
        True when the author may route work to the runner.
    """
    owner_id = getattr(runner, "registered_by_user_id", None)
    if owner_id is None or _runner_registered_by_admin(db, runner, admin_cache):
        return True
    from preloop.models.crud import crud_flow
    from preloop.plugins.account_hooks import (
        AuthorizationContext,
        may_use_runner_for_flows,
    )

    flow = crud_flow.get(db, id=flow_id) if flow_id else None
    if flow is None:
        return False
    author_id = getattr(flow, "created_by_user_id", None)
    if author_id is None:
        return not require_author
    if str(author_id) == str(owner_id):
        return True
    author = crud_user.get(db, id=author_id)
    if author is None or not getattr(author, "is_active", True):
        return False
    return may_use_runner_for_flows(
        AuthorizationContext(
            account_id=account_id,
            db=db,
            user=author,
            attributes={
                "operation": "lease",
                "pool": pool,
                "flow_id": flow_id,
                "harness": harness,
                "workspace_id": workspace_id,
            },
        ),
        runner,
        is_account_admin=_user_is_account_admin(db, author),
    ).allowed


def runner_usable_by_flow(
    db: Optional[Session],
    runner: Any,
    *,
    pool: Optional[str],
    pinned_runner_id: Optional[UUID] = None,
    admin_cache: Optional[Dict[Any, bool]] = None,
) -> bool:
    """Whether a flow may route harness work to this runner (wave 1).

    A flow uses a runner it names (``agent_config.runner_id`` or an explicit
    ``runner_pool``). An unpinned flow uses only runners registered by an
    account admin, so a member's personal runner never takes work it was
    not named for. EE sharing (#183) plugs in through ``runner:accept``.
    """
    if pinned_runner_id is not None:
        return getattr(runner, "id", None) == pinned_runner_id
    explicit = _explicit_pool(pool)
    if explicit and not _is_auto_pool(explicit) and not _is_server_pool(explicit):
        from preloop.models.crud.flow_runner import runner_matches_pool

        return runner_matches_pool(runner, explicit)
    return _runner_registered_by_admin(db, runner, admin_cache)


def runner_eligible_for_harness(
    db: Optional[Session],
    runner: Any,
    *,
    account_id: Any,
    harness: str,
    model: Optional[str] = None,
    pool: Optional[str] = None,
    pinned_runner_id: Optional[UUID] = None,
    admin_cache: Optional[Dict[Any, bool]] = None,
    flow_id: Optional[str] = None,
    reroute_from: Optional[UUID] = None,
    required_billing: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[Mapping[str, Any]]]:
    """The single eligibility rule for harness routing (contract B).

    Used by the lease and by the flow editor. A runner is eligible when it
    belongs to the account, the flow may use it, its inventory has an
    enabled, signed-in, runnable entry for ``harness`` that can run
    ``model``, and it is online. When ``flow_id`` is given, the flow's
    author must also be allowed on the runner for explicit pool routing
    (:func:`_flow_author_may_route_to`).

    ``reroute_from`` (``offline_policy=reroute``, #1508) replaces the pin:
    any other runner the flow's author may use for flows qualifies, and
    ``required_billing`` keeps the billing path of the pinned runner, so a
    seat run is never moved to a gateway-metered path.

    Returns:
        ``(eligible, reason, entry)``. ``reason`` is a routing reason when
        not eligible.
    """
    if account_id is not None and str(getattr(runner, "account_id", "")) != str(
        account_id
    ):
        return False, ROUTING_OWNER_HAS_NO_RUNNER, None
    if reroute_from is not None:
        if getattr(runner, "id", None) == reroute_from or db is None:
            return False, ROUTING_OWNER_HAS_NO_RUNNER, None
        if not _flow_author_may_use_runner(
            db,
            runner,
            account_id=account_id,
            pool=AUTO_RUNNER_POOL,
            flow_id=flow_id,
            harness=harness,
            workspace_id=None,
            admin_cache=admin_cache if admin_cache is not None else {},
            require_author=True,
        ):
            return False, ROUTING_OWNER_HAS_NO_RUNNER, None
    elif not runner_usable_by_flow(
        db,
        runner,
        pool=pool,
        pinned_runner_id=pinned_runner_id,
        admin_cache=admin_cache,
    ):
        return False, ROUTING_OWNER_HAS_NO_RUNNER, None
    # A known flow also needs its author to be allowed on the runner (#2176),
    # so routing and fallback decisions agree with what the lease accepts.
    if (
        reroute_from is None
        and flow_id is not None
        and not _flow_author_may_route_to(
            db,
            runner,
            account_id=account_id,
            pool=pool,
            flow_id=flow_id,
            pinned_runner_id=pinned_runner_id,
            harness=harness,
            workspace_id=None,
            admin_cache=admin_cache if admin_cache is not None else {},
        )
    ):
        return False, ROUTING_OWNER_HAS_NO_RUNNER, None
    entry = runner_harness_entry(runner, harness)
    reason = harness_entry_reason(entry, model)
    if reason is not None:
        return False, reason, entry
    if required_billing is not None and harness_entry_billing(entry) != (
        required_billing
    ):
        # Reroute keeps the policy path: a seat harness never lands on a
        # metered (gateway) one, nor the other way round.
        return False, ROUTING_NO_RUNNER_WITH_HARNESS, entry
    # Checked first: a runner that said going_to_sleep is asleep whether or
    # not its socket has dropped yet (#1506 derive_state).
    if getattr(runner, "sleep_signaled_at", None) is not None:
        return False, ROUTING_RUNNER_ASLEEP, entry
    if getattr(runner, "status", None) not in ("online", "busy") or not is_online(
        runner
    ):
        return False, ROUTING_RUNNER_OFFLINE, entry
    return True, None, entry


def harness_routing_reason(
    db: Optional[Session],
    runners: Iterable[Any],
    *,
    account_id: Any,
    harness: str,
    model: Optional[str] = None,
    pool: Optional[str] = None,
    pinned_runner_id: Optional[UUID] = None,
    flow_id: Optional[str] = None,
    reroute_from: Optional[UUID] = None,
    required_billing: Optional[str] = None,
) -> Tuple[int, Optional[str]]:
    """Count eligible runners and name why the rest cannot take the run.

    ``reroute_from`` and ``required_billing`` are passed to
    :func:`runner_eligible_for_harness` unchanged.

    Returns:
        ``(eligible_count, reason)``. ``reason`` is None when at least one
        runner is eligible, else the reason of the runner that got furthest
        (``owner_has_no_runner`` when there is no runner at all).
    """
    eligible = 0
    best: Optional[str] = None
    cache: Dict[Any, bool] = {}
    for runner in runners:
        ok, reason, _entry = runner_eligible_for_harness(
            db,
            runner,
            account_id=account_id,
            harness=harness,
            model=model,
            pool=pool,
            pinned_runner_id=pinned_runner_id,
            admin_cache=cache,
            flow_id=flow_id,
            reroute_from=reroute_from,
            required_billing=required_billing,
        )
        if ok:
            eligible += 1
        elif reason is not None and (
            best is None or _REASON_RANK[reason] > _REASON_RANK[best]
        ):
            best = reason
    if eligible:
        return eligible, None
    return 0, best or ROUTING_OWNER_HAS_NO_RUNNER


def runner_can_take_lease_now(
    runner: Any,
    *,
    profile: Optional[str],
    agent_type: Any,
    workspace_id: Optional[str],
    harness: Optional[str],
) -> bool:
    """The lease checks beyond harness eligibility, without claiming a slot.

    Mirrors what :func:`lease_job` requires of a candidate before it claims
    one: a free slot, not paused, flow consent other than ``never`` (and an
    owner to ask when it is ``ask_me``), the host profile and the
    authorized directory. Used where a decision must agree with whether a
    lease would succeed (``offline_policy=reroute`` versus the hosted
    fallback, #1508).

    Args:
        runner: Candidate runner.
        profile: Host profile the lease names, if any.
        agent_type: Agent type of the run.
        workspace_id: Authorized directory the run works in, if any.
        harness: Harness the directory must allow.

    Returns:
        True when the runner could take the lease right now.
    """
    if getattr(runner, "paused", False) is True:
        return False
    if getattr(runner, "status", None) not in ("online", "busy"):
        return False
    if not isinstance(getattr(runner, "free_slots", 0), int) or runner.free_slots <= 0:
        return False
    consent = getattr(runner, "flow_execution_consent", None) or "ask_me"
    if consent == "never" or (
        consent != "always" and getattr(runner, "registered_by_user_id", None) is None
    ):
        return False
    if profile and not runner_has_host_exec_profile(
        runner, profile, None, agent_type or HOST_EXEC_AGENT_TYPE
    ):
        return False
    return not workspace_id or runner_offers_authorized_directory(
        runner, workspace_id, harness
    )


def runner_accepts_job(
    runner: FlowRunner,
    pool: str,
    payload: Dict[str, Any],
    *,
    pinned_runner_id: Optional[UUID] = None,
) -> bool:
    """Require a usable execution capability and honor scheduling preferences."""
    if getattr(runner, "paused", False) is True:
        return False
    native = bool(host_exec_profile_name(payload))
    if (not native or payload.get("_publication")) and (
        getattr(runner, "capabilities", None) or {}
    ).get("docker_available") is not True:
        return False
    selection = payload.get(HOST_EXEC_LEASE_KEY)
    harness = selection.get("harness") if isinstance(selection, Mapping) else None
    tagged = bool(
        harness or native or pinned_runner_id or (pool and pool.lower() != "auto")
    )
    return tagged or getattr(runner, "accept_untagged", False) is True


def lease_job(
    db: Session,
    *,
    account_id: UUID,
    pool: str,
    execution_id: UUID,
    payload: Dict[str, Any],
    required_runner_id: Optional[UUID] = None,
    outcome: Optional[Dict[str, Any]] = None,
    reroute_from: Optional[UUID] = None,
    required_billing: Optional[str] = None,
) -> Optional[FlowRunner]:
    """Assign a pending job to one matching online runner. None if queued.

    ``reroute_from`` (``offline_policy=reroute``, #1508) leases a harness
    job pinned to that asleep or offline runner to another runner in the
    pool that the flow's author may use, with the same harness, model and
    ``required_billing``. It never leases a job without a harness selection
    and never considers ``reroute_from`` itself.

    A payload with ``host_exec`` (contract B harness routing) only goes to a
    runner that :func:`runner_eligible_for_harness` accepts. ``outcome``,
    when given, receives ``billing_mode`` (from the inventory entry) on a
    lease and ``routing_reason`` when the job stays queued.

    Candidates are re-fetched with ``SELECT ... FOR UPDATE SKIP LOCKED`` so
    two concurrent leases cannot hand out the same free slot. A runner may
    hold up to its capacity at once; ``find_matching`` already orders the
    emptiest machine first. Credentials are stripped from the stored payload;
    the caller still holds the original dict for the in-memory WebSocket push.

    ``required_runner_id`` pins the job to the host that holds its local
    recovery state. No other runner is considered, even an idle one in the
    same pool: the work would restart from a cold clone there.
    """
    from preloop.models.crud import crud_audit_log, crud_flow_execution

    if reroute_from is not None:
        selection = payload.get(HOST_EXEC_LEASE_KEY)
        if required_runner_id is not None or not (
            isinstance(selection, Mapping) and selection.get("harness")
        ):
            return None
    # Keep the account lock through lease assignment. A stopped queued
    # execution cannot acquire a new runner after activation's snapshot.
    if not crud_flow_execution.admit_runtime_start(
        db, execution_id=execution_id, commit=False
    ):
        db.commit()
        return None
    matches = crud_flow_runner.find_matching(
        db, account_id=account_id, pool=pool, online_only=True
    )
    from preloop.models.crud.owner_consent import runner_for_execution

    consent_runner = runner_for_execution(
        db, account_id=account_id, execution_id=execution_id
    )
    if isinstance(consent_runner, str):
        matches = [row for row in matches if str(row.id) == consent_runner]
    if required_runner_id is not None:
        matches = [row for row in matches if row.id == required_runner_id]
    if reroute_from is not None:
        matches = [row for row in matches if row.id != reroute_from]
    available = [
        row
        for row in matches
        if row.status in ("online", "busy") and row.free_slots > 0
    ]
    required_profile = host_exec_profile_name(payload)
    selection = payload.get(HOST_EXEC_LEASE_KEY)
    harness = selection.get("harness") if isinstance(selection, Mapping) else None
    harness_model = selection.get("model") if isinstance(selection, Mapping) else None
    workspace = payload.get(HOST_EXEC_WORKSPACE_KEY)
    workspace_id = workspace.get("id") if isinstance(workspace, Mapping) else None
    workspace_harness = harness or host_exec_harness(payload.get("agent_type"))
    # ``agent_config.runner_id`` and a host-bound continuation both arrive
    # as ``required_runner_id``: a runner the flow names explicitly.
    pinned = required_runner_id
    admin_cache: Dict[Any, bool] = {}
    stored = persistable_job_payload(payload)
    execution = crud_flow_execution.get(db, id=str(execution_id), account_id=account_id)
    flow_id = str(execution.flow_id) if execution else None
    for candidate in available:
        if not runner_accepts_job(candidate, pool, payload, pinned_runner_id=pinned):
            continue
        entry = None
        if harness:
            ok, _reason, entry = runner_eligible_for_harness(
                db,
                candidate,
                account_id=account_id,
                harness=harness,
                model=harness_model,
                pool=pool,
                pinned_runner_id=pinned,
                admin_cache=admin_cache,
                flow_id=flow_id,
                reroute_from=reroute_from,
                required_billing=required_billing,
            )
            if not ok:
                continue
        if not _runner_may_accept(
            db,
            account_id=account_id,
            runner=candidate,
            pool=pool,
            execution_id=execution_id,
            flow_id=flow_id,
            pinned_runner_id=pinned,
            harness=workspace_harness,
            workspace_id=workspace_id,
        ):
            continue
        if not _flow_author_may_route_to(
            db,
            candidate,
            account_id=account_id,
            pool=pool,
            flow_id=flow_id,
            pinned_runner_id=pinned,
            harness=workspace_harness,
            workspace_id=workspace_id,
            admin_cache=admin_cache,
        ):
            continue
        if required_profile and not runner_has_host_exec_profile(
            candidate,
            required_profile,
            # Harness routing checked the model against the inventory; the
            # profile advertisement need not repeat the model list.
            None if harness else payload.get("model_identifier"),
            payload.get("agent_type") or HOST_EXEC_AGENT_TYPE,
            require_publication=bool(payload.get("host_exec_publication")),
            require_continuation=bool(payload.get("host_exec_resume")),
            require_read_only_review=bool(payload.get("host_exec_read_only")),
        ):
            continue
        if workspace_id and not runner_offers_authorized_directory(
            candidate, workspace_id, workspace_harness
        ):
            # Only a runner whose owner authorized this directory for the
            # harness may take a run that works in it.
            continue
        consent_mode = getattr(candidate, "flow_execution_consent", None) or "ask_me"
        consent_owner = getattr(candidate, "registered_by_user_id", None)
        if consent_mode == "never":
            crud_audit_log.log_action(
                db,
                account_id=account_id,
                action="runner_flow_consent",
                resource_type="flow_runner",
                resource_id=str(candidate.id),
                status="denied",
                details={"execution_id": str(execution_id), "decision": "never"},
                commit=False,
            )
            if outcome is not None:
                outcome["consent_status"] = "declined"
            continue
        if consent_mode != "always":
            if consent_owner is None:
                # Legacy/system runners cannot answer an owner request. Keep
                # looking for usable capacity instead of inventing a refusal.
                continue
            from preloop.services.owner_consent import check_owner_consent

            try:
                consent = check_owner_consent(
                    db,
                    account_id=getattr(candidate, "account_id", account_id),
                    owner_user_id=consent_owner,
                    action="runner_flow",
                    subject_id=str(candidate.id),
                    request_key=f"{execution_id}:{candidate.id}:{consent_owner}",
                    details={
                        "execution_id": str(execution_id),
                        "runner_id": str(candidate.id),
                    },
                    execution_id=str(execution_id),
                )
            except ValueError:
                # An unavailable or changed owner is not an owner decline.
                # A different eligible runner may still accept this work.
                continue
            if outcome is not None:
                outcome["owner_consent"] = consent
                outcome["consent_status"] = consent.status
            if consent.status != "approved":
                # Bind this run to one owner's decision rather than asking
                # every machine in the pool for the same work.
                db.commit()
                return None
            # Approval persistence releases the account lock. Recheck runtime
            # admission and claim the runner anew before constructing a lease.
            if not crud_flow_execution.admit_runtime_start(
                db, execution_id=execution_id, commit=False
            ):
                db.commit()
                return None
        runner = crud_flow_runner.claim_free_slot(db, runner_id=candidate.id)
        if runner is None:
            continue
        if (
            getattr(runner, "flow_execution_consent", None) or "ask_me"
        ) != consent_mode or getattr(
            runner, "registered_by_user_id", None
        ) != consent_owner:
            db.rollback()
            continue
        if not runner_accepts_job(runner, pool, payload, pinned_runner_id=pinned):
            db.rollback()
            continue
        if payload.get("_publication"):
            capability = runner.publication_capabilities or {}
            if (
                capability.get("version") != 1
                or capability.get("helper_ready") is not True
                or payload.get("agent_type")
                not in {"codex", "opencode", "pi", "deepseek"}
            ):
                db.rollback()
                continue
            crud_flow_runner.bind_publication_lease(
                db,
                runner_id=runner.id,
                execution_id=execution_id,
                account_id=account_id,
                nonce=payload["_publication"]["nonce"],
            )
        if outcome is not None:
            outcome["consent_status"] = "approved"
        assignment = crud_flow_runner.create_assignment(
            db,
            runner_id=runner.id,
            execution_id=execution_id,
            pending_job=stored,
            commit=False,
        )
        assignment.reported_status = "PENDING"
        crud_audit_log.log_action(
            db,
            account_id=account_id,
            action="runner_flow_consent",
            resource_type="flow_runner",
            resource_id=str(runner.id),
            status="success",
            details={"execution_id": str(execution_id), "decision": consent_mode},
            commit=False,
        )
        db.add(assignment)
        db.commit()
        db.refresh(runner)
        emit_runner_updated(runner, db)
        if outcome is not None and harness:
            outcome["billing_mode"] = harness_entry_billing(entry)
        return runner
    db.commit()
    if outcome is not None and harness:
        everyone = crud_flow_runner.find_matching(
            db, account_id=account_id, pool=pool, online_only=False
        )
        if required_runner_id is not None:
            everyone = [row for row in everyone if row.id == required_runner_id]
        _count, reason = harness_routing_reason(
            db,
            everyone,
            account_id=account_id,
            harness=harness,
            model=harness_model,
            pool=pool,
            pinned_runner_id=pinned,
            flow_id=flow_id,
            reroute_from=reroute_from,
            required_billing=required_billing,
        )
        # Eligible but every slot is taken: still queued, no named cause.
        outcome["routing_reason"] = reason
    return None


def mark_queued_or_fail(
    *,
    queued_since: datetime,
    timeout: timedelta = DEFAULT_QUEUE_TIMEOUT,
) -> str:
    """Return PENDING while waiting, FAILED after the offline timeout."""
    now = datetime.now(timezone.utc)
    if queued_since.tzinfo is None:
        queued_since = queued_since.replace(tzinfo=timezone.utc)
    if now - queued_since > timeout:
        return "FAILED"
    return "PENDING"


def is_online(runner: FlowRunner) -> bool:
    if not runner.last_heartbeat:
        return False
    hb = runner.last_heartbeat
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - hb <= ONLINE_HEARTBEAT_TTL


def runner_console_payload(
    runner: FlowRunner, *, registered_by_email: Optional[str] = None
) -> Dict[str, Any]:
    """Fields the console runners table needs. Never includes the runner token."""
    heartbeat = runner.last_heartbeat
    execution_id = runner.current_execution_id
    assignments = list(getattr(runner, "assignments", None) or [])
    inventory_updated = getattr(runner, "harness_inventory_updated_at", None)
    from preloop.models.crud.flow_runner import runner_labels

    presence = derive_presence(runner)
    return {
        "id": str(runner.id),
        "name": runner.name,
        "hostname": runner.hostname,
        "os": runner.os,
        "arch": runner.arch,
        "labels": runner_labels(runner),
        "paused": bool(getattr(runner, "paused", False)),
        "accept_untagged": bool(getattr(runner, "accept_untagged", False)),
        "flow_execution_consent": getattr(runner, "flow_execution_consent", None)
        or "ask_me",
        "status": runner.status,
        "last_heartbeat": heartbeat.isoformat() if heartbeat is not None else None,
        "current_execution_id": str(execution_id) if execution_id else None,
        "concurrency": int(getattr(runner, "concurrency", 0) or 0),
        "capacity": int(getattr(runner, "capacity", 0) or 0),
        "running_count": len(assignments),
        "running_execution_ids": [str(row.execution_id) for row in assignments],
        "registered_by_user_id": (
            str(runner.registered_by_user_id) if runner.registered_by_user_id else None
        ),
        "registered_by_email": registered_by_email,
        "capabilities": dict(getattr(runner, "capabilities", None) or {}),
        # Console events go to every account member, so they carry the
        # member view; owners and admins get version and host from the API.
        "harness_inventory": redact_harness_inventory(
            getattr(runner, "harness_inventory", None)
        ),
        "harness_inventory_updated_at": (
            inventory_updated.isoformat() if inventory_updated is not None else None
        ),
        "presence": presence.state,
        "presence_since": (
            presence.since.isoformat() if presence.since is not None else None
        ),
    }


def emit_runner_updated(runner: FlowRunner, db: Optional[Session] = None) -> None:
    """Push one runner row to console websockets subscribed to ``runners``."""
    if not getattr(runner, "account_id", None):
        return
    email = None
    user_id = getattr(runner, "registered_by_user_id", None)
    if db is not None and user_id:
        user = crud_user.get(db, id=user_id)
        if user is not None:
            email = getattr(user, "email", None)
    emit_account_event(
        build_account_event(
            account_id=str(runner.account_id),
            topic=ACCOUNT_TOPIC_RUNNERS,
            event_type="runner_updated",
            runner_id=str(runner.id),
            payload=runner_console_payload(runner, registered_by_email=email),
        )
    )


def emit_runner_deleted(account_id: Any, runner_id: Any) -> None:
    """Tell console websockets subscribed to ``runners`` that a row is gone."""
    if not account_id or not runner_id:
        return
    emit_account_event(
        build_account_event(
            account_id=str(account_id),
            topic=ACCOUNT_TOPIC_RUNNERS,
            event_type="runner_deleted",
            runner_id=str(runner_id),
            payload={"id": str(runner_id)},
        )
    )


_HARNESS_DISPLAY_NAMES = {
    "copilot_cli": "GitHub Copilot CLI",
    "cursor_cli": "Cursor CLI",
    "devin_cli": "Devin CLI",
    "claude_code": "Claude Code",
}


def harness_options(
    db: Optional[Session],
    runners: Iterable[Any],
    *,
    account_id: Any,
    usable: Any,
    usable_harness: Any = None,
) -> Dict[str, Any]:
    """Harness + model choices for the flow editor (contract B).

    Args:
        db: Database session (unused by the pure checks; kept for symmetry).
        runners: Account runners.
        account_id: Caller's account.
        usable: ``callable(runner) -> bool``, whether the caller may route
            to the runner (registrant or account admin in wave 1).
        usable_harness: Optional ``callable(runner, harness) -> bool``. When
            given, a harness the caller may not pin on that runner (a share
            limited to other harnesses) is left out for that runner.

    Returns:
        ``{"harnesses": [...]}`` for runnable harnesses (``copilot_cli``,
        ``cursor_cli``, ``devin_cli``, ``claude_code``) reported by at least one usable runner. A runner is
        counted online when :func:`runner_eligible_for_harness` accepts it
        (pinned to itself, so the owner rule is the caller's, not a flow's).
    """
    from preloop.services.host_exec import (
        HOST_EXEC_AGENT_TYPE_BY_HARNESS,
    )

    options: Dict[str, Dict[str, Any]] = {}
    for runner in runners:
        if str(getattr(runner, "account_id", "")) != str(account_id) or not usable(
            runner
        ):
            continue
        for entry in runner_harness_entries(runner) or []:
            harness = entry.get("harness")
            if harness not in HOST_EXEC_AGENT_TYPE_BY_HARNESS:
                continue
            if entry.get("support_level") in (None, "presence_only"):
                continue
            if usable_harness is not None and not usable_harness(runner, harness):
                continue
            ok, reason, _entry = runner_eligible_for_harness(
                db,
                runner,
                account_id=account_id,
                harness=harness,
                pinned_runner_id=getattr(runner, "id", None),
            )
            option = options.setdefault(
                harness,
                {
                    "harness": harness,
                    "display_name": _HARNESS_DISPLAY_NAMES.get(
                        harness, entry.get("display_name") or harness
                    ),
                    "agent_type": HOST_EXEC_AGENT_TYPE_BY_HARNESS[harness],
                    "billing": harness_entry_billing(entry),
                    "models": {},
                    "runners_online": 0,
                    "runners_total": 0,
                    "runners": [],
                },
            )
            option["runners_total"] += 1
            if ok:
                option["runners_online"] += 1
            option["runners"].append(
                {
                    "id": runner.id,
                    "name": runner.name,
                    "online": bool(
                        getattr(runner, "status", None) in ("online", "busy")
                        and is_online(runner)
                    ),
                    "eligible": ok,
                    "reason": reason,
                }
            )
            for item in entry.get("models") or []:
                if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                    continue
                model = option["models"].setdefault(
                    item["id"],
                    {
                        "id": item["id"],
                        "source": item.get("source") or "static",
                        "runners_online": 0,
                    },
                )
                if ok:
                    model["runners_online"] += 1
    harnesses = []
    for harness in sorted(options):
        option = options[harness]
        option["models"] = list(option["models"].values())
        harnesses.append(option)
    return {"harnesses": harnesses}


def resolve_fallback_server_model(db: Session, *, account_id: Any, wanted: Any) -> Any:
    """The AI model a ``fallback_server`` run uses, or None.

    ``wanted`` is an AI model id or model identifier visible to the account
    that the hosted fallback harness can reach. Used when the flow is saved
    (reject an unknown value) and when the run starts (load the model), so a
    flow cannot be saved in a state that only fails when the fallback is
    needed.
    """
    from preloop.models.crud import crud_ai_model
    from preloop.services.host_exec import HARNESS_SERVER_FALLBACK_AGENT_TYPE
    from preloop.services.model_routing import model_usable_for_agent

    name = wanted.strip() if isinstance(wanted, str) else ""
    if not name or account_id is None:
        return None
    for model in crud_ai_model.get_all_for_account(db, account_id=account_id):
        if str(model.id) != name and model.model_identifier != name:
            continue
        if model_usable_for_agent(model, HARNESS_SERVER_FALLBACK_AGENT_TYPE):
            return model
    return None
