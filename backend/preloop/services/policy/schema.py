"""Pydantic models for YAML-based policy definitions.

This module defines the schema for declarative policy-as-code configuration
for MCP governance. Policies can be defined in YAML/JSON files and imported
via the API to configure:

- MCP servers
- Approval workflows
- Tool configurations with conditions
- Default behaviors

Example YAML:
    version: "1.0"
    metadata:
      name: "Production Security Policy"
      description: "Strict approval requirements for production tools"

    mcp_servers:
      - name: "github-mcp"
        url: "https://mcp.github.com"
        transport: "streamable-http"
        auth_type: "bearer"

    approval_workflows:
      - name: "high-risk"
        timeout_seconds: 300
        require_reason: true
        approvals_required: 1

    tools:
      - name: "execute_command"
        source: "builtin"
        enabled: true
        approval_workflow: "high-risk"
        conditions:
          - expression: "args.command.contains('rm -rf')"
            action: "require_approval"

    model_io:
      - id: deny-pii-in-prompts
        target: model.request
        detectors:
          pii:
            types: [email, phone, credit_card]
        conditions:
          - expression: "pii.found == true"
            action: deny

    defaults:
      unknown_tools: "deny"
      require_approval_for_new_tools: true
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PolicyVersion(str, Enum):
    """Supported policy schema versions."""

    V1_0 = "1.0"


class PolicyMetadata(BaseModel):
    """Metadata for a policy definition.

    Attributes:
        name: Human-readable name for the policy.
        description: Optional description of the policy's purpose.
        author: Optional author name or email.
        created_at: Optional creation timestamp (auto-populated on export).
        tags: Optional list of tags for categorization.
    """

    name: str = Field(..., description="Human-readable name for the policy")
    description: Optional[str] = Field(
        None, description="Description of the policy's purpose"
    )
    author: Optional[str] = Field(None, description="Author name or email")
    created_at: Optional[datetime] = Field(
        None, description="Creation timestamp (auto-populated on export)"
    )
    tags: Optional[List[str]] = Field(None, description="Tags for categorization")


class MCPServerAuthType(str, Enum):
    """Authentication types for MCP servers."""

    NONE = "none"
    BEARER = "bearer"
    API_KEY = "api_key"
    OAUTH = "oauth"


class MCPServerTransport(str, Enum):
    """Transport types for MCP servers."""

    HTTP_STREAMING = "http-streaming"
    STREAMABLE_HTTP = "streamable-http"
    STDIO = "stdio"
    SSE = "sse"


class MCPServerDefinition(BaseModel):
    """MCP server definition in policy YAML.

    Attributes:
        name: Unique name for this server (used as reference in tools).
        url: Server URL endpoint.
        transport: Transport protocol to use.
        auth_type: Authentication type.
        auth_config: Authentication configuration (secrets should use env var refs).
    """

    name: str = Field(..., description="Unique name for this MCP server")
    url: str = Field(..., description="Server URL endpoint")
    transport: MCPServerTransport = Field(
        MCPServerTransport.STREAMABLE_HTTP, description="Transport protocol"
    )
    auth_type: MCPServerAuthType = Field(
        MCPServerAuthType.NONE, description="Authentication type"
    )
    auth_config: Optional[Dict[str, Any]] = Field(
        None,
        description="Auth configuration. Use ${ENV_VAR} for secrets.",
    )

    model_config = ConfigDict(use_enum_values=True)


class ApprovalWorkflowType(str, Enum):
    """Types of approval workflows."""

    SIMPLE = "simple"
    MULTI_STAGE = "multi_stage"
    CONSENSUS = "consensus"


class ApprovalWorkflowDefinition(BaseModel):
    """Approval workflow definition in policy YAML.

    Note: notification_channels is no longer used in the schema. Approvers
    configure their own notification preferences in user settings.

    Attributes:
        name: Unique name for this policy (used as reference in tools).
        description: Optional description of the policy.
        timeout_seconds: How long to wait for approval before timing out.
        require_reason: Whether approver must provide a reason.
        is_default: Whether this is the default policy for the account.
        workflow_type: Type of approval workflow.
        approvals_required: Number of approvals needed (quorum).
        approver_users: List of usernames who can approve.
        approver_teams: List of team names whose members can approve.
        escalation_users: Users to escalate to on timeout.
        escalation_teams: Teams to escalate to on timeout.
        channel_configs: Per-channel configuration.
        approval_type: Type of approval - 'standard' for human or 'ai_driven' for AI.
        ai_model: AI model to use for evaluation (required if ai_driven).
        ai_guidelines: Guidelines for the AI to follow when making decisions.
        ai_context: Additional context for the AI (examples, domain knowledge).
        ai_confidence_threshold: Minimum confidence for AI to auto-decide (0.0-1.0).
        ai_fallback_behavior: What to do when AI is uncertain.
        escalation_workflow: Policy to escalate to when AI is uncertain.
    """

    name: str = Field(..., description="Unique name for this policy")
    description: Optional[str] = Field(None, description="Policy description")
    timeout_seconds: int = Field(
        300, ge=30, le=86400, description="Approval timeout in seconds (30s-24h)"
    )
    require_reason: bool = Field(
        False, description="Whether approver must provide a reason"
    )
    is_default: bool = Field(False, description="Whether this is the default policy")
    workflow_type: ApprovalWorkflowType = Field(
        ApprovalWorkflowType.SIMPLE, description="Approval workflow type"
    )
    approvals_required: int = Field(
        1, ge=1, le=10, description="Number of approvals required"
    )
    # Reference by username/team name (resolved to IDs on import)
    approver_users: Optional[List[str]] = Field(
        None, description="Usernames who can approve"
    )
    approver_teams: Optional[List[str]] = Field(
        None, description="Team names whose members can approve"
    )
    escalation_users: Optional[List[str]] = Field(
        None, description="Usernames to escalate to on timeout"
    )
    escalation_teams: Optional[List[str]] = Field(
        None, description="Team names to escalate to on timeout"
    )
    channel_configs: Optional[Dict[str, Any]] = Field(
        None, description="Per-channel configuration"
    )

    # AI-driven approval settings
    approval_type: Literal["standard", "ai_driven"] = Field(
        "standard",
        description="Type of approval: 'standard' for human approvers, 'ai_driven' for AI evaluation",
    )
    ai_model: Optional[str] = Field(
        None,
        description="AI model to use for evaluation (e.g., 'claude-sonnet-4.7', 'gpt-5.4')",
    )
    ai_guidelines: Optional[str] = Field(
        None,
        description="Guidelines for the AI to follow when making decisions",
    )
    ai_context: Optional[Dict[str, Any]] = Field(
        None,
        description="Additional context for the AI (e.g., examples, domain knowledge)",
    )
    ai_confidence_threshold: float = Field(
        0.8,
        ge=0.0,
        le=1.0,
        description="Minimum confidence score for AI to auto-decide (0.0-1.0)",
    )
    ai_fallback_behavior: Literal["escalate", "approve", "deny"] = Field(
        "escalate",
        description="What to do when AI is uncertain: escalate to humans, auto-approve, or auto-deny",
    )
    escalation_workflow: Optional[str] = Field(
        None,
        description="Name of policy to escalate to when AI is uncertain (for fallback_behavior='escalate')",
    )
    async_approval: bool = Field(
        False,
        description="When enabled, tool calls return immediately and agents poll for approval status",
    )

    model_config = ConfigDict(use_enum_values=True)

    @model_validator(mode="after")
    def validate_ai_driven_settings(self) -> "ApprovalWorkflowDefinition":
        """Validate AI-driven approval workflow settings."""
        if self.approval_type == "ai_driven":
            if not self.ai_model:
                raise ValueError(
                    "ai_model is required when approval_type is 'ai_driven'"
                )
        return self


class ConditionAction(str, Enum):
    """Actions to take when a condition matches."""

    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"
    ALLOW = "allow"


class ConditionType(str, Enum):
    """Type of condition expression.

    This enum supports the open core licensing model:

    - SIMPLE (open source): Basic comparisons using Python-like syntax.
      Supports operators: ==, !=, >, <, >=, <=
      Examples:
        - "args.amount > 500"
        - "args.recipient == 'bob'"
        - "args.priority != 'low'"

    - CEL (enterprise): Full CEL (Common Expression Language) expressions
      with advanced functions and capabilities.
      Examples:
        - "args.command.contains('rm -rf')"
        - "args.path.startsWith('/etc/')"
        - "args.tags.exists(t, t == 'production')"
        - "args.amount > 1000 && args.approved == false"
    """

    SIMPLE = "simple"
    CEL = "cel"


class ToolCondition(BaseModel):
    """Condition for when to apply actions to tool invocations.

    Supports two types of conditions for the open core model:

    Simple conditions (open source):
        Basic comparisons using Python-like syntax. These are evaluated
        using a lightweight parser that supports basic operators.

        Supported operators: ==, !=, >, <, >=, <=

        Examples:
            - "args.amount > 500"
            - "args.recipient == 'bob'"
            - "args.count <= 10"

    CEL conditions (enterprise):
        Full CEL (Common Expression Language) expressions with advanced
        functions like contains(), startsWith(), endsWith(), exists(), etc.

        Examples:
            - "args.command.contains('rm -rf')"
            - "args.path.startsWith('/etc/')"
            - "args.environment == 'production' && args.force == true"
            - "args.tags.exists(t, t == 'sensitive')"

    Attributes:
        expression: Expression to evaluate against tool arguments.
        action: Action to take when condition matches.
        condition_type: Type of expression - 'simple' (open source) or 'cel' (enterprise).
        description: Optional human-readable description.
    """

    expression: str = Field(..., description="Expression to evaluate against tool args")
    action: ConditionAction = Field(
        ConditionAction.REQUIRE_APPROVAL, description="Action when condition matches"
    )
    condition_type: ConditionType = Field(
        ConditionType.SIMPLE,
        description="Expression type: 'simple' (open source) or 'cel' (enterprise)",
    )
    description: Optional[str] = Field(
        None, description="Human-readable description of this condition"
    )

    model_config = ConfigDict(use_enum_values=True)

    @field_validator("expression")
    @classmethod
    def validate_expression_not_empty(cls, v: str) -> str:
        """Ensure expression is not empty."""
        if not v.strip():
            raise ValueError("Condition expression cannot be empty")
        return v.strip()


class ToolSource(str, Enum):
    """Source types for tools."""

    BUILTIN = "builtin"
    MCP = "mcp"
    HTTP = "http"
    AGENT = "agent"


def is_known_tool_source(source: str) -> bool:
    """Return True if source is a ToolSource value (case-insensitive)."""
    return source.lower() in {item.value for item in ToolSource}


class ToolDefinition(BaseModel):
    """Tool configuration definition in policy YAML.

    Attributes:
        name: Tool name (must match actual tool name).
        source: Source type or MCP server name.
        enabled: Whether the tool is enabled.
        approval_workflow: Name of approval workflow to use (reference).
        conditions: List of conditions for conditional behavior.
        description: Optional custom description override.
        custom_config: Additional tool-specific configuration.
    """

    name: str = Field(..., description="Tool name")
    source: str = Field(
        "builtin",
        description=(
            "Source: a ToolSource value ('builtin', 'mcp', 'http', 'agent') "
            "or an MCP server name"
        ),
    )
    enabled: bool = Field(True, description="Whether the tool is enabled")
    approval_workflow: Optional[str] = Field(
        None, description="Name of approval workflow to use"
    )
    conditions: Optional[List[ToolCondition]] = Field(
        None, description="Conditions for conditional behavior"
    )
    description: Optional[str] = Field(None, description="Custom description override")
    justification: Optional[Literal["optional", "required"]] = Field(
        None,
        description="Justification mode: 'optional' (agent may provide), 'required' (agent must provide)",
    )
    custom_config: Optional[Dict[str, Any]] = Field(
        None, description="Additional tool-specific configuration"
    )

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        """Validate source is either a known type or a custom MCP server name."""
        # Allow enum values and custom server names
        if v.lower() in [e.value for e in ToolSource]:
            return v.lower()
        # Custom MCP server names are allowed
        return v


class UnknownToolsPolicy(str, Enum):
    """Policy for handling unknown/new tools."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ModelIOTarget(str, Enum):
    """Stable rule targets for model request and response payloads."""

    REQUEST = "model.request"
    RESPONSE = "model.response"


class DetectorTimeoutFailMode(str, Enum):
    """Fail mode when a detector exceeds its hard timeout.

    Default is ``deny`` (fail closed). Set ``allow`` to skip the timed-out
    rule and continue evaluation.
    """

    ALLOW = "allow"
    DENY = "deny"


SUPPORTED_PII_TYPES = ("email", "phone", "credit_card")


class PIIDetectorConfig(BaseModel):
    """Deterministic PII detector configuration.

    ``types`` selects which entity recognizers run. Unknown types are
    rejected at schema validation. Third-party recognizers are out of
    scope and must stay off by default.
    """

    types: List[str] = Field(
        default_factory=lambda: list(SUPPORTED_PII_TYPES),
        description="PII entity types to scan (email, phone, credit_card)",
    )

    @field_validator("types")
    @classmethod
    def validate_pii_types(cls, value: List[str]) -> List[str]:
        """Reject unknown PII types so YAML cannot silently skip a scan."""
        if not value:
            raise ValueError("pii.types must not be empty")
        unknown = [item for item in value if item not in SUPPORTED_PII_TYPES]
        if unknown:
            raise ValueError(
                f"Unknown PII types: {unknown}. Supported: {list(SUPPORTED_PII_TYPES)}"
            )
        return list(dict.fromkeys(value))


class ModerationDetectorConfig(BaseModel):
    """Moderation detector configuration.

    ``backend`` names a registered checker. The built-in ``local`` backend
    is a keyword ruleset and works without a live provider. Tests register
    a ``fake`` backend.
    """

    backend: str = Field(
        "local",
        description="Registered moderation backend name (default: local)",
    )


class ModelIODetectors(BaseModel):
    """Detectors enabled for one model I/O rule.

    A detector runs only when this block enables it or when a condition
    expression references its attributes (pii., injection., moderation.).
    """

    pii: Optional[Union[bool, PIIDetectorConfig]] = Field(
        None, description="Enable PII scan, optionally with entity types"
    )
    injection: Optional[bool] = Field(
        None, description="Enable prompt-injection heuristics"
    )
    moderation: Optional[Union[bool, ModerationDetectorConfig]] = Field(
        None, description="Enable moderation check"
    )


class ModelIORule(BaseModel):
    """Content policy rule targeting model.request or model.response.

    Conditions use the same simple/CEL style as tool rules. Documented
    attributes include model.id, model.provider, model.name, session.id,
    request.text, response.text, pii.found, pii.types_found,
    injection.score, injection.matched_patterns, moderation.flagged,
    and moderation.categories.
    """

    id: str = Field(..., min_length=1, description="Stable rule identifier")
    target: ModelIOTarget = Field(..., description="model.request or model.response")
    enabled: bool = Field(True, description="Whether this rule is evaluated")
    description: Optional[str] = Field(None, description="Human-readable description")
    approval_workflow: Optional[str] = Field(
        None, description="Approval workflow name for require_approval"
    )
    detectors: Optional[ModelIODetectors] = Field(
        None, description="Detectors to run for this rule"
    )
    detector_timeout_ms: int = Field(
        500,
        ge=50,
        le=30000,
        description="Hard timeout for detectors on this rule",
    )
    on_detector_timeout: DetectorTimeoutFailMode = Field(
        DetectorTimeoutFailMode.DENY,
        description="Fail mode when detectors time out (default deny)",
    )
    conditions: List[ToolCondition] = Field(
        ...,
        min_length=1,
        description="First matching condition wins, same as tools",
    )

    model_config = ConfigDict(use_enum_values=True)

    @field_validator("id")
    @classmethod
    def validate_id_not_empty(cls, value: str) -> str:
        """Rule ids are used in audit and approval tickets."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("model_io rule id cannot be empty")
        return stripped


class DefaultsDefinition(BaseModel):
    """Default behaviors for the policy.

    Attributes:
        unknown_tools: How to handle tools not explicitly configured.
        require_approval_for_new_tools: Require approval for newly discovered tools.
        default_approval_workflow: Default approval workflow for tools requiring approval.
        inherit_from_parent: Whether to inherit settings from parent/global policy.
    """

    unknown_tools: UnknownToolsPolicy = Field(
        UnknownToolsPolicy.ALLOW,
        description="How to handle tools not explicitly configured",
    )
    require_approval_for_new_tools: bool = Field(
        False, description="Require approval for newly discovered tools"
    )
    default_approval_workflow: Optional[str] = Field(
        None, description="Default approval workflow name"
    )
    inherit_from_parent: bool = Field(
        True, description="Whether to inherit from parent/global policy"
    )

    model_config = ConfigDict(use_enum_values=True)


class PolicyDocument(BaseModel):
    """Complete policy document schema.

    This is the root model for YAML/JSON policy files.

    Attributes:
        version: Schema version for compatibility checking.
        metadata: Policy metadata (name, description, etc.).
        mcp_servers: List of MCP server definitions.
        approval_workflows: List of approval workflow definitions.
        tools: List of tool configuration definitions.
        model_io: List of model request/response content policy rules.
        defaults: Default behavior settings.
    """

    version: PolicyVersion = Field(
        PolicyVersion.V1_0, description="Policy schema version"
    )
    metadata: PolicyMetadata = Field(..., description="Policy metadata")
    mcp_servers: Optional[List[MCPServerDefinition]] = Field(
        None, description="MCP server definitions"
    )
    approval_workflows: Optional[List[ApprovalWorkflowDefinition]] = Field(
        None, description="Approval workflow definitions"
    )
    tools: Optional[List[ToolDefinition]] = Field(
        None, description="Tool configuration definitions"
    )
    model_io: Optional[List[ModelIORule]] = Field(
        None, description="Model request and response content policy rules"
    )
    defaults: Optional[DefaultsDefinition] = Field(
        None, description="Default behavior settings"
    )

    model_config = ConfigDict(use_enum_values=True)

    @model_validator(mode="after")
    def validate_references(self) -> "PolicyDocument":
        """Validate that all references are resolvable within the document."""
        # Collect defined names
        mcp_server_names = set()
        if self.mcp_servers:
            for server in self.mcp_servers:
                if server.name in mcp_server_names:
                    raise ValueError(f"Duplicate MCP server name: '{server.name}'")
                mcp_server_names.add(server.name)

        policy_names = set()
        if self.approval_workflows:
            for policy in self.approval_workflows:
                if policy.name in policy_names:
                    raise ValueError(
                        f"Duplicate approval workflow name: '{policy.name}'"
                    )
                policy_names.add(policy.name)

        # Validate tool references
        if self.tools:
            for tool in self.tools:
                # Check approval workflow references
                if (
                    tool.approval_workflow
                    and tool.approval_workflow not in policy_names
                ):
                    raise ValueError(
                        f"Tool '{tool.name}' references unknown approval workflow "
                        f"'{tool.approval_workflow}'. Available policies: {policy_names}"
                    )

                # Native sources (builtin, mcp, http, agent) are not server names.
                source_lower = tool.source.lower()
                if not is_known_tool_source(source_lower):
                    # It's a custom MCP server name reference
                    if source_lower not in {s.lower() for s in mcp_server_names}:
                        raise ValueError(
                            f"Tool '{tool.name}' references unknown MCP server "
                            f"'{tool.source}'. Available servers: {mcp_server_names}"
                        )

        if self.model_io:
            model_io_ids: set[str] = set()
            for rule in self.model_io:
                if rule.id in model_io_ids:
                    raise ValueError(f"Duplicate model_io rule id: '{rule.id}'")
                model_io_ids.add(rule.id)
                if (
                    rule.approval_workflow
                    and rule.approval_workflow not in policy_names
                ):
                    raise ValueError(
                        f"model_io rule '{rule.id}' references unknown approval "
                        f"workflow '{rule.approval_workflow}'. "
                        f"Available policies: {policy_names}"
                    )

        # Validate default approval workflow reference
        if self.defaults and self.defaults.default_approval_workflow:
            if self.defaults.default_approval_workflow not in policy_names:
                raise ValueError(
                    f"Default approval workflow '{self.defaults.default_approval_workflow}' "
                    f"not found. Available policies: {policy_names}"
                )

        # Validate escalation_workflow references in AI-driven policies
        if self.approval_workflows:
            for policy in self.approval_workflows:
                if (
                    policy.escalation_workflow
                    and policy.escalation_workflow not in policy_names
                ):
                    raise ValueError(
                        f"Approval workflow '{policy.name}' references unknown "
                        f"escalation_workflow '{policy.escalation_workflow}'. "
                        f"Available policies: {policy_names}"
                    )

        return self


# Export/Import result schemas


class PolicyValidationError(BaseModel):
    """Validation error details."""

    path: str = Field(..., description="JSON path to the error location")
    message: str = Field(..., description="Error message")
    value: Optional[Any] = Field(None, description="The invalid value")


class PolicyValidationResult(BaseModel):
    """Result of policy validation."""

    is_valid: bool = Field(..., description="Whether the policy is valid")
    errors: List[PolicyValidationError] = Field(
        default_factory=list, description="List of validation errors"
    )
    warnings: List[str] = Field(default_factory=list, description="Non-fatal warnings")


class PolicyDiffItem(BaseModel):
    """Single diff item between policies."""

    path: str = Field(..., description="JSON path to the changed item")
    operation: Literal["add", "remove", "modify"] = Field(
        ..., description="Type of change"
    )
    old_value: Optional[Any] = Field(None, description="Previous value")
    new_value: Optional[Any] = Field(None, description="New value")


class PolicyDiffResult(BaseModel):
    """Result of comparing two policies."""

    has_changes: bool = Field(..., description="Whether there are any changes")
    changes: List[PolicyDiffItem] = Field(
        default_factory=list, description="List of changes"
    )
    summary: str = Field(..., description="Human-readable summary of changes")


class PolicyImportResult(BaseModel):
    """Result of importing a policy."""

    success: bool = Field(..., description="Whether import was successful")
    policy_name: str = Field(..., description="Name of the imported policy")
    mcp_servers_created: int = Field(0, description="Number of MCP servers created")
    mcp_servers_updated: int = Field(0, description="Number of MCP servers updated")
    policies_created: int = Field(0, description="Number of approval workflows created")
    policies_updated: int = Field(0, description="Number of approval workflows updated")
    tools_created: int = Field(0, description="Number of tool configs created")
    tools_updated: int = Field(0, description="Number of tool configs updated")
    tools_skipped: int = Field(
        0,
        description=(
            "Number of tools skipped due to missing server references "
            "(when skip_missing_servers=true)"
        ),
    )
    model_io_rules_applied: int = Field(
        0, description="Number of model I/O content rules applied"
    )
    warnings: List[str] = Field(default_factory=list, description="Non-fatal warnings")
    errors: List[str] = Field(default_factory=list, description="Errors that occurred")
