"""Verified chat connection management and authenticated provider ingress."""

from __future__ import annotations
import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
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
    verify_ingress,
)
from preloop.utils.encryption import encrypt_value, decrypt_value
from preloop.utils.permissions import require_permission, ensure_permission_in_oss

router = APIRouter(prefix="/chat", tags=["Chat"])
MANAGE_PERMISSION = "manage_policies"


class ConnectionCreate(BaseModel):
    provider: Literal["slack", "mattermost", "discord"]
    workspace_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=120)
    verification_secret: str = Field(min_length=1, max_length=1024)
    bot_token: str = Field(min_length=1, max_length=4096)
    bot_user_id: str = Field(default="", max_length=256)
    base_url: str = Field(default="", max_length=2048)


class ConnectionUpdate(BaseModel):
    enabled: bool


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
    db: Session, connection: models.ChatConnection, user: models.User
) -> dict[str, Any]:
    identity = crud_chat.identity(db, connection.id, user_id=user.id)
    return {
        "id": str(connection.id),
        "provider": connection.provider,
        "workspace_id": connection.workspace_id,
        "name": connection.name,
        "enabled": connection.enabled,
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
    return {
        "connections": [
            public_connection(db, item, current_user) for item in connections
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
                raise ChatProviderError("Mattermost requires the bot user ID")
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
    connection = crud_chat.connections.update(
        db, db_obj=connection, obj_in=payload.model_dump()
    )
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
            "mattermost": "Run /preloop /link CODE; the command response is private.",
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


@router.post("/ingress/{connection_id}")
async def ingest(
    connection_id: UUID, request: Request, db: Session = Depends(get_db_session)
) -> dict[str, Any]:
    connection = crud_chat.connections.get(db, connection_id)
    if connection is None or not connection.enabled:
        raise HTTPException(404, "Connection not found")
    account = crud_account.get(db, id=connection.account_id)
    if account is None or not account.is_active:
        raise HTTPException(404, "Connection not found")
    # Bound the body while streaming, rather than allocating an arbitrary body.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_EVENT_BYTES:
            raise HTTPException(413, "Event is too large")
    try:
        ingress = verify_ingress(
            provider_config(connection), bytes(raw), request.headers
        )
    except (ChatProviderError, ValueError) as exc:
        raise HTTPException(401, "Invalid provider request") from exc
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
            if verb in {"link", "note", "message", "approve", "deny"}:
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
