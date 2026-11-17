"""Conservative, auditable signals that a server discarded input context."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow_execution
from preloop.services.context_optimization import estimate_tokens

CONTEXT_TRUNCATED_SUSPECTED = "context_truncated_suspected"
# Character estimates vary across tokenizers. Require a large absolute deficit
# as well as less than 40% of the estimate, never just a small token discrepancy.
MIN_INPUT_ESTIMATE = 8192
MIN_TOKEN_DEFICIT = 8192
MAX_REPORTED_RATIO = 0.4
MIN_HISTORY_GROWTH = 128
logger = logging.getLogger(__name__)


def estimate_request_input(payload: dict[str, Any]) -> int:
    """Estimate textual input, including schemas, without counting media blobs.

    Only model-visible input fields count. Images/audio/files can have enormous
    wire representations unrelated to their input tokens. Excluding them makes
    this a text-only lower estimate, not a multimodal capacity guarantee.
    """

    def text_only(value: Any) -> Any:
        if isinstance(value, dict):
            if isinstance(value.get("type"), str) and value["type"] in {
                "image_url",
                "input_image",
                "input_audio",
                "audio",
                "image",
                "input_file",
                "file",
                "video",
                "video_url",
            }:
                return None
            return {
                key: cleaned
                for key, item in value.items()
                if key not in {"encrypted_content", "signature"}
                if (cleaned := text_only(item)) is not None
            }
        if isinstance(value, list):
            return [
                cleaned for item in value if (cleaned := text_only(item)) is not None
            ]
        if isinstance(value, str) and value.startswith("data:"):
            return None
        return value

    content = {
        key: text_only(payload[key])
        for key in (
            "instructions",
            "system",
            "messages",
            "input",
            "tools",
            "functions",
            "response_format",
            "text",
        )
        if payload.get(key) is not None
    }
    if not content:
        return 0
    return int(
        estimate_tokens(json.dumps(content, ensure_ascii=False, separators=(",", ":")))
    )


def detect_truncation(
    *, estimated: int, reported: int, previous: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return evidence only for a substantial deficit or a growing-history plateau.

    Counts must be provider reported and cache inclusive. Estimated fallback
    usage and zero/missing usage never prove input truncation. The plateau
    signal still requires a 25% deficit to tolerate small schema/token variance.
    """
    if reported <= 0 or estimated < MIN_INPUT_ESTIMATE:
        return None
    signal = None
    if (
        estimated - reported >= MIN_TOKEN_DEFICIT
        and reported < estimated * MAX_REPORTED_RATIO
    ):
        signal = "input_token_deficit"
    elif (
        previous
        and previous.get("reported_input_tokens") == reported
        and isinstance(previous.get("estimated_input_tokens"), int)
        and estimated - previous["estimated_input_tokens"] >= MIN_HISTORY_GROWTH
        and reported < estimated * 0.75
    ):
        signal = "unchanged_input_tokens_with_growing_history"
    if signal is None:
        return None
    evidence = {
        "signal": signal,
        "estimated_input_tokens": estimated,
        "reported_input_tokens": reported,
    }
    if previous:
        evidence["previous_estimated_input_tokens"] = previous.get(
            "estimated_input_tokens"
        )
        evidence["previous_reported_input_tokens"] = previous.get(
            "reported_input_tokens"
        )
    return evidence


def truncation_message(signal: dict[str, Any]) -> str:
    """Describe the observed capacity problem and the server-side remedy."""
    estimate = signal["estimated_input_tokens"]
    if signal["signal"] == "registered_context_window_exceeded":
        lead = (
            f"The request exceeds the model server's registered context window of "
            f"{signal['context_window']} tokens and was not sent"
        )
    else:
        lead = (
            "The model server appears to have truncated the prompt to "
            f"{signal['reported_input_tokens']} tokens"
        )
    return (
        f"{lead}; the request needed about {estimate} input tokens. "
        "Raise the server's context window or choose a model with a larger one. "
        "Set the actual server limit in model_parameters.context_window "
        "(docs/guide/model-context-limits.md)."
    )


def record_execution_truncation(
    db: Session, *, execution_id: Any, signal: dict[str, Any]
) -> None:
    """Persist the gateway diagnosis independently of the agent's final result."""
    try:
        crud_flow_execution.record_context_truncation(
            db,
            execution_id=execution_id,
            signal=signal,
            message=truncation_message(signal),
            category=CONTEXT_TRUNCATED_SUSPECTED,
        )
    except Exception as exc:  # noqa: BLE001 - annotation cannot mask the outcome
        logger.warning(
            "Could not annotate context truncation for execution %s (%s)",
            execution_id,
            type(exc).__name__,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - preserve the original gateway outcome
            logger.warning("Rollback after context truncation annotation failed")
