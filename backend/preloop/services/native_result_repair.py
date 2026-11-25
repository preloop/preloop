"""Build repair prompts using the controller's authoritative schema verdict.

The hosted wrapper embeds this stdlib-only client and calls the controller.
Local callers use the same validator directly. No schema keywords are skipped.
"""

import json
import os
from typing import Any, Callable, cast
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from preloop.services.native_answer import parse_native_answer
except ImportError:  # pragma: no cover - embedded after that module's source
    pass

#: Said first for an answer that is not JSON, in words a model acts on.
NOT_JSON_INSTRUCTION = (
    "Your answer was wrapped in a Markdown code fence or contained text "
    "outside the JSON. Reply with the JSON object only: no backticks, no prose."
)

#: Most errors named in one repair prompt.
MAX_REPAIR_ERRORS = 20
#: Longest previous answer quoted back to the model, in characters.
MAX_PREVIOUS_ANSWER_CHARS = 64 * 1024

REPAIR_INSTRUCTIONS = """

---
Your previous final answer was a review, but it does not match the required \
result schema, so it cannot be accepted. Validation errors (JSON path: message):
{errors}
Your previous final answer, verbatim:
{previous}

Return the same review again as one complete JSON object that satisfies the \
schema: keep its findings, evidence and verdict, and fix only what the errors \
name (add every missing required field, use only allowed values and the \
declared types). The repository inspection from your previous answer still \
counts; run a command only if a value you must fill in needs it.
"""

#: Used once per run, when the latest session neither read the change nor
#: gave a usable answer (see ``CodexAgent._native_result_repair_block``).
UNINSPECTED_REPAIR_INSTRUCTIONS = """

---
This review is not complete, for two reasons.

1. The repository has not been inspected as required. {inspection}

2. Your previous final answer does not match the required result schema, so \
it cannot be accepted. Validation errors (JSON path: message):
{errors}
Your previous final answer, verbatim:
{previous}

Do both now, in this order. First inspect the change with commands. Then \
return the review as one complete JSON object that satisfies the schema: every \
required field, only allowed values, the declared types. A progress note or a \
plan is not an answer; the final answer must be the finished review.
"""

Validator = Callable[[Any, Any], dict[str, Any]]


class _NoValidationRedirect(HTTPRedirectHandler):
    """Never forward the validation capability to another destination."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def controller_validation(document: Any, schema: Any) -> dict[str, Any]:
    """Ask the controller to validate against the launch-bound schema.

    Raises:
        OSError: When the controller cannot be reached.
        ValueError: When the controller response is not a validation verdict.
    """
    request = Request(
        os.environ["PRELOOP_RESULT_VALIDATION_URL"],
        data=json.dumps(
            {"document": document, "schema": schema},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Preloop-Result-Capability": os.environ[
                "PRELOOP_RESULT_VALIDATION_CAPABILITY"
            ],
        },
        method="POST",
    )
    with build_opener(_NoValidationRedirect).open(request, timeout=5) as response:
        payload = response.read(64 * 1024 + 1)
    if len(payload) > 64 * 1024:
        raise ValueError("Validation response is too large")
    result = json.loads(payload)
    if (
        not isinstance(result, dict)
        or type(result.get("valid")) is not bool
        or not isinstance(result.get("errors"), list)
        or result["valid"] != (not result["errors"])
        or any(
            not isinstance(error, dict)
            or not isinstance(error.get("path"), str)
            or not isinstance(error.get("message"), str)
            for error in result["errors"]
        )
    ):
        raise ValueError("Invalid validation verdict")
    return result


def schema_errors(
    document: Any, schema: Any, validator: Validator | None = None
) -> list[dict[str, str]]:
    """Return the controller's errors, including its paths and message bounds."""
    if not isinstance(document, dict):
        return [{"path": "$", "message": "The final answer was not a JSON object"}]
    if validator is None:
        from preloop.services.flow_result_schema import validate_result_document

        validator = validate_result_document
    return cast(list[dict[str, str]], validator(document, schema)["errors"])


def repair_prompt(
    raw: Any,
    schema: Any,
    prompt: str,
    inspection: str = "",
    *,
    validator: Validator | None = None,
) -> str:
    """The continuation prompt for an invalid final answer, or "" when valid.

    Args:
        raw: The final answer as text, or None when the session wrote none.
        schema: The flow's result schema.
        prompt: The run's original prompt, repeated first so the fresh
            session has the task and the schema text it already had.
        inspection: What the inspection check requires, in plain words. When
            given, the prompt is the combined one for a session that neither
            read the change nor answered usably: it asks for the inspection
            and the answer. Empty for a plain schema repair.
        validator: Controller transport, or the local controller validator.

    Returns:
        An empty string when the controller accepts the answer; otherwise
        the prompt naming each error.
    """
    if raw is None or not str(raw).strip():
        errors = [{"path": "$", "message": "No final answer was written"}]
        previous = "(none)"
    else:
        text = str(raw)
        previous = text[:MAX_PREVIOUS_ANSWER_CHARS]
        try:
            document, _ = parse_native_answer(text)
        except ValueError as exc:
            errors = [
                {
                    "path": "$",
                    "message": f"{NOT_JSON_INSTRUCTION} Not valid JSON: {exc}",
                }
            ]
        else:
            errors = schema_errors(document, schema, validator)
    if not errors:
        return ""
    lines = [f"- {error['path']}: {error['message']}" for error in errors]
    if len(lines) > MAX_REPAIR_ERRORS:
        lines = [
            *lines[:MAX_REPAIR_ERRORS],
            f"- (+{len(errors) - MAX_REPAIR_ERRORS} more)",
        ]
    if inspection:
        return prompt + UNINSPECTED_REPAIR_INSTRUCTIONS.format(
            inspection=inspection, errors="\n".join(lines), previous=previous
        )
    return prompt + REPAIR_INSTRUCTIONS.format(
        errors="\n".join(lines), previous=previous
    )


def repair_paths(
    raw: Any, schema: Any, *, validator: Validator | None = None
) -> list[str]:
    """JSON paths that fail, for the continuation marker (no values).

    Args:
        raw: The final answer as text, or None.
        schema: The flow's result schema.
        validator: Controller transport, or the local controller validator.

    Returns:
        Up to :data:`MAX_REPAIR_ERRORS` distinct paths.
    """
    try:
        document, _ = parse_native_answer(raw)
    except (TypeError, ValueError):
        return ["$"]
    paths = [error["path"] for error in schema_errors(document, schema, validator)]
    return list(dict.fromkeys(paths))[:MAX_REPAIR_ERRORS]
