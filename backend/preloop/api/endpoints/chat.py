"""Verified chat connection management and authenticated provider ingress."""

from __future__ import annotations
import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from anyio import from_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.auth.permissions import has_permission
from preloop.models import models
from preloop.models.crud import crud_chat, crud_account
from preloop.models.db.session import get_db_session
from preloop.services.chat_providers import (
    ChatProviderConfig,
    ChatProviderError,
    MAX_EVENT_BYTES,
    validate_mattermost_url,
    discover_mattermost_bot_user_id,
    verify_ingress,
)
from preloop.utils.encryption import encrypt_value, decrypt_value
from preloop.utils.permissions import require_permission, ensure_permission_in_oss

router = APIRouter(prefix="/chat", tags=["Chat"])
MANAGE_PERMISSION = "manage_policies"


class ConnectionCreate(BaseModel):
    provider: Literal["slack", "mattermost", "discord"]
    workspace_id: str = Field(min_length=1, max_length=256)
    name: str = Field(default="", max_length=120)
    verification_secret: str = Field(min_length=1, max_length=1024)
    bot_token: str = Field(min_length=1, max_length=4096)
    bot_user_id: str = Field(default="", max_length=256)
    base_url: str = Field(default="", max_length=2048)

    @model_validator(mode="after")
    def default_connection_name(self) -> ConnectionCreate:
        """Allow clients to omit the display name or submit a blank input."""
        provider_name = {
            "slack": "Slack",
            "mattermost": "Mattermost",
            "discord": "Discord",
        }
        self.name = self.name.strip() or f"Preloop {provider_name[self.provider]} bot"
        return self


class ConnectionUpdate(BaseModel):
    enabled: bool | None = None
    default_flow_id: UUID | None = None


def provider_config(connection: models.ChatConnection) -> ChatProviderConfig:
    credentials = json.loads(decrypt_value(connection.credentials_encrypted))
    return ChatProviderConfig(
        provider=connection.provider,
        workspace_id=connection.workspace_id,
        **credentials,
    )


def require_active_account(db: Session, user: models.User) -> None:
    account = crud_account.get(db, id=user.account_id)
    if account is None or not account.is_active:
        raise HTTPException(403, "Account is unavailable")


def get_connection(
    db: Session, connection_id: UUID, user: models.User
) -> models.ChatConnection:
    require_active_account(db, user)
    connection = crud_chat.connections.get(
        db, connection_id, account_id=str(user.account_id)
    )
    if connection is None:
        raise HTTPException(404, "Connection not found")
    return connection


def public_connection(
    db: Session,
    connection: models.ChatConnection,
    user: models.User,
    state: tuple[models.ChatIdentity | None, models.ChatListener | None] | None = None,
) -> dict[str, Any]:
    if state is None:
        identity = crud_chat.identity(db, connection.id, user_id=user.id)
        health = crud_chat.listeners.get_multi(db, connection_id=connection.id, limit=1)
        worker = health[0] if health else None
    else:
        identity, worker = state
    fresh = bool(
        worker and worker.lease_until and worker.lease_until > datetime.utcnow()
    )
    ready = bool(
        worker
        and worker.lease_until
        and worker.lease_until > datetime.utcnow()
        and worker.status in {"connected", "worker_ready"}
    )
    return {
        "id": str(connection.id),
        "provider": connection.provider,
        "workspace_id": connection.workspace_id,
        "name": connection.name,
        "enabled": connection.enabled,
        "default_flow_id": str(connection.default_flow_id)
        if connection.default_flow_id
        else None,
        "configured": True,
        "worker_ready": ready and connection.enabled,
        "worker_status": worker.status if fresh else "waiting",
        "callback_seen": bool(worker and worker.last_event_at),
        "transport": "direct_message"
        if connection.provider == "mattermost"
        and not provider_config(connection).verification_secret
        else "events",
        "linked": identity is not None,
        "external_user_id": identity.external_user_id if identity else None,
        "ingress_url": f"/api/v1/chat/ingress/{connection.id}",
    }


@router.get("/connections")
def list_connections(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    require_active_account(db, current_user)
    connections = crud_chat.connections.get_multi(
        db, account_id=str(current_user.account_id), limit=100
    )
    states = crud_chat.connection_states(
        db, [item.id for item in connections], current_user.id
    )
    return {
        "connections": [
            public_connection(db, item, current_user, states[item.id])
            for item in connections
        ],
        "can_manage": has_permission(current_user, MANAGE_PERMISSION, db),
    }


@router.post("/connections", status_code=201)
@require_permission(MANAGE_PERMISSION)
def create_connection(
    payload: ConnectionCreate,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    require_active_account(db, current_user)
    ensure_permission_in_oss(db, current_user, MANAGE_PERMISSION)
    if not has_permission(current_user, MANAGE_PERMISSION, db):
        raise HTTPException(403, "Connection management permission required")
    if payload.provider == "mattermost":
        try:
            validate_mattermost_url(payload.base_url)
            if not payload.bot_user_id:
                payload.bot_user_id = from_thread.run(
                    discover_mattermost_bot_user_id, payload.base_url, payload.bot_token
                )
        except ChatProviderError as exc:
            raise HTTPException(422, str(exc)) from exc
    if payload.provider == "discord":
        try:
            if len(bytes.fromhex(payload.verification_secret)) != 32:
                raise ValueError()
        except ValueError as exc:
            raise HTTPException(
                422, "Discord requires a 32-byte hexadecimal public key"
            ) from exc
    credentials = {
        key: getattr(payload, key)
        for key in ("verification_secret", "bot_token", "bot_user_id", "base_url")
    }
    connection = crud_chat.connections.create(
        db,
        obj_in={
            "account_id": current_user.account_id,
            "provider": payload.provider,
            "workspace_id": payload.workspace_id,
            "name": payload.name,
            "credentials_encrypted": encrypt_value(json.dumps(credentials)),
        },
    )
    return public_connection(db, connection, current_user)


@router.patch("/connections/{connection_id}")
@require_permission(MANAGE_PERMISSION)
def update_connection(
    connection_id: UUID,
    payload: ConnectionUpdate,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    require_active_account(db, current_user)
    ensure_permission_in_oss(db, current_user, MANAGE_PERMISSION)
    if not has_permission(current_user, MANAGE_PERMISSION, db):
        raise HTTPException(403, "Connection management permission required")
    connection = get_connection(db, connection_id, current_user)
    if payload.default_flow_id is not None:
        from preloop.models.crud import crud_flow

        flow = crud_flow.get(
            db, id=payload.default_flow_id, account_id=current_user.account_id
        )
        from preloop.services.read_only_review import read_only_review

        if (
            flow is None
            or not flow.chat_alias
            or not flow.is_enabled
            or read_only_review(flow.git_clone_config)
        ):
            raise HTTPException(
                422, "Default flow must be enabled and opted in to chat"
            )
    changes = payload.model_dump(exclude_unset=True)
    if "enabled" in changes and changes["enabled"] is None:
        raise HTTPException(422, "Enabled must be a boolean")
    connection = crud_chat.connections.update(db, db_obj=connection, obj_in=changes)
    if not connection.enabled:
        crud_chat.close_bindings(db, connection.id)
    return public_connection(db, connection, current_user)


@router.post("/connections/{connection_id}/link-code")
def create_link_code(
    connection_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, str]:
    connection = get_connection(db, connection_id, current_user)
    if not connection.enabled:
        raise HTTPException(409, "Connection is disabled")
    crud_chat.invalidate_codes(db, connection.id, current_user.id)
    code = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    crud_chat.codes.create(
        db,
        obj_in={
            "account_id": current_user.account_id,
            "connection_id": connection.id,
            "user_id": current_user.id,
            "digest": hashlib.sha256(code.encode()).hexdigest(),
            "expires_at": expires_at,
        },
    )
    return {
        "code": code,
        "expires_at": expires_at.isoformat() + "Z",
        "instruction": {
            "slack": "Send a plain direct message: link CODE",
            "mattermost": "Send a private direct message to the bot: link CODE"
            if not provider_config(connection).verification_secret
            else "Run /preloop /link CODE; the command response is private.",
            "discord": "Run /preloop with message: /link CODE; the command response is private.",
        }[connection.provider],
    }


@router.delete("/connections/{connection_id}/identity", status_code=204)
def unlink_identity(
    connection_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> None:
    connection = get_connection(db, connection_id, current_user)
    crud_chat.invalidate_codes(db, connection.id, current_user.id)
    identity = crud_chat.identity(db, connection.id, user_id=current_user.id)
    if identity:
        crud_chat.close_bindings(
            db, connection.id, external_user_id=identity.external_user_id
        )
        crud_chat.identities.delete(db, id=identity.id)


@router.get("/connections/{connection_id}/deliveries")
def list_deliveries(
    connection_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, Any]:
    connection = get_connection(db, connection_id, current_user)
    rows = crud_chat.recent_deliveries(
        db,
        account_id=current_user.account_id,
        connection_id=connection.id,
        user_id=current_user.id,
    )
    return {
        "deliveries": [
            {
                "id": str(row.id),
                "status": row.status,
                "created_at": row.created_at.isoformat() + "Z",
                "last_error": row.last_error,
            }
            for row in rows
        ]
    }


async def _read_ingress_body(request: Request) -> bytes:
    """Read a provider body, refusing it once it passes the event limit."""
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_EVENT_BYTES:
            raise HTTPException(413, "Event is too large")
    return bytes(raw)


@router.post("/ingress/{connection_id}")
def ingest(
    connection_id: UUID,
    request: Request,
    db: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Accept one verified provider event.

    The handler is synchronous so the session checkout stays on the
    threadpool. The request stream is read on the event loop.
    """
    connection = crud_chat.connections.get(db, connection_id)
    if connection is None:
        draft = crud_chat.drafts.get(db, connection_id)
        if draft and not draft.consumed_at and draft.expires_at > datetime.utcnow():
            account = crud_account.get(db, id=draft.account_id)
            if not account or not account.is_active:
                raise HTTPException(404, "Connection not found")
            state = json.loads(decrypt_value(draft.state_encrypted))
            from preloop.api.endpoints.chat_setup import draft_config

            raw = from_thread.run(_read_ingress_body, request)
            try:
                ingress = verify_ingress(
                    draft_config(draft.provider, state), raw, request.headers
                )
                # A draft accepts provider verification challenges only.
                if (
                    draft.provider == "slack" and "challenge" in ingress.acknowledgement
                ) or (
                    draft.provider == "discord"
                    and ingress.acknowledgement == {"type": 1}
                ):
                    return ingress.acknowledgement
            except (ChatProviderError, ValueError):
                pass
        raise HTTPException(404, "Connection not found")
    if not connection.enabled:
        raise HTTPException(404, "Connection not found")
    account = crud_account.get(db, id=connection.account_id)
    if account is None or not account.is_active:
        raise HTTPException(404, "Connection not found")
    # Bound the body while streaming, rather than allocating an arbitrary body.
    raw = from_thread.run(_read_ingress_body, request)
    try:
        ingress = verify_ingress(
            provider_config(connection), bytes(raw), request.headers
        )
    except (ChatProviderError, ValueError) as exc:
        raise HTTPException(401, "Invalid provider request") from exc
    crud_chat.record_ingress(db, connection.id)
    if ingress.event:
        event = ingress.event
        text = event.text
        if connection.provider == "slack":
            bot_id = provider_config(connection).bot_user_id
            mention = f"<@{bot_id}>"
            if bot_id and text.startswith(mention):
                text = text[len(mention) :].strip()
        if connection.provider == "slack":
            verb = text.split(maxsplit=1)[0] if text else ""
            if text.strip() in {"end", "status"}:
                text = "/" + text.strip()
            elif verb in {
                "link",
                "note",
                "message",
                "approve",
                "deny",
                "ask",
                "help",
            }:
                text = "/" + text
        payload = {
            "text": text,
            "private": event.is_private,
            "channel_id": event.channel_id,
            "thread_id": event.thread_id,
        }
        actor = crud_chat.principal(db, connection, event.user_id)
        if actor is not None:
            payload["actor_id"] = str(actor.id)
        if text.startswith("/link "):
            payload["link_digest"] = hashlib.sha256(
                text.split(maxsplit=1)[1].strip().encode()
            ).hexdigest()
            payload["text"] = "/link"
        crud_chat.receive(
            db,
            connection=connection,
            event_id=event.event_id,
            external_user_id=event.user_id,
            payload=payload,
        )
    return ingress.acknowledgement
