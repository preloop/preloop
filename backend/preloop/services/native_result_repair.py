"""Find why a native result does not match its schema, inside the agent container.

Stdlib only: :func:`repair_prompt` runs in the hosted wrapper (see
``CodexAgent._native_result_repair_block``), where ``jsonschema`` is not
installed. It covers the keywords a native-result schema can use (types,
``enum``/``const``, ``properties``/``required``/``additionalProperties``,
``minProperties``/``maxProperties``, ``items``, ``uniqueItems``, length and
size bounds, ``pattern``, numeric bounds, ``multipleOf``, ``anyOf`` and local
``$ref``). It only decides whether to ask the model for a corrected
answer; the controller validates the captured document with the full draft
2020-12 validator and stays authoritative. A keyword this module does not
know is skipped, so it can under-report but never invents an error that the
controller would not also find for the keywords it does check.
"""

import json
import re
from typing import Any

#: Most errors named in one repair prompt.
MAX_REPAIR_ERRORS = 20
#: Longest previous answer quoted back to the model, in characters.
MAX_PREVIOUS_ANSWER_CHARS = 64 * 1024
#: Longest value shown inside one error message, in characters (the
#: controller caps stored messages at 300 characters too).
MAX_VALUE_CHARS = 200

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

_TYPES: dict[str, Any] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
    "number": lambda value: isinstance(value, (int, float))
    and not isinstance(value, bool),
    "integer": lambda value: (isinstance(value, int) and not isinstance(value, bool))
    or (isinstance(value, float) and value.is_integer()),
}


def _show(value: Any) -> str:
    """``repr`` of a value, shortened so one error cannot flood the prompt."""
    text = repr(value)
    if len(text) <= MAX_VALUE_CHARS:
        return text
    return text[: MAX_VALUE_CHARS - 3] + "..."


def _path(parts: list[Any]) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _resolve(root: Any, reference: Any) -> Any:
    if not isinstance(reference, str) or not reference.startswith("#"):
        return None
    target = root
    for part in reference[2:].split("/") if reference.startswith("#/") else []:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(target, list) and part.isdecimal():
            index = int(part)
            target = target[index] if index < len(target) else None
        elif isinstance(target, dict):
            target = target.get(part)
        else:
            return None
    return target


def schema_errors(document: Any, schema: Any) -> list[dict[str, str]]:
    """Validation errors of ``document`` against a native-result ``schema``.

    Args:
        document: The parsed final answer.
        schema: The flow's result schema.

    Returns:
        ``[{"path", "message"}]`` in document order; empty when no checked
        keyword fails. Paths name a missing property itself (``$.findings``),
        as the controller's validator reports them.
    """
    errors: list[dict[str, str]] = []

    def check(value: Any, node: Any, parts: list[Any], depth: int) -> None:
        if not isinstance(node, dict) or depth > 64:
            return
        if "$ref" in node:
            check(value, _resolve(schema, node["$ref"]), parts, depth + 1)
        if "anyOf" in node and isinstance(node["anyOf"], list):
            if not any(
                not _collect(value, option, depth + 1) for option in node["anyOf"]
            ):
                errors.append(
                    {
                        "path": _path(parts),
                        "message": f"{_show(value)} is not valid under any of the "
                        "given schemas",
                    }
                )
        declared = node.get("type")
        if declared is not None:
            names = declared if isinstance(declared, list) else [declared]
            known = [name for name in names if name in _TYPES]
            if known and not any(_TYPES[name](value) for name in known):
                wanted = ", ".join(repr(name) for name in known)
                errors.append(
                    {
                        "path": _path(parts),
                        "message": f"{_show(value)} is not of type {wanted}",
                    }
                )
                return
        if "enum" in node and isinstance(node["enum"], list):
            if not any(_equal(value, option) for option in node["enum"]):
                errors.append(
                    {
                        "path": _path(parts),
                        "message": f"{_show(value)} is not one of {_show(node['enum'])}",
                    }
                )
        if "const" in node and not _equal(value, node["const"]):
            errors.append(
                {
                    "path": _path(parts),
                    "message": f"{_show(node['const'])} was expected",
                }
            )
        if isinstance(value, str):
            _string(value, node, parts)
        if _TYPES["number"](value):
            _number(value, node, parts)
        if isinstance(value, list):
            _array(value, node, parts, depth)
        if isinstance(value, dict):
            _object(value, node, parts, depth)

    def _collect(value: Any, node: Any, depth: int) -> list[dict[str, str]]:
        before = len(errors)
        check(value, node, [], depth)
        found = errors[before:]
        del errors[before:]
        return found

    def _string(value: str, node: dict[str, Any], parts: list[Any]) -> None:
        minimum, maximum = node.get("minLength"), node.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(
                {"path": _path(parts), "message": f"{_show(value)} is too short"}
            )
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(
                {"path": _path(parts), "message": f"{_show(value)} is too long"}
            )
        pattern = node.get("pattern")
        if isinstance(pattern, str):
            try:
                matched = re.search(pattern, value) is not None
            except re.error:
                matched = True
            if not matched:
                errors.append(
                    {
                        "path": _path(parts),
                        "message": f"{_show(value)} does not match {pattern!r}",
                    }
                )

    def _number(value: Any, node: dict[str, Any], parts: list[Any]) -> None:
        bounds = (
            ("minimum", lambda limit: value < limit, "less than the minimum of"),
            ("maximum", lambda limit: value > limit, "greater than the maximum of"),
            (
                "exclusiveMinimum",
                lambda limit: value <= limit,
                "less than or equal to the minimum of",
            ),
            (
                "exclusiveMaximum",
                lambda limit: value >= limit,
                "greater than or equal to the maximum of",
            ),
        )
        for keyword, fails, words in bounds:
            limit = node.get(keyword)
            if _TYPES["number"](limit) and fails(limit):
                errors.append(
                    {
                        "path": _path(parts),
                        "message": f"{_show(value)} is {words} {limit!r}",
                    }
                )
        step = node.get("multipleOf")
        if _TYPES["number"](step) and step > 0:
            quotient = value / step
            if isinstance(quotient, float) and not quotient.is_integer():
                errors.append(
                    {
                        "path": _path(parts),
                        "message": f"{_show(value)} is not a multiple of {step!r}",
                    }
                )

    def _array(
        value: list[Any], node: dict[str, Any], parts: list[Any], depth: int
    ) -> None:
        minimum, maximum = node.get("minItems"), node.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(
                {"path": _path(parts), "message": f"{_show(value)} is too short"}
            )
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(
                {"path": _path(parts), "message": f"{_show(value)} is too long"}
            )
        if node.get("uniqueItems") is True:
            seen: list[Any] = []
            for item in value:
                if any(_equal(item, other) for other in seen):
                    errors.append(
                        {
                            "path": _path(parts),
                            "message": f"{_show(value)} has non-unique elements",
                        }
                    )
                    break
                seen.append(item)
        items = node.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                check(item, items, [*parts, index], depth + 1)

    def _object(
        value: dict[str, Any], node: dict[str, Any], parts: list[Any], depth: int
    ) -> None:
        properties = node.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = node.get("required")
        for name in required if isinstance(required, list) else []:
            if isinstance(name, str) and name not in value:
                errors.append(
                    {
                        "path": _path([*parts, name]),
                        "message": f"{_show(name)} is a required property",
                    }
                )
        fewest, most = node.get("minProperties"), node.get("maxProperties")
        if isinstance(fewest, int) and len(value) < fewest:
            errors.append(
                {
                    "path": _path(parts),
                    "message": f"{_show(value)} does not have enough properties",
                }
            )
        if isinstance(most, int) and len(value) > most:
            errors.append(
                {
                    "path": _path(parts),
                    "message": f"{_show(value)} has too many properties",
                }
            )
        extra = node.get("additionalProperties")
        unexpected = [name for name in value if name not in properties]
        if extra is False and unexpected:
            names = ", ".join(repr(name) for name in unexpected)
            errors.append(
                {
                    "path": _path(parts),
                    "message": f"Additional properties are not allowed ({names} "
                    f"{'was' if len(unexpected) == 1 else 'were'} unexpected)",
                }
            )
        for name, child in value.items():
            if name in properties:
                check(child, properties[name], [*parts, name], depth + 1)
            elif isinstance(extra, dict):
                check(child, extra, [*parts, name], depth + 1)

    check(document, schema, [], 0)
    return errors


def _equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and bool(left == right)
    return bool(left == right)


def repair_prompt(raw: Any, schema: Any, prompt: str) -> str:
    """The continuation prompt for an invalid final answer, or "" when valid.

    Args:
        raw: The final answer as text, or None when the session wrote none.
        schema: The flow's result schema.
        prompt: The run's original prompt, repeated first so the fresh
            session has the task and the schema text it already had.

    Returns:
        An empty string when the answer is a JSON document with no error this
        module checks for; otherwise the prompt naming each error.
    """
    if raw is None or not str(raw).strip():
        errors = [{"path": "$", "message": "No final answer was written"}]
        previous = "(none)"
    else:
        text = str(raw)
        previous = text[:MAX_PREVIOUS_ANSWER_CHARS]
        try:
            document = json.loads(text)
        except ValueError as exc:
            errors = [{"path": "$", "message": f"Not valid JSON: {exc}"}]
        else:
            errors = schema_errors(document, schema)
    if not errors:
        return ""
    lines = [f"- {error['path']}: {error['message']}" for error in errors]
    if len(lines) > MAX_REPAIR_ERRORS:
        lines = [
            *lines[:MAX_REPAIR_ERRORS],
            f"- (+{len(errors) - MAX_REPAIR_ERRORS} more)",
        ]
    return prompt + REPAIR_INSTRUCTIONS.format(
        errors="\n".join(lines), previous=previous
    )


def repair_paths(raw: Any, schema: Any) -> list[str]:
    """JSON paths that fail, for the continuation marker (no values).

    Args:
        raw: The final answer as text, or None.
        schema: The flow's result schema.

    Returns:
        Up to :data:`MAX_REPAIR_ERRORS` distinct paths.
    """
    try:
        document = json.loads(str(raw))
    except (TypeError, ValueError):
        return ["$"]
    paths = [error["path"] for error in schema_errors(document, schema)]
    return list(dict.fromkeys(paths))[:MAX_REPAIR_ERRORS]
