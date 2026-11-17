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
