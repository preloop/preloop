"""Atomic restricted runtime exchange using existing policy and hashed-key rows.

Provider verification is a trusted adapter responsibility. This module accepts
verified identities, never bearer tokens. Policy/session state share one locked
SecretReference; only one secret delivery is supported. Every replay refuses
rather than minting renewed authority, even after a policy generation change.
"""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from preloop.models import models

POLICY_KEY = "restricted_runtime_policy"
SESSIONS_KEY = "restricted_runtime_sessions"
MAX_SESSIONS = 1024
MAX_TTL_SECONDS = 600


class RestrictedRuntimeDeniedError(ValueError):
    """Safe denial code, excluding provider identity and credential material."""


class ResourceScope(BaseModel):
    """An immutable server ID and explicit upstream tool ceiling."""

    model_config = ConfigDict(extra="forbid")
    server_id: UUID
    tools: list[Annotated[str, Field(min_length=1, max_length=128, strict=True)]] = (
        Field(min_length=1, max_length=64)
    )

    @model_validator(mode="after")
    def explicit_tools(self) -> ResourceScope:
        """Reject wildcards, ambiguous names and duplicated tool grants."""
        if len(set(self.tools)) != len(self.tools) or any(
            "*" in tool or tool.strip() != tool for tool in self.tools
        ):
            raise ValueError("Explicit unique tools required")
        return self


class RuntimePolicy(BaseModel):
    """Static authorization ceiling, not a second policy expression language."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    enabled: Annotated[bool, Field(strict=True)]
    user_id: UUID
    managed_agent_id: UUID
    enrollment_id: UUID
    policy_snapshot_id: UUID
    scopes: list[Literal["mcp:read", "mcp:write"]] = Field(min_length=1, max_length=2)
    resource_scope: list[ResourceScope] = Field(min_length=1, max_length=32)
    expires_at: Annotated[
        float, Field(strict=True, allow_inf_nan=False, le=253402300799)
    ]
    grant_ttl_seconds: Annotated[int, Field(strict=True, ge=1, le=600)] = 600

    @model_validator(mode="after")
    def unique_ceiling(self) -> RuntimePolicy:
        """Reject duplicate resource identities or incoherent scope ceilings."""
        if (
            len({scope.server_id for scope in self.resource_scope})
            != len(self.resource_scope)
            or len(set(self.scopes)) != len(self.scopes)
            or "mcp:read" not in self.scopes
        ):
            raise ValueError("Explicit unique ceiling required")
        return self


@dataclass(frozen=True)
class IssuedRuntimeCredential:
    """A secret delivered once; callers must not log or persist this object."""

    api_key_id: UUID
    runtime_session_id: UUID
    generation: int
    expires_at: datetime
    token: str
    credential_type: str = "restricted_runtime"


def enabled() -> bool:
    """Keep generic restricted runtime issuance and authorization default off."""
    return (
        os.getenv("PRELOOP_RESTRICTED_RUNTIME_CREDENTIALS", "false").lower() == "true"
    )


def _session_key(external_session_id: str) -> str:
    if (
        not isinstance(external_session_id, str)
        or not external_session_id
        or len(external_session_id) > 1024
    ):
        raise RestrictedRuntimeDeniedError("invalid_external_session")
    # High-entropy identity fingerprint, not password hashing.
    return hashlib.sha256(external_session_id.encode()).hexdigest()


def _time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _context_digest(context: dict[str, Any]) -> str:
    """Bind the exact key context without duplicating its whole resource ceiling."""
    try:
        encoded = json.dumps(
            context, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError):
        raise RestrictedRuntimeDeniedError(
            "restricted_runtime_credential_denied"
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def _reference(
    db: Session, account_id: UUID, policy_id: UUID, *, nowait: bool = False
) -> models.SecretReference:
    from preloop.models.crud import crud_secret_reference

    # populate_existing must not discard the caller's pending policy disable.
    db.flush()
    try:
        if nowait:
            # A refused NOWAIT must roll back its savepoint, preserving the
            # caller's transaction for clean refusal handling.
            with db.begin_nested():
                reference = crud_secret_reference.get_for_update(
                    db, secret_id=policy_id, account_id=account_id, nowait=True
                )
        else:
            reference = crud_secret_reference.get_for_update(
                db, secret_id=policy_id, account_id=account_id
            )
    except OperationalError:
        raise RestrictedRuntimeDeniedError("runtime_policy_unavailable") from None
    if reference is None or reference.account_id is None:
        raise RestrictedRuntimeDeniedError("runtime_policy_unavailable")
    return reference


def _policy(
    reference: models.SecretReference, now: datetime
) -> tuple[RuntimePolicy, int]:
    if not enabled():
        raise RestrictedRuntimeDeniedError("restricted_runtime_disabled")
    metadata = reference.meta_data
    if not isinstance(metadata, dict):
        raise RestrictedRuntimeDeniedError("runtime_policy_unavailable")
    generation = metadata.get("generation")
    if type(generation) is not int or generation < 1:
        raise RestrictedRuntimeDeniedError("runtime_policy_unavailable")
    try:
        policy = RuntimePolicy.model_validate(metadata[POLICY_KEY])
    except (KeyError, ValidationError, TypeError, ValueError):
        raise RestrictedRuntimeDeniedError("runtime_policy_unavailable") from None
    if reference.status != "active" or not policy.enabled:
        raise RestrictedRuntimeDeniedError("runtime_policy_disabled")
    if policy.expires_at <= now.timestamp():
        raise RestrictedRuntimeDeniedError("runtime_policy_expired")
    return policy, generation


def _sessions(reference: models.SecretReference) -> dict[str, Any]:
    value = (reference.meta_data or {}).get(SESSIONS_KEY, {})
    if not isinstance(value, dict) or len(value) > MAX_SESSIONS:
        raise RestrictedRuntimeDeniedError("runtime_session_state_unavailable")
    return deepcopy(value)


def _approved_principal(
    db: Session, *, account_id: UUID, policy: RuntimePolicy, for_update: bool = False
) -> tuple[models.User, models.ManagedAgent]:
    from preloop.models.crud import (
        crud_account,
        crud_managed_agent,
        crud_managed_agent_enrollment,
        crud_policy_snapshot,
        crud_user,
    )

    # Callers can reuse a Session with an identity map. Flush their pending
    # changes before refreshing, so a local disable is not discarded and a
    # concurrent committed disable is not hidden by a cached ORM instance.
    db.flush()
    account = crud_account.get(db, id=account_id)
    user = crud_user.get(db, id=policy.user_id, account_id=str(account_id))
    agent = crud_managed_agent.get_for_account(
        db,
        account_id=str(account_id),
        agent_id=str(policy.managed_agent_id),
        for_update=for_update,
    )
    enrollment = crud_managed_agent_enrollment.get_for_agent(
        db,
        account_id=str(account_id),
        agent_id=str(policy.managed_agent_id),
        enrollment_id=str(policy.enrollment_id),
    )
    snapshot = crud_policy_snapshot.get(
        db, id=policy.policy_snapshot_id, account_id=str(account_id)
    )
    for row in (account, user, agent, enrollment, snapshot):
        if row is not None:
            db.refresh(row)
    if (
        account is None
        or not account.is_active
        or user is None
        or not user.is_active
        or agent is None
        or agent.lifecycle_state != "active"
        or str(agent.owner_user_id) != str(policy.user_id)
        or enrollment is None
        or enrollment.status != "validated"
        or snapshot is None
        or not snapshot.is_active
    ):
        raise RestrictedRuntimeDeniedError("runtime_principal_not_approved")
    return user, agent


def _resources(
    db: Session,
    *,
    account_id: UUID,
    granted: list[ResourceScope],
    ceiling: list[ResourceScope],
) -> None:
    from preloop.models.crud import crud_mcp_server

    db.flush()
    allowed = {str(scope.server_id): set(scope.tools) for scope in ceiling}
    for resource in granted:
        server = crud_mcp_server.get(
            db, id=resource.server_id, account_id=str(account_id)
        )
        if server is not None:
            db.refresh(server)
        if (
            str(resource.server_id) not in allowed
            or not set(resource.tools) <= allowed[str(resource.server_id)]
            or server is None
            or server.status != "active"
        ):
            raise RestrictedRuntimeDeniedError("runtime_resource_not_granted")


def exchange(
    db: Session,
    *,
    account_id: UUID,
    policy_id: UUID,
    generation: int,
    external_session_id: str,
    creator_subject: str,
    upstream_expires_at: datetime,
    requested_scopes: list[str] | None = None,
    requested_resources: list[ResourceScope] | None = None,
    now: datetime | None = None,
) -> IssuedRuntimeCredential:
    """Issue once under the policy row lock; caller commits before delivery.

    Args:
        db: Caller-owned transaction. No helper commits before all state exists.
        account_id: Account determined by the trusted adapter's explicit binding.
        policy_id: Account-owned authority reference.
        generation: Generation captured before external verification.
        external_session_id: Verified provider/environment/session identity.
        creator_subject: Verified creator, already approved by the adapter.
        upstream_expires_at: Strict upstream expiration, with no added skew.
        requested_scopes: Optional narrowing of the operator-approved ceiling.
        requested_resources: Optional narrowing of immutable server/tool grants.
        now: Frozen time seam for synthetic regressions.

    Returns:
        One hashed-key credential; its secret cannot be retrieved on retry.

    Raises:
        RestrictedRuntimeDeniedError: Invalid, revoked, expired or repeated authority.
    """
    from preloop.models.crud import crud_api_key, crud_runtime_session

    instant = _time(now or datetime.now(UTC))
    reference = _reference(db, account_id, policy_id)
    policy, current_generation = _policy(reference, instant)
    if type(generation) is not int or generation != current_generation:
        raise RestrictedRuntimeDeniedError("runtime_policy_generation_changed")
    digest = _session_key(external_session_id)
    if (
        not isinstance(creator_subject, str)
        or not creator_subject
        or len(creator_subject) > 512
    ):
        raise RestrictedRuntimeDeniedError("invalid_external_creator")
    sessions = _sessions(reference)
    if digest in sessions:
        raise RestrictedRuntimeDeniedError(
            "runtime_session_already_exchanged_or_revoked"
        )
    if len(sessions) >= MAX_SESSIONS:
        raise RestrictedRuntimeDeniedError("runtime_session_state_capacity_reached")
    scopes = requested_scopes if requested_scopes is not None else policy.scopes
    resources = (
        requested_resources
        if requested_resources is not None
        else policy.resource_scope
    )
    if (
        not isinstance(scopes, list)
        or not scopes
        or any(not isinstance(value, str) for value in scopes)
        or not set(scopes) <= set(policy.scopes)
        or len(set(scopes)) != len(scopes)
        or "mcp:read" not in scopes
    ):
        raise RestrictedRuntimeDeniedError("runtime_scope_not_granted")
    if not resources or len({r.server_id for r in resources}) != len(resources):
        raise RestrictedRuntimeDeniedError("runtime_resource_not_granted")
    expires_at = min(
        _time(upstream_expires_at),
        datetime.fromtimestamp(policy.expires_at, UTC),
        instant + timedelta(seconds=min(policy.grant_ttl_seconds, MAX_TTL_SECONDS)),
    )
    if expires_at <= instant:
        raise RestrictedRuntimeDeniedError("runtime_upstream_expired")
    user, agent = _approved_principal(
        db, account_id=account_id, policy=policy, for_update=True
    )
    _resources(
        db, account_id=account_id, granted=resources, ceiling=policy.resource_scope
    )
    runtime = crud_runtime_session.upsert_by_source(
        db,
        account_id=account_id,
        session_source_type="restricted_runtime",
        session_source_id=f"{policy_id}:{digest}",
        runtime_principal_type=agent.session_source_type,
        runtime_principal_id=agent.session_source_id,
        started_at=instant,
        last_activity_at=instant,
        reopen_if_ended=False,
    )
    if runtime.ended_at is not None:
        raise RestrictedRuntimeDeniedError("runtime_session_ended")
    context = {
        "runtime_session_id": str(runtime.id),
        "managed_agent_id": str(agent.id),
        "restricted_runtime": {
            "policy_id": str(policy_id),
            "generation": current_generation,
            "session_fingerprint": digest,
            "creator_subject": creator_subject,
            "enrollment_id": str(policy.enrollment_id),
            "policy_snapshot_id": str(policy.policy_snapshot_id),
            "resource_scope": [r.model_dump(mode="json") for r in resources],
        },
        "runtime_principal": {
            "type": agent.session_source_type,
            "id": agent.session_source_id,
        },
    }
    key, token = crud_api_key.create_runtime_key(
        db,
        name=f"Restricted runtime {policy_id} {digest[:16]}",
        account_id=account_id,
        user_id=user.id,
        scopes=list(scopes),
        expires_at=expires_at,
        context_data=context,
        restricted_runtime=True,
        commit=False,
    )
    sessions[digest] = {
        "status": "issued",
        "api_key_id": str(key.id),
        "context_digest": _context_digest(context),
        "scopes": list(scopes),
        "expires_at": expires_at.timestamp(),
    }
    reference.meta_data = {**(reference.meta_data or {}), SESSIONS_KEY: sessions}
    db.add(reference)
    db.flush()
    return IssuedRuntimeCredential(
        key.id, runtime.id, current_generation, expires_at, token
    )


def revoke(
    db: Session, *, account_id: UUID, policy_id: UUID, external_session_id: str
) -> None:
    """Persist a durable tombstone, then disable its key in the same transaction."""
    reference = _reference(db, account_id, policy_id)
    sessions = _sessions(reference)
    digest = _session_key(external_session_id)
    existing = sessions.get(digest)
    if digest not in sessions and len(sessions) >= MAX_SESSIONS:
        raise RestrictedRuntimeDeniedError("runtime_session_state_capacity_reached")
    sessions[digest] = {"status": "revoked"}
    reference.meta_data = {**(reference.meta_data or {}), SESSIONS_KEY: sessions}
    db.add(reference)
    db.flush()
    if isinstance(existing, dict) and existing.get("api_key_id"):
        key = (
            db.query(models.ApiKey)
            .filter(
                models.ApiKey.id == existing["api_key_id"],
                models.ApiKey.account_id == account_id,
                models.ApiKey.credential_type == "restricted_runtime",
            )
            .populate_existing()
            .one_or_none()
        )
        if key is not None:
            key.is_active = False
            db.add(key)
            db.flush()


def _authorize(
    db: Session,
    *,
    account_id: UUID,
    api_key_id: UUID,
    scope: Literal["mcp:read", "mcp:write"] = "mcp:read",
    server_id: UUID | None = None,
    upstream_tool: str | None = None,
    now: datetime | None = None,
) -> tuple[models.User, list[ResourceScope]]:
    """Read fresh key, policy, principal and session state at each supported boundary."""
    from preloop.models.crud import crud_runtime_session

    instant = _time(now or datetime.now(UTC))
    # Preserve an in-transaction key deactivation before refreshing authority.
    db.flush()
    key = (
        db.query(models.ApiKey)
        .filter(
            models.ApiKey.id == api_key_id,
            models.ApiKey.account_id == account_id,
        )
        .populate_existing()
        .one_or_none()
    )
    if (
        key is None
        or key.credential_type != "restricted_runtime"
        or key.credential_version != 1
        or key.ci_principal_id is not None
        or key.ci_actions is not None
        or not key.is_active
        or key.expires_at is None
        or _time(key.expires_at) <= instant
    ):
        raise RestrictedRuntimeDeniedError("restricted_runtime_credential_denied")
    context = key.context_data
    if not isinstance(context, dict):
        raise RestrictedRuntimeDeniedError("restricted_runtime_credential_denied")
    try:
        authority = context["restricted_runtime"]
        if not isinstance(authority, dict):
            raise TypeError("invalid authority")
        policy_id = UUID(authority["policy_id"])
        runtime_id = UUID(context["runtime_session_id"])
    except (TypeError, KeyError, ValueError, AttributeError):
        raise RestrictedRuntimeDeniedError(
            "restricted_runtime_credential_denied"
        ) from None
    reference = _reference(db, account_id, policy_id, nowait=True)
    policy, generation = _policy(reference, instant)
    fingerprint = authority.get("session_fingerprint")
    if not isinstance(fingerprint, str):
        raise RestrictedRuntimeDeniedError("restricted_runtime_credential_denied")
    record = _sessions(reference).get(fingerprint)
    if (
        not isinstance(record, dict)
        or record.get("status") != "issued"
        or record.get("api_key_id") != str(key.id)
        or record.get("context_digest") != _context_digest(context)
        or record.get("scopes") != key.scopes
        or record.get("expires_at") != _time(key.expires_at).timestamp()
        or type(authority.get("generation")) is not int
        or authority["generation"] != generation
        or context.get("managed_agent_id") != str(policy.managed_agent_id)
        or authority.get("enrollment_id") != str(policy.enrollment_id)
        or authority.get("policy_snapshot_id") != str(policy.policy_snapshot_id)
        or str(key.user_id) != str(policy.user_id)
        or not isinstance(key.scopes, list)
        or any(not isinstance(value, str) for value in key.scopes)
        or len(set(key.scopes)) != len(key.scopes)
        or scope not in key.scopes
        or not set(key.scopes) <= set(policy.scopes)
    ):
        raise RestrictedRuntimeDeniedError("restricted_runtime_authority_changed")
    try:
        resources = [
            ResourceScope.model_validate(r) for r in authority["resource_scope"]
        ]
    except (KeyError, TypeError, ValidationError, ValueError):
        raise RestrictedRuntimeDeniedError(
            "restricted_runtime_credential_denied"
        ) from None
    _resources(
        db, account_id=account_id, granted=resources, ceiling=policy.resource_scope
    )
    user, _ = _approved_principal(db, account_id=account_id, policy=policy)
    runtime = crud_runtime_session.get_account_session(
        db, account_id=str(account_id), runtime_session_id=str(runtime_id)
    )
    if runtime is not None:
        db.refresh(runtime)
    if runtime is None or runtime.ended_at is not None:
        raise RestrictedRuntimeDeniedError("runtime_session_ended")
    if (
        scope == "mcp:write" or server_id is not None or upstream_tool is not None
    ) and (
        server_id is None
        or not upstream_tool
        or not any(
            r.server_id == server_id and upstream_tool in r.tools for r in resources
        )
    ):
        raise RestrictedRuntimeDeniedError("runtime_resource_not_granted")
    return user, resources


def authorize(
    db: Session,
    *,
    account_id: UUID,
    api_key_id: UUID,
    scope: Literal["mcp:read", "mcp:write"] = "mcp:read",
    server_id: UUID | None = None,
    upstream_tool: str | None = None,
    now: datetime | None = None,
) -> models.User:
    """Authorize one supported boundary with fresh authority and exact resources."""
    user, _ = _authorize(
        db,
        account_id=account_id,
        api_key_id=api_key_id,
        scope=scope,
        server_id=server_id,
        upstream_tool=upstream_tool,
        now=now,
    )
    return user


def authorized_resources(
    db: Session, *, account_id: UUID, api_key_id: UUID, now: datetime | None = None
) -> list[ResourceScope]:
    """Project the current listing grants under one policy lock and state check.

    Callers must filter freshly resolved tool owners against these immutable
    server IDs and original tool names in the same transaction. The projection
    must never be cached or substituted for a later invocation authorization.
    """
    _, resources = _authorize(
        db, account_id=account_id, api_key_id=api_key_id, scope="mcp:read", now=now
    )
    return resources
