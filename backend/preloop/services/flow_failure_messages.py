"""Messages for failures raised by the flow controller and runtime adapters.

Keep the message and category together so changing user-facing wording cannot
silently remove classification. Templates accept named format arguments.
"""

NATIVE_RESULT_SCHEMA_REQUIRED = (
    "native_result requires an enabled result_schema. "
    "Set a result schema and enable result validation, or disable native_result."
)
AGENT_REPORTED_FAILURE = (
    "Agent reported an explicit failure in result.json ({field}={value!r})."
)
#: Longest agent-written summary carried into an execution's error message.
AGENT_SUMMARY_MAX_CHARS = 1000


def agent_reported_failure_message(result: object, field: str, value: object) -> str:
    """Return the agent-reported failure message, with the agent's summary.

    Args:
        result: The agent's result.json document.
        field: The field that classified the failure (``status``/``verdict``).
        value: That field's value.

    Returns:
        ``AGENT_REPORTED_FAILURE`` followed by the agent's own ``summary``
        (or ``reason``/``error``/``message``) when it wrote one.
    """
    message = AGENT_REPORTED_FAILURE.format(field=field, value=value)
    if isinstance(result, dict):
        for key in ("summary", "reason", "error", "message"):
            text = result.get(key)
            if isinstance(text, str) and text.strip():
                summary = text.strip()[:AGENT_SUMMARY_MAX_CHARS]
                return f"{message} {summary}"
    return message


AGENT_TOOL_LOOP = (
    "Execution stopped after detecting a repeated tool loop: {tools} repeated "
    "at least {repetitions} times with identical arguments."
)

RUNNER_UNAVAILABLE = (
    "No runner in pool {pool!r} picked up this execution after {wait_seconds} seconds. "
    "Check that a runner is online and eligible for this pool."
)
CI_AUTHORIZATION_DENIED = "Restricted CI authorization denied"
CI_FLOW_DENIED = "Restricted CI flow denied"
CI_REVIEW_DENIED = "Restricted CI review denied"
HOOK_CREDENTIAL_UNBOUND = (
    "Token is not bound to a managed agent or active flow execution"
)
HOOK_BINDING_INVALID = "Read-only native permission credential binding is invalid"

# Prefixes also recognize executions written before these messages were extended.
FAILURE_MESSAGE_CATEGORIES: dict[str, str] = {
    NATIVE_RESULT_SCHEMA_REQUIRED: "flow_config",
    AGENT_REPORTED_FAILURE: "agent_reported_failure",
    RUNNER_UNAVAILABLE: "runner_unavailable",
    AGENT_TOOL_LOOP: "agent_tool_loop",
    CI_AUTHORIZATION_DENIED: "ci_authorization",
    CI_FLOW_DENIED: "ci_authorization",
    CI_REVIEW_DENIED: "ci_authorization",
    HOOK_CREDENTIAL_UNBOUND: "hook_binding_invalid",
    HOOK_BINDING_INVALID: "hook_binding_invalid",
}
