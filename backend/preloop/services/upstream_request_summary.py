"""Allowlisted, content-free evidence of the effective upstream request."""

from typing import Any

from preloop.schemas.openrouter import validate_provider_preferences

EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


def summarize_upstream_request(
    body: dict[str, Any],
    *,
    schema_in_instructions: bool = False,
    openrouter: bool = False,
) -> dict[str, Any]:
    """Project controls only, without retaining messages, schemas or headers.

    Args:
        body: Effective outbound body, after protocol translation.
        schema_in_instructions: Whether the gateway moved a schema into instructions.
        openrouter: Whether provider routing is meaningful for this upstream.

    Returns:
        A compact JSON-serializable summary with no request content.
    """
    extra = body.get("extra_body")
    effective = {**body, **extra} if isinstance(extra, dict) else body
    result: dict[str, Any] = {}
    if isinstance(effective.get("model"), str):
        result["model"] = effective["model"]
    config = effective.get("generationConfig") or effective.get("inferenceConfig") or {}
    config = config if isinstance(config, dict) else {}
    for source, keys in (
        (effective, ("max_completion_tokens", "max_output_tokens", "max_tokens")),
        (config, ("maxOutputTokens", "maxTokens")),
    ):
        for key in keys:
            if type(source.get(key)) is int:
                result["max_tokens"] = source[key]
                break
        if "max_tokens" in result:
            break
    text = effective.get("text")
    output_config = effective.get("output_config")
    bedrock_output = effective.get("outputConfig")
    output = effective.get("response_format")
    if output is None and isinstance(text, dict):
        output = text.get("format")
    if output is None and isinstance(output_config, dict):
        output = output_config.get("format")
    if output is None and isinstance(bedrock_output, dict):
        output = bedrock_output.get("textFormat")
    output_format: dict[str, Any] = {"type": "none"}
    if (
        isinstance(output, dict)
        and isinstance(output.get("type"), str)
        and output.get("type") in {"json_object", "json_schema"}
    ):
        output_format = {"type": output["type"]}
        if output["type"] == "json_schema":
            schema = output.get("json_schema", output)
            output_format["strict"] = (
                isinstance(schema, dict) and schema.get("strict") is True
            )
    elif config.get("responseMimeType") == "application/json":
        output_format = (
            {"type": "json_schema", "strict": False}
            if (config.get("responseSchema") or config.get("responseJsonSchema"))
            else {"type": "json_object"}
        )
    result["output_format"] = output_format
    result["schema_in_instructions"] = schema_in_instructions
    tools = effective.get("tools")
    tool_config = effective.get("toolConfig")
    if tools is None and isinstance(tool_config, dict):
        tools = tool_config.get("tools")
    result["tool_count"] = (
        sum(
            len(tool["functionDeclarations"])
            if isinstance(tool, dict)
            and isinstance(tool.get("functionDeclarations"), list)
            else 1
            for tool in tools
        )
        if isinstance(tools, list)
        else 0
    )
    reasoning = effective.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, dict) else {}
    effort = reasoning.get("effort", effective.get("reasoning_effort"))
    if effort is None and isinstance(output_config, dict):
        effort = output_config.get("effort")
    safe_reasoning: dict[str, Any] = {}
    if isinstance(effort, str) and effort in EFFORTS:
        safe_reasoning["effort"] = effort
        result["reasoning_effort"] = effort
    for key in ("max_tokens", "enabled", "exclude"):
        value = reasoning.get(key)
        if (key == "max_tokens" and type(value) is int and value >= 0) or (
            key != "max_tokens" and type(value) is bool
        ):
            safe_reasoning[key] = value
    if safe_reasoning:
        result["reasoning"] = safe_reasoning
    additional = effective.get("additionalModelRequestFields")
    additional = additional if isinstance(additional, dict) else {}
    thinking = (
        effective.get("thinking")
        or config.get("thinkingConfig")
        or additional.get("thinking")
    )
    if isinstance(thinking, dict):
        safe_thinking = {}
        for key in (
            "type",
            "budget_tokens",
            "thinkingBudget",
            "includeThoughts",
            "thinkingLevel",
        ):
            value = thinking.get(key)
            if (
                (key == "type" and value in ("enabled", "disabled", "adaptive"))
                or (
                    key == "thinkingLevel"
                    and value in ("minimal", "low", "medium", "high")
                )
                or (key in ("budget_tokens", "thinkingBudget") and type(value) is int)
                or (key == "includeThoughts" and type(value) is bool)
            ):
                safe_thinking[key] = value
        if safe_thinking:
            result["thinking"] = safe_thinking
    for key in ("enable_thinking", "thinking_budget"):
        value = effective.get(key)
        if (key == "enable_thinking" and type(value) is bool) or (
            key == "thinking_budget" and type(value) is int and value >= 0
        ):
            result[key] = value
    if openrouter and "provider" in effective:
        try:
            result["provider"] = validate_provider_preferences(effective["provider"])
        except ValueError:
            pass
    return result


def public_upstream_summary(value: Any) -> dict[str, Any] | None:
    """Project stored diagnostics defensively for exports and restricted readers."""
    if not isinstance(value, dict) or not value:
        return None
    body = {
        key: value[key]
        for key in (
            "model",
            "max_tokens",
            "reasoning",
            "reasoning_effort",
            "thinking",
            "enable_thinking",
            "thinking_budget",
            "provider",
        )
        if key in value
    }
    body["response_format"] = value.get("output_format")
    result = summarize_upstream_request(
        body,
        schema_in_instructions=value.get("schema_in_instructions") is True,
        openrouter=True,
    )
    count = value.get("tool_count")
    if type(count) is int and count >= 0:
        result["tool_count"] = count
    else:
        result.pop("tool_count", None)
    for key in ("output_format", "schema_in_instructions"):
        if key not in value:
            result.pop(key, None)
    return result
