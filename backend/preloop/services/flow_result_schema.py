"""Per-flow result schema: checked at flow save, applied at result capture (#868).

A flow may carry an optional JSON Schema (draft 2020-12) for the
``result.json`` document its agent writes. When the flow sets one and keeps
``validate_result`` on, the orchestrator validates the captured, sanitized
document against it. A document that does not conform turns a run that would
have SUCCEEDED into FAILED with ``failure_category: result_schema_invalid``;
the document itself is still persisted so a consumer can inspect it.

The CRA presets are exempt: they have a dedicated validator
(:mod:`preloop.cra.validate`) and the generic one is not applied on top.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

#: Largest accepted schema, as compact JSON text.
RESULT_SCHEMA_MAX_BYTES = 64 * 1024
#: Most validation errors stored and returned for one document.
RESULT_SCHEMA_MAX_ERRORS = 20
#: Longest stored error message.
RESULT_SCHEMA_MAX_MESSAGE = 300

#: Error message prefix; ``derive_failure_category`` matches on it.
RESULT_SCHEMA_INVALID_MESSAGE = "Result document does not match the flow result schema"


class ResultSchemaError(ValueError):
    """A flow's result schema is too large or not a valid JSON Schema."""


#: Keys the runner and control plane add to a result next to the agent's
#: document (host-exec harness envelope, gateway metering, container
#: termination). They are not validated against the flow's schema.
PLATFORM_RESULT_KEYS = frozenset(
    {
        "container_termination",
        "harness",
        "session_id",
        "usage",
        "premium_requests",
        "gateway_metered",
        "gateway_request_count",
        "gateway_bypass_allowed",
        "host_read_only_review",
    }
)


def check_result_schema(schema: Any) -> Optional[dict[str, Any]]:
    """Validate a flow's ``result_schema`` at save time.

    Args:
        schema: The submitted schema, or None to clear it.

    Returns:
        The schema unchanged, or None.

    Raises:
        ResultSchemaError: When the schema is not an object, exceeds
            :data:`RESULT_SCHEMA_MAX_BYTES`, or is not a valid draft
            2020-12 JSON Schema.
    """
    if schema is None:
        return None
    if not isinstance(schema, dict):
        raise ResultSchemaError("result_schema must be a JSON object")
    try:
        size = len(json.dumps(schema, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ResultSchemaError(f"result_schema is not valid JSON: {exc}") from exc
    if size > RESULT_SCHEMA_MAX_BYTES:
        raise ResultSchemaError(
            f"result_schema is {size} bytes; the limit is "
            f"{RESULT_SCHEMA_MAX_BYTES} bytes"
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        where = _json_path(exc.path)
        raise ResultSchemaError(
            f"result_schema is not a valid JSON Schema (draft 2020-12) at "
            f"{where}: {_bounded(exc.message)}"
        ) from exc
    return schema


def _json_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _bounded(message: str) -> str:
    if len(message) <= RESULT_SCHEMA_MAX_MESSAGE:
        return message
    return message[: RESULT_SCHEMA_MAX_MESSAGE - 3] + "..."


def _error_path(error: Any) -> str:
    parts = list(error.absolute_path)
    # A missing required property is reported on its parent object; name the
    # property itself so the path says which field the document lacks.
    if error.validator == "required" and isinstance(error.instance, Mapping):
        required = error.validator_value or []
        missing = [name for name in required if name not in error.instance]
        for name in missing:
            if f"'{name}'" in error.message:
                parts.append(name)
                break
    return _json_path(parts)


def validate_result_document(
    document: Optional[Mapping[str, Any]], schema: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate one captured result document against a flow's schema.

    Args:
        document: The sanitized ``result.json``, or None when none was
            captured.
        schema: The flow's result schema.

    Returns:
        ``{"valid": bool, "errors": [{"path", "message"}], "error_count": int}``
        with at most :data:`RESULT_SCHEMA_MAX_ERRORS` errors. Never raises: a
        schema that cannot be applied is reported as one error at ``$``.
    """
    if document is None:
        return {
            "valid": False,
            "errors": [{"path": "$", "message": "No result.json was captured"}],
            "error_count": 1,
        }
    instance = dict(document)
    # Platform-owned runtime metadata is not part of the authored document.
    # A schema that declares one of these keys still sees it.
    declared = schema.get("properties") if isinstance(schema, Mapping) else None
    declared = declared if isinstance(declared, Mapping) else {}
    for key in PLATFORM_RESULT_KEYS:
        if key not in declared:
            instance.pop(key, None)
    try:
        validator = Draft202012Validator(dict(schema))
        found = sorted(
            validator.iter_errors(instance),
            key=lambda err: (list(map(str, err.absolute_path)), err.message),
        )
    except Exception as exc:  # A stored schema that no longer applies.
        return {
            "valid": False,
            "errors": [
                {
                    "path": "$",
                    "message": _bounded(f"Result schema could not be applied: {exc}"),
                }
            ],
            "error_count": 1,
        }
    errors = [
        {"path": _error_path(err), "message": _bounded(err.message)}
        for err in found[:RESULT_SCHEMA_MAX_ERRORS]
    ]
    return {"valid": not found, "errors": errors, "error_count": len(found)}


def flow_result_schema(flow: Any) -> Optional[dict[str, Any]]:
    """The schema to apply for ``flow``, or None when validation is off."""
    if flow is None:
        return None
    schema = getattr(flow, "result_schema", None)
    if not isinstance(schema, dict) or not schema:
        return None
    if getattr(flow, "validate_result", True) is False:
        return None
    return schema


def schema_failure_message(validation: Mapping[str, Any]) -> str:
    """Execution error message for an invalid result document."""
    errors = validation.get("errors") or []
    first = errors[0] if errors else {"path": "$", "message": "invalid"}
    count = int(validation.get("error_count") or len(errors) or 1)
    more = f" (+{count - 1} more)" if count > 1 else ""
    return (
        f"{RESULT_SCHEMA_INVALID_MESSAGE}: {first.get('path')}: "
        f"{first.get('message')}{more}"
    )
