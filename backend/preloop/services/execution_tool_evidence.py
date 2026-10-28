"""Versioned tool observations, independent of bounded console previews.

A model request is not proof that a tool executed. Request-side tool results
are separately identified observations. Hashes cover canonical sanitized data,
never hidden credentials, and cannot recover previously uncaptured bytes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from preloop.utils.secret_scrubbing import scrub_structure

EVIDENCE_TYPE = "execution_tool_evidence"
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_OBSERVATIONS = 512


def canonical_bytes(value: Any) -> bytes:
    """Canonical v1 encoding: sorted keys, compact JSON, UTF-8, no ASCII escapes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    """SHA-256 of the documented canonical representation."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _redact_value(value: Any) -> Any:
    """Keep exact nonsecret wire strings while scrubbing structured secrets."""
    from preloop.services.model_gateway_events import ModelGatewayEventEmitter

    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(
                word in key.lower()
                for word in (
                    "api_key",
                    "authorization",
                    "password",
                    "secret",
                    "credential",
                )
            )
            or ("token" in key.lower() and "tokens" not in key.lower())
            else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        scrubbed = ModelGatewayEventEmitter._redact_for_tool_activity(
            scrub_structure(value)
        )
        try:
            parsed = json.loads(scrubbed)
        except ValueError:
            return scrubbed
        if isinstance(parsed, (dict, list)):
            clean = _redact_value(parsed)
            if clean != parsed:
                return canonical_bytes(clean).decode("utf-8")
        return scrubbed
    return value


def capture_tools(
    request: Any, response: Any, *, capture_content: bool
) -> dict[str, Any]:
    """Capture tool-only wire observations without prompt/history body fields."""
    observations: list[dict[str, Any]] = []
    incomplete = False
    remaining = MAX_EVIDENCE_BYTES

    def add(item: dict[str, Any], source: str, kind: str) -> None:
        nonlocal incomplete, remaining
        function = item.get("function")
        function = function if isinstance(function, dict) else item
        raw = (
            function.get("arguments", item.get("input"))
            if kind == "call"
            else item.get("output", item.get("content"))
        )
        identity = (
            item.get("call_id")
            or item.get("tool_call_id")
            or item.get("tool_use_id")
            or item.get("id")
        )
        name = function.get("name")
        # Enforce identifier bounds independently of argument content.
        if not isinstance(identity, str) or not 0 < len(identity) <= 256:
            incomplete = True
            identity = None
        if name is not None and (not isinstance(name, str) or len(name) > 256):
            incomplete = True
            name = None
        value = _redact_value(raw) if capture_content else None
        if isinstance(value, str):
            # Keep exact wire argument strings. This redacts recognized secrets
            # without reserializing JSON or applying the 8KiB preview limit.
            from preloop.services.model_gateway_events import ModelGatewayEventEmitter

            value = ModelGatewayEventEmitter._redact_for_tool_activity(value)
        redacted = not capture_content or value != raw
        record = {
            "source": source,
            "kind": kind,
            "call_id": identity,
            "name": name,
            "value": value,
            "redacted": redacted,
            "truncated": False,
        }
        encoded = canonical_bytes(record)
        if len(observations) >= MAX_OBSERVATIONS or len(encoded) > remaining:
            incomplete = True
            return
        remaining -= len(encoded)
        observations.append(record)

    def scan(items: Any, source: str, depth: int = 0) -> None:
        nonlocal incomplete
        if not isinstance(items, list):
            return
        if depth > 12:
            incomplete = True
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind in {"function_call", "tool_use", "custom_tool_call"}:
                add(item, source, "call")
            elif (
                kind
                in {"function_call_output", "tool_result", "custom_tool_call_output"}
                or item.get("role") == "tool"
            ):
                add(item, source, "result")
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict):
                    add(call, source, "call")
            scan(item.get("content"), source, depth + 1)

    if isinstance(response, dict):
        scan(response.get("output"), "response")
        scan(response.get("content"), "response")
        for choice in response.get("choices") or []:
            if isinstance(choice, dict):
                scan([choice.get("message")], "response")
    if isinstance(request, dict):
        scan(request.get("messages"), "request")
        scan(request.get("input"), "request")
    return {
        "version": 1,
        "observations": observations,
        "capture_complete": not incomplete,
        "content_capture_enabled": capture_content,
        "response_available": isinstance(response, dict),
    }
