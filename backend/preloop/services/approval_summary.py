"""Generate short user-facing summaries for approval requests."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models.crud.ai_model import ai_model as crud_ai_model
from preloop.models.models.ai_model import AIModel
from preloop.services.litellm_routing import to_litellm_model
from preloop.utils.redaction import redact_dict

logger = logging.getLogger(__name__)

SUMMARY_TIMEOUT_SECONDS = 5.0
SUMMARY_MAX_TOKENS = 150
SUMMARY_MAX_CHARS = 400


def _ask_user_question(tool_args: Optional[dict[str, Any]]) -> Optional[str]:
    """Return ask_user question text when present."""
    if not isinstance(tool_args, dict):
        return None
    if not tool_args.get("is_question"):
        return None
    question = tool_args.get("question")
    if isinstance(question, str) and question.strip():
        return question.strip()
    return None


def _to_litellm_model(model: AIModel) -> str:
    return to_litellm_model(model)


def _compact_args(tool_args: dict[str, Any]) -> dict[str, Any]:
    """Shrink redacted args for the summarizer prompt."""
    compact: dict[str, Any] = {}
    for key, value in tool_args.items():
        if key.startswith("_"):
            continue
        if key in {"content", "body", "message", "prompt", "files"}:
            text = str(value)
            compact[key] = text[:200] + ("..." if len(text) > 200 else "")
            continue
        if isinstance(value, str) and len(value) > 300:
            compact[key] = value[:297] + "..."
        elif isinstance(value, (dict, list)):
            serialized = json.dumps(value, default=str)
            if len(serialized) > 400:
                compact[key] = serialized[:397] + "..."
            else:
                compact[key] = value
        else:
            compact[key] = value
    return compact


def _call_summary_model(
    *,
    model: AIModel,
    tool_name: str,
    tool_args: dict[str, Any],
    agent_reasoning: Optional[str],
    managed_agent_name: Optional[str],
) -> str:
    """Synchronous litellm completion for a short approval ask."""
    import litellm

    context = {
        "tool_name": tool_name,
        "agent_name": managed_agent_name,
        "agent_reasoning": (agent_reasoning or "")[:500] or None,
        "tool_args": _compact_args(tool_args),
    }
    kwargs: dict[str, Any] = {
        "model": _to_litellm_model(model),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You write the primary question shown to a human when an AI "
                    "agent needs approval to run a tool. Reply with 1-2 short "
                    "imperative sentences only (no markdown, no JSON, no quotes). "
                    "Example: Allow the coding agent to force-push branch feature/x "
                    "on repo acme/api?\n"
                    "Rules: describe the concrete action and key arguments; do not "
                    "invent permissions or side effects; never include secrets, "
                    "tokens, or raw JSON dumps; stay under 280 characters."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False, default=str),
            },
        ],
        "temperature": 0.1,
        "max_tokens": SUMMARY_MAX_TOKENS,
    }
    if model.api_key:
        kwargs["api_key"] = model.api_key
    if model.api_endpoint:
        kwargs["api_base"] = model.api_endpoint

    response = litellm.completion(**kwargs)
    text = (response.choices[0].message.content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    # Drop accidental surrounding quotes
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return text


async def generate_approval_summary(
    db: Session,
    *,
    account_id: str,
    tool_name: str,
    tool_args: Optional[dict[str, Any]] = None,
    agent_reasoning: Optional[str] = None,
    managed_agent_name: Optional[str] = None,
) -> Optional[str]:
    """Produce a user-facing approval ask, or None when unavailable.

    Prefer the ask_user question when present. Otherwise call the account's
    default (or system) LLM with a short timeout. Failures return None so
    callers keep the existing tool_name / JSON UX.
    """
    args = tool_args or {}
    question = _ask_user_question(args)
    if question:
        return question[:SUMMARY_MAX_CHARS]

    try:
        model = crud_ai_model.get_default_active_model(
            db, account_id=str(account_id), model_kind="llm"
        )
    except Exception as exc:
        logger.warning("Could not resolve default model for approval summary: %s", exc)
        return None

    if model is None:
        logger.debug(
            "No default LLM for account %s; skipping approval summary", account_id
        )
        return None

    redacted_args = redact_dict(dict(args))
    try:
        summary = await asyncio.wait_for(
            asyncio.to_thread(
                _call_summary_model,
                model=model,
                tool_name=tool_name,
                tool_args=redacted_args,
                agent_reasoning=agent_reasoning,
                managed_agent_name=managed_agent_name,
            ),
            timeout=SUMMARY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Approval summary timed out after %ss for tool %s",
            SUMMARY_TIMEOUT_SECONDS,
            tool_name,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Approval summary generation failed for tool %s: %s",
            tool_name,
            exc,
            exc_info=True,
        )
        return None

    if not summary:
        return None
    return summary
