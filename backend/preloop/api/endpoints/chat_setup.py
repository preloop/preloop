"""Management-only expiring setup and explicit provider installation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode
from uuid import UUID

import httpx
from anyio import from_thread
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.bounded_route import BoundedChatRoute
from preloop.api.endpoints.chat import (
    MANAGE_PERMISSION,
    public_connection,
    require_active_account,
)
from preloop.api.auth.permissions import has_permission
from preloop.models import models
from preloop.models.crud import crud_chat
from preloop.models.db.session import get_db_session
from preloop.services.chat_providers import ChatProviderConfig, ChatProviderError
from preloop.services.chat_setup import discover, install_discord, slack_manifest
from preloop.utils.encryption import encrypt_value, decrypt_value
from preloop.utils.permissions import ensure_permission_in_oss

router = APIRouter(prefix="/chat/setup", tags=["Chat"], route_class=BoundedChatRoute)


class SetupStart(BaseModel):
    provider: Literal["slack", "mattermost", "discord"]
    bot_token: str = Field(default="", max_length=4096)
    verification_secret: str = Field(default="", max_length=1024)
    base_url: str = Field(default="", max_length=2048)
    team_hint: str = Field(default="", max_length=2048)


class SetupFinish(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=256)
    name: str = Field(default="", max_length=120)
    replace_callback: bool = False


def manager(
    response: Response,
    db: Session = Depends(get_db_session),
    user: models.User = Depends(get_current_active_user),
) -> models.User:
    response.headers["Cache-Control"] = "no-store"
    require_active_account(db, user)
    ensure_permission_in_oss(db, user, MANAGE_PERMISSION)
    if not has_permission(user, MANAGE_PERMISSION, db):
        raise HTTPException(403, "Connection management permission required")
    crud_chat.purge_expired_drafts(db)
    return user


def callback(draft_id: UUID) -> str:
    # Fixed deployment origin; never derive callback from an untrusted Host header.
    from preloop.config import settings

    origin = str(settings.preloop_url).rstrip("/")
    return origin + f"/api/v1/chat/ingress/{draft_id}"


def draft_config(
    provider: str, state: dict[str, Any], workspace_id: str = ""
) -> ChatProviderConfig:
    return ChatProviderConfig(
        provider=provider,
        workspace_id=workspace_id,
        **{
            key: state.get(key, "")
            for key in ("bot_token", "verification_secret", "base_url", "bot_user_id")
        },
    )


def get_draft(db: Session, draft_id: UUID, user: models.User) -> models.ChatSetupDraft:
    draft = crud_chat.setup_draft(db, draft_id, user.account_id, user.id)
    if draft is None:
        raise HTTPException(404, "Setup expired or unavailable; start again")
    return draft


def public_draft(draft: models.ChatSetupDraft) -> dict[str, Any]:
    state = json.loads(decrypt_value(draft.state_encrypted))
    info = state.get("discovery", {})
    return {
        "id": str(draft.id),
        "provider": draft.provider,
        "expires_at": draft.expires_at.isoformat() + "Z",
        "callback_url": callback(draft.id),
        "name": info.get("name", ""),
        "bot_username": info.get("bot_username", ""),
        "workspaces": info.get("workspaces", []),
        "install_url": info.get("install_url"),
        "callback_conflict": bool(
            info.get("interactions_endpoint_url")
            and info["interactions_endpoint_url"] != callback(draft.id)
        ),
        "manifest": slack_manifest(callback(draft.id))
        if draft.provider == "slack"
        else None,
        "signing_secret_required": draft.provider == "slack"
        and not state.get("verification_secret"),
    }


def oauth_configured() -> bool:
    from preloop.config import settings

    redirect = os.getenv("CHAT_SLACK_REDIRECT_URI", "")
    expected = (
        str(settings.preloop_url).rstrip("/") + "/api/v1/chat/setup/slack/callback"
    )
    return (
        redirect.startswith("https://")
        and redirect == expected
        and all(
            os.getenv(key)
            for key in (
                "CHAT_SLACK_CLIENT_ID",
                "CHAT_SLACK_CLIENT_SECRET",
                "CHAT_SLACK_SIGNING_SECRET",
            )
        )
    )


@router.get("/options")
def setup_options(user: models.User = Depends(manager)) -> dict[str, bool]:
    return {"slack_oauth": oauth_configured()}


@router.post("", status_code=201)
def start_setup(
    payload: SetupStart,
    db: Session = Depends(get_db_session),
    user: models.User = Depends(manager),
) -> dict[str, Any]:
    if not payload.bot_token:
        raise HTTPException(422, "A bot token is required")
    state = payload.model_dump(exclude={"provider"})
    try:
        info = (
            from_thread.run(discover, draft_config(payload.provider, state))
            if not payload.team_hint
            else from_thread.run(
                _discover_hint, draft_config(payload.provider, state), payload.team_hint
            )
        )
    except ChatProviderError as exc:
        raise HTTPException(422, str(exc)) from exc
    state["bot_user_id"] = info["bot_user_id"]
    if payload.provider == "discord":
        state["verification_secret"] = info.pop("verification_secret")
    state["discovery"] = info
    draft = crud_chat.drafts.create(
        db,
        obj_in={
            "account_id": user.account_id,
            "user_id": user.id,
            "provider": payload.provider,
            "state_encrypted": encrypt_value(json.dumps(state)),
            "expires_at": datetime.utcnow() + timedelta(minutes=30),
        },
    )
    return public_draft(draft)


async def _discover_hint(config: ChatProviderConfig, hint: str) -> dict[str, Any]:
    return await discover(config, team_hint=hint)


@router.get("/{draft_id}")
def read_setup(
    draft_id: UUID,
    db: Session = Depends(get_db_session),
    user: models.User = Depends(manager),
) -> dict[str, Any]:
    return public_draft(get_draft(db, draft_id, user))


@router.post("/{draft_id}/refresh")
def refresh_setup(
    draft_id: UUID,
    db: Session = Depends(get_db_session),
    user: models.User = Depends(manager),
) -> dict[str, Any]:
    draft = get_draft(db, draft_id, user)
    state = json.loads(decrypt_value(draft.state_encrypted))
    try:
        info = from_thread.run(discover, draft_config(draft.provider, state))
    except ChatProviderError as exc:
        raise HTTPException(422, str(exc)) from exc
    if draft.provider == "discord":
        state["verification_secret"] = info.pop("verification_secret")
    state["discovery"] = info
    draft = crud_chat.drafts.update(
        db, db_obj=draft, obj_in={"state_encrypted": encrypt_value(json.dumps(state))}
    )
    return public_draft(draft)


class SigningSecret(BaseModel):
    verification_secret: str = Field(min_length=1, max_length=1024)


@router.put("/{draft_id}/signing-secret")
def signing_secret(
    draft_id: UUID,
    payload: SigningSecret,
    db: Session = Depends(get_db_session),
    user: models.User = Depends(manager),
) -> dict[str, Any]:
    draft = get_draft(db, draft_id, user)
    if draft.provider != "slack":
        raise HTTPException(409, "This provider discovers its verification credentials")
    state = json.loads(decrypt_value(draft.state_encrypted))
    state["verification_secret"] = payload.verification_secret
    draft = crud_chat.drafts.update(
        db, db_obj=draft, obj_in={"state_encrypted": encrypt_value(json.dumps(state))}
    )
    return public_draft(draft)


@router.post("/{draft_id}/finish", status_code=201)
def finish_setup(
    draft_id: UUID,
    payload: SetupFinish,
    db: Session = Depends(get_db_session),
    user: models.User = Depends(manager),
) -> dict[str, Any]:
    previous = crud_chat.drafts.get(db, draft_id, account_id=str(user.account_id))
    existing = None
    if previous and previous.user_id == user.id and previous.consumed_at:
        existing = crud_chat.connections.get(
            db, draft_id, account_id=str(user.account_id)
        )
        if existing and existing.provider != "discord":
            return public_connection(db, existing, user)
    if existing:
        if existing.workspace_id != payload.workspace_id:
            raise HTTPException(409, "Retry installation for the original workspace")
        provider = existing.provider
        state = json.loads(decrypt_value(existing.credentials_encrypted))
    else:
        draft = get_draft(db, draft_id, user)
        provider = draft.provider
        state = json.loads(decrypt_value(draft.state_encrypted))
    config = draft_config(provider, state, payload.workspace_id)
    try:
        info = from_thread.run(discover, config)
        if info["bot_user_id"] != state.get("bot_user_id"):
            raise ChatProviderError("The bot identity changed; start setup again")
        if payload.workspace_id not in {row["id"] for row in info["workspaces"]}:
            raise ChatProviderError(
                "Add the bot to the selected workspace, then refresh"
            )
        if provider == "slack" and not state.get("verification_secret"):
            raise ChatProviderError(
                "Enter the Signing Secret from Slack → Basic Information → App Credentials"
            )
        if provider == "discord":
            state["verification_secret"] = info["verification_secret"]
            config = draft_config(provider, state, payload.workspace_id)
    except ChatProviderError as exc:
        raise HTTPException(422, str(exc)) from exc
    credentials = {
        key: state.get(key, "")
        for key in (
            "verification_secret",
            "bot_token",
            "bot_user_id",
            "base_url",
            "slack_refresh_token",
            "slack_token_expires_at",
        )
        if key in state
    }
    try:
        if existing:
            connection = crud_chat.connections.update(
                db,
                db_obj=existing,
                obj_in={
                    "credentials_encrypted": encrypt_value(json.dumps(credentials))
                },
            )
        else:
            # The callback target is durable before ANY remote installation
            # mutation. A failed/expired consume can never orphan a callback.
            connection = crud_chat.complete_setup(
                db,
                draft_id,
                user.account_id,
                user.id,
                {
                    "provider": provider,
                    "workspace_id": payload.workspace_id,
                    "name": payload.name.strip()
                    or info["name"]
                    or f"Preloop {provider.title()} bot",
                    "credentials_encrypted": encrypt_value(json.dumps(credentials)),
                },
            )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if provider == "discord":
        try:
            from_thread.run(
                _install_discord,
                config,
                info["application_id"],
                callback(draft_id),
                payload.replace_callback,
            )
        except ChatProviderError as exc:
            # Retain the durable target; Finish retries the idempotent install
            # even though the draft has already been consumed.
            raise HTTPException(
                422,
                "Connection saved; retry Finish to complete installation. " + str(exc),
            ) from exc
    return public_connection(db, connection, user)


async def _install_discord(
    config: ChatProviderConfig, app_id: str, url: str, replace: bool
) -> None:
    await install_discord(config, app_id, url, replace_callback=replace)


@router.post("/slack/oauth")
def slack_oauth(
    response: Response,
    db: Session = Depends(get_db_session),
    user: models.User = Depends(manager),
) -> dict[str, str]:
    if not oauth_configured():
        raise HTTPException(404, "Slack OAuth is not configured")
    browser_proof = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(
        browser_proof.encode(), nonce.encode(), hashlib.sha256
    ).hexdigest()
    state = nonce + "." + signature
    response.set_cookie(
        "__Host-preloop_slack_oauth",
        browser_proof,
        max_age=900,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    draft = crud_chat.drafts.create(
        db,
        obj_in={
            "account_id": user.account_id,
            "user_id": user.id,
            "provider": "slack",
            "state_encrypted": encrypt_value("{}"),
            "expires_at": datetime.utcnow() + timedelta(minutes=15),
            "oauth_digest": hashlib.sha256(state.encode()).hexdigest(),
        },
    )
    return {
        "id": str(draft.id),
        "authorization_url": "https://slack.com/oauth/v2/authorize?"
        + urlencode(
            {
                "client_id": os.environ["CHAT_SLACK_CLIENT_ID"],
                "scope": "chat:write,im:write,im:history,app_mentions:read",
                "state": state,
                "redirect_uri": os.environ["CHAT_SLACK_REDIRECT_URI"],
            }
        ),
    }


async def _exchange_slack(code: str) -> dict[str, Any]:
    from preloop.services.slack_oauth import exchange_slack

    return await exchange_slack(
        {"code": code, "redirect_uri": os.environ["CHAT_SLACK_REDIRECT_URI"]}
    )


@router.get("/slack/callback", response_class=HTMLResponse)
def slack_callback(
    request: Request, state: str, code: str, db: Session = Depends(get_db_session)
) -> HTMLResponse:
    if not oauth_configured() or len(state) > 256 or len(code) > 2048:
        raise HTTPException(400, "Invalid authorization")
    proof = request.cookies.get("__Host-preloop_slack_oauth", "")
    nonce, separator, signature = state.rpartition(".")
    expected = hmac.new(proof.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    if not proof or not separator or not hmac.compare_digest(signature, expected):
        raise HTTPException(403, "Return to the browser that started Slack setup")
    draft = crud_chat.oauth_draft(db, hashlib.sha256(state.encode()).hexdigest())
    if draft is None:
        raise HTTPException(400, "Authorization expired or already used")
    try:
        data = from_thread.run(_exchange_slack, code)
        values: dict[str, Any] = {
            "bot_token": data["access_token"],
            "verification_secret": os.environ["CHAT_SLACK_SIGNING_SECRET"],
        }
        info = from_thread.run(discover, draft_config("slack", values))
        if info["workspaces"][0]["id"] != data.get("team", {}).get("id"):
            raise ChatProviderError("Slack workspace mismatch")
        if data.get("refresh_token"):
            import time

            values["slack_refresh_token"] = data["refresh_token"]
            values["slack_token_expires_at"] = time.time() + int(
                data.get("expires_in", 43200)
            )
        values.update(bot_user_id=info["bot_user_id"], discovery=info)
        crud_chat.drafts.update(
            db,
            db_obj=draft,
            obj_in={"state_encrypted": encrypt_value(json.dumps(values))},
        )
    except (ChatProviderError, httpx.HTTPError, ValueError) as exc:
        raise HTTPException(422, "Slack authorization failed; restart setup") from exc
    response = HTMLResponse(
        "<p>Slack authorized. Return to Chat connections and refresh setup to confirm the workspace.</p>",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'",
        },
    )
    response.delete_cookie(
        "__Host-preloop_slack_oauth",
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response
