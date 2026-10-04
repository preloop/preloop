"""Run with python -m preloop.services.chat_worker; no in-process-only jobs."""

from __future__ import annotations
import asyncio
import hashlib
import logging
import os
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models.crud import crud_chat
from preloop.models.db.session import get_session_factory
from preloop.services.chat_assistant import ChatBroker
from preloop.services.chat_providers import (
    ChatProviderError,
    send_private_reply,
    external_command,
)
from preloop.api.endpoints.chat import provider_config
from preloop.api.auth.permissions import has_permission

logger = logging.getLogger(__name__)


async def process_one(db: Session, app: Any) -> bool:
    """Claim one durable item; uncertain writes require operator inspection."""
    row = crud_chat.claim(db)
    if row is None:
        return False
    connection = crud_chat.connections.get(
        db, row.connection_id, account_id=str(row.account_id)
    )
    if connection is None or not connection.enabled:
        crud_chat.transition(db, row, "cancelled", last_error="Connection disabled")
        return True
    text = str(row.payload.get("text", ""))
    try:
        if row.reply is None:
            if row.payload.get("link_digest") or text.startswith("/link "):
                # Linking proof itself must stay private. Slack supports DM;
                # Discord/Mattermost slash acknowledgments are ephemeral.
                if connection.provider == "slack" and not row.payload.get("private"):
                    reply = "Send the linking code in a direct message to the bot."
                    crud_chat.transition(
                        db,
                        row,
                        "processing",
                        payload={**row.payload, "public_help": True},
                    )
                else:
                    identity = crud_chat.consume_code(
                        db,
                        connection,
                        row.external_user_id,
                        "",
                        digest=row.payload.get("link_digest")
                        or hashlib.sha256(
                            text.split(maxsplit=1)[1].strip().encode()
                        ).hexdigest(),
                    )
                    crud_chat.transition(
                        db, row, "processing", user_id=identity.user_id
                    )
                    reply = "Your Preloop identity is linked."
            else:
                user = crud_chat.principal(db, connection, row.external_user_id)
                if user is None:
                    if row.payload.get("actor_id"):
                        crud_chat.transition(
                            db, row, "cancelled", last_error="Original actor revoked"
                        )
                        return True
                    # An unauthenticated actor may receive only this fixed help.
                    reply = "Link your identity from Preloop Settings → Chat connections, then use the provider-specific linking command shown there."
                    crud_chat.transition(
                        db,
                        row,
                        "processing",
                        payload={**row.payload, "public_help": True},
                    )
                else:
                    original_actor = row.payload.get("actor_id")
                    if original_actor and original_actor != str(user.id):
                        crud_chat.transition(
                            db, row, "cancelled", last_error="Identity was relinked"
                        )
                        return True
                    crud_chat.transition(
                        db,
                        row,
                        "processing",
                        user_id=user.id,
                        payload={**row.payload, "actor_id": str(user.id)},
                    )
                    broker = ChatBroker(db, connection, row.external_user_id, app)
                    if row.payload.get("approval_request_id"):
                        request_id = UUID(row.payload["approval_request_id"])
                        if not crud_chat.eligible_approver(db, user, request_id):
                            crud_chat.transition(
                                db,
                                row,
                                "cancelled",
                                last_error="Approver no longer eligible",
                            )
                            return True
                        if not has_permission(user, "decide_approvals", db):
                            crud_chat.transition(
                                db,
                                row,
                                "cancelled",
                                last_error="Approver permission revoked",
                            )
                            return True
                        crud_chat.transition(
                            db,
                            row,
                            "processing",
                            payload={
                                **row.payload,
                                "required_permissions": [
                                    "view_approvals",
                                    "decide_approvals",
                                ],
                                "resource_proofs": [
                                    {
                                        "kind": "approval_request",
                                        "id": str(request_id),
                                        "permission": "view_approvals",
                                    }
                                ],
                            },
                        )
                        await broker.request(
                            "GET",
                            f"/api/v1/approval-requests/{request_id}",
                            "view_approvals",
                        )
                        approve = external_command(
                            connection.provider, f"/approve {request_id}"
                        )
                        deny = external_command(
                            connection.provider, f"/deny {request_id}"
                        )
                        reply = f"Preloop approval {request_id} needs your review. Use {approve} or {deny}. Review full details in the dashboard before deciding."
                    elif text.startswith("/"):
                        # Commit intent before the POST. A crash is observable.
                        permission = (
                            "decide_approvals"
                            if text.split()[0] in {"/approve", "/deny"}
                            else "control_managed_agent"
                        )
                        crud_chat.transition(
                            db,
                            row,
                            "acting",
                            payload={**row.payload, "required_permission": permission},
                        )
                        parts = text.split(maxsplit=3)
                        resources = []
                        if parts[0] == "/note" and len(parts) >= 3:
                            resources.append(
                                {
                                    "kind": "runtime_session",
                                    "id": str(UUID(parts[1])),
                                    "permission": permission,
                                }
                            )
                        elif parts[0] == "/message" and len(parts) == 4:
                            resources.extend(
                                [
                                    {
                                        "kind": "managed_agent",
                                        "id": str(UUID(parts[1])),
                                        "permission": permission,
                                    },
                                    {
                                        "kind": "runtime_session",
                                        "id": str(UUID(parts[2])),
                                        "permission": permission,
                                    },
                                ]
                            )
                        elif parts[0] in {"/approve", "/deny"} and len(parts) == 2:
                            resources.append(
                                {
                                    "kind": "approval_request",
                                    "id": str(UUID(parts[1])),
                                    "permission": permission,
                                }
                            )
                        crud_chat.transition(
                            db,
                            row,
                            "acting",
                            payload={**row.payload, "resource_proofs": resources},
                        )
                        reply = await asyncio.wait_for(
                            broker.command(text, str(row.id)), timeout=90
                        )
                    else:
                        reply = await asyncio.wait_for(broker.answer(text), timeout=90)
                        crud_chat.transition(
                            db,
                            row,
                            "processing",
                            payload={
                                **row.payload,
                                "read_proofs": broker.read_proofs,
                                "read_windows": broker.read_windows,
                                "required_permission": "view_ai_models",
                            },
                        )
            crud_chat.transition(db, row, "reply_ready", reply=reply)
        # Recheck revocation immediately before disclosing any stored response.
        if not row.payload.get("public_help"):
            actor_id = row.payload.get("actor_id")
            if not actor_id or row.user_id is None or str(row.user_id) != actor_id:
                crud_chat.transition(
                    db, row, "cancelled", last_error="Original actor is unavailable"
                )
                return True
        if row.user_id is not None:
            user = crud_chat.principal(db, connection, row.external_user_id)
            if user is None or user.id != row.user_id:
                crud_chat.transition(
                    db, row, "cancelled", last_error="Identity revoked"
                )
                return True
            if row.payload.get(
                "approval_request_id"
            ) and not crud_chat.eligible_approver(
                db, user, UUID(row.payload["approval_request_id"])
            ):
                crud_chat.transition(
                    db, row, "cancelled", last_error="Approver no longer eligible"
                )
                return True
            permission = row.payload.get("required_permission")
            if permission and not has_permission(user, permission, db):
                crud_chat.transition(
                    db, row, "cancelled", last_error="Permission revoked"
                )
                return True
            for required in row.payload.get("required_permissions", []):
                if not has_permission(user, required, db):
                    crud_chat.transition(
                        db, row, "cancelled", last_error="Permission revoked"
                    )
                    return True
            for resource in row.payload.get("resource_proofs", []):
                ChatBroker(
                    db, connection, row.external_user_id, app
                ).authorize_resource(
                    resource["kind"], resource["id"], resource["permission"]
                )
            if row.payload.get("read_proofs"):
                broker = ChatBroker(db, connection, row.external_user_id, app)
                broker.read_windows = row.payload.get("read_windows", {})
                await broker.validate_reads(row.payload["read_proofs"])
        crud_chat.transition(db, row, "sending")
        async with httpx.AsyncClient() as client:
            message_id = await send_private_reply(
                provider_config(connection),
                row.external_user_id,
                row.reply or "No answer available.",
                delivery_id=str(row.id),
                client=client,
            )
        crud_chat.transition(
            db, row, "delivered", provider_message_id=message_id, last_error=None
        )
    except HTTPException as exc:
        crud_chat.transition(
            db,
            row,
            "failed",
            last_error=f"Action denied or unavailable ({exc.status_code})",
        )
    except (ValueError, ChatProviderError) as exc:
        # Never store exception text that might include credentials/provider data.
        crud_chat.transition(
            db,
            row,
            "failed",
            last_error="Invalid request or provider refused operation",
        )
    except (httpx.HTTPError, asyncio.TimeoutError):
        if row.status in {"acting", "sending"}:
            crud_chat.transition(
                db,
                row,
                "uncertain",
                last_error="Operation outcome is uncertain; inspect before retrying",
            )
        else:
            crud_chat.transition(
                db,
                row,
                "failed" if row.attempts >= 3 else "pending",
                next_attempt_at=datetime.utcnow()
                + timedelta(seconds=30 * row.attempts),
                last_error="Service unavailable",
            )
    return True


def enqueue_approval_notifications(
    account_id: UUID, request_id: UUID, user_ids: list[UUID]
) -> int:
    """Hook called by existing approval notification service after recipients resolve."""
    count = 0
    with get_session_factory()() as db:
        for user_id in set(user_ids):
            identities = crud_chat.identities.get_multi(
                db, account_id=str(account_id), user_id=user_id, limit=100
            )
            for identity in identities:
                connection = crud_chat.connections.get(
                    db, identity.connection_id, account_id=str(account_id)
                )
                if connection is None or not connection.enabled:
                    continue
                user = crud_chat.principal(db, connection, identity.external_user_id)
                if (
                    user is None
                    or not has_permission(user, "view_approvals", db)
                    or not has_permission(user, "decide_approvals", db)
                    or not crud_chat.eligible_approver(db, user, request_id)
                ):
                    continue
                row = crud_chat.receive(
                    db,
                    connection=connection,
                    event_id=f"approval:{request_id}:{user_id}",
                    external_user_id=identity.external_user_id,
                    payload={
                        "approval_request_id": str(request_id),
                        "actor_id": str(user_id),
                    },
                )
                crud_chat.work.update(db, db_obj=row, obj_in={"user_id": user_id})
                count += 1
    return count


async def run() -> None:
    """Initialize governance and NATS without API schedulers, then consume jobs."""
    from preloop.api.app import create_app

    # The gateway lifespan initializes all request-governance plugin hooks
    # fail-closed and connects NATS for Agent Control, but skips API workers.
    os.environ["PRELOOP_SERVICE_ROLE"] = "chat"
    app = create_app()
    async with app.router.lifespan_context(app):
        while True:
            try:
                with get_session_factory()() as db:
                    worked = await process_one(db, app)
                if not worked:
                    await asyncio.sleep(2)
            except Exception:
                logger.exception("Chat worker failed; durable state retained")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run())
