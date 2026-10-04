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
            types: [email, phone, credit_card, iban, employee_id]
        conditions:
          - expression: "pii.found == true"
            action: deny

    sensitive_data:
      detectors:
        locales: [de, nl]
        custom_patterns:
          - name: employee_id
            regex: 'EMP-\\d{6}'
        keywords:
          - name: codenames
            terms: ["Project Phoenix"]

    defaults:
      unknown_tools: "deny"
      require_approval_for_new_tools: true
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from preloop.services.sensitive_data.detectors import (
    BUILTIN_TYPE_IDS,
    TYPE_NAME_RE,
    UnsafePatternError,
    compile_keyword_pattern,
    compile_safe_regex,
    registered_type_ids,
)


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
    # Model I/O rules only: the call proceeds as with allow, and the match is
    # recorded and sent to the policy owners (#959). Tool conditions reject it.
    NOTIFY = "notify"


#: Actions a model I/O rule may use but a tool condition may not.
MODEL_IO_ONLY_ACTIONS = frozenset({ConditionAction.NOTIFY.value})


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

    @field_validator("conditions")
    @classmethod
    def validate_tool_condition_actions(
        cls, value: Optional[List[ToolCondition]]
    ) -> Optional[List[ToolCondition]]:
        """Reject actions only the model I/O evaluator implements.

        The tool evaluator has no notify branch, so a tool condition with
        ``notify`` would silently act as something else.
        """
        for condition in value or []:
            action = getattr(condition.action, "value", condition.action)
            if action in MODEL_IO_ONLY_ACTIONS:
                raise ValueError(
                    f"Tool conditions do not support action '{action}'. "
                    "Use it in a model_io rule."
                )
        return value


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


#: Types the ``pii`` detector scans when a rule does not list any. New
#: built-in types are opt-in so rules written before they existed keep
#: their behaviour.
SUPPORTED_PII_TYPES = ("email", "phone", "credit_card")

#: Every built-in sensitive-data type a rule may select.
BUILTIN_SENSITIVE_TYPES = BUILTIN_TYPE_IDS

#: Locales ``national_id`` understands.
SUPPORTED_NATIONAL_ID_LOCALES = ("us", "de", "uk", "fr", "nl")


def _validate_type_names(value: List[str], *, label: str) -> List[str]:
    """Accept built-in type ids and well-formed custom names.

    Custom names (patterns, keyword lists, registered detectors) are checked
    against the document's ``sensitive_data.detectors`` block by
    :class:`PolicyDocument`; here only the shape is enforced so a typo such
    as ``Email`` or ``credit-card`` is rejected with the full list.
    """
    if not value:
        raise ValueError(f"{label} must not be empty")
    unknown = [
        item
        for item in value
        if item not in BUILTIN_SENSITIVE_TYPES and not TYPE_NAME_RE.match(str(item))
    ]
    if unknown:
        raise ValueError(
            f"Unknown PII types: {unknown}. Supported: {list(BUILTIN_SENSITIVE_TYPES)} "
            "or the name of a custom pattern or keyword list"
        )
    return list(dict.fromkeys(value))


class PIIDetectorConfig(BaseModel):
    """Deterministic PII detector configuration.

    ``types`` selects which entity recognizers run: any built-in sensitive
    data type or the name of a custom pattern or keyword list declared under
    ``sensitive_data.detectors``. Unknown names are rejected at schema
    validation. Third-party recognizers must be registered explicitly and
    stay off by default.
    """

    types: List[str] = Field(
        default_factory=lambda: list(SUPPORTED_PII_TYPES),
        description=(
            "Sensitive data types to scan (built-in ids or custom entry names); "
            "default email, phone, credit_card"
        ),
    )

    @field_validator("types")
    @classmethod
    def validate_pii_types(cls, value: List[str]) -> List[str]:
        """Reject unknown PII types so YAML cannot silently skip a scan."""
        return _validate_type_names(value, label="pii.types")


class CustomPatternDefinition(BaseModel):
    """Account-defined regex sensitive-data type.

    The regex is compiled through a safety gate: a length cap and a static
    check that rejects nested quantifiers such as ``(a+)+``. It also runs
    under the rule's detector timeout.
    """

    name: str = Field(..., description="Type name used in rules (snake_case)")
    regex: str = Field(..., description="Python regular expression")
    flags: List[Literal["i", "m", "s", "x"]] = Field(
        default_factory=list, description="Regex flags: i, m, s, x"
    )
    description: Optional[str] = Field(None, description="Human-readable note")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Names are identifiers and cannot shadow a built-in type."""
        if not TYPE_NAME_RE.match(value or ""):
            raise ValueError(
                f"Custom pattern name {value!r} must match {TYPE_NAME_RE.pattern}"
            )
        if value in BUILTIN_SENSITIVE_TYPES:
            raise ValueError(f"Custom pattern name {value!r} is a built-in type")
        return value

    @model_validator(mode="after")
    def validate_regex(self) -> "CustomPatternDefinition":
        """Compile once so an unsafe pattern fails at validation time."""
        try:
            compile_safe_regex(self.regex, self.flags)
        except UnsafePatternError as exc:
            raise ValueError(f"custom_patterns[{self.name}]: {exc}") from exc
        return self


class KeywordListDefinition(BaseModel):
    """Account-defined keyword list matched on whole words only."""

    name: str = Field(..., description="Type name used in rules (snake_case)")
    terms: List[str] = Field(..., min_length=1, description="Terms to match")
    case_sensitive: bool = Field(False, description="Match case exactly")
    description: Optional[str] = Field(None, description="Human-readable note")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Names are identifiers and cannot shadow a built-in type."""
        if not TYPE_NAME_RE.match(value or ""):
            raise ValueError(
                f"Keyword list name {value!r} must match {TYPE_NAME_RE.pattern}"
            )
        if value in BUILTIN_SENSITIVE_TYPES:
            raise ValueError(f"Keyword list name {value!r} is a built-in type")
        return value

    @model_validator(mode="after")
    def validate_terms(self) -> "KeywordListDefinition":
        """Compile once so an empty or oversized list fails at validation."""
        try:
            compile_keyword_pattern(self.terms, self.case_sensitive)
        except ValueError as exc:
            raise ValueError(f"keywords[{self.name}]: {exc}") from exc
        return self


class SensitiveDataDetectorsConfig(BaseModel):
    """Account-wide detector configuration shared by every rule.

    ``types`` is the default scan set for rules that do not list their own.
    ``locales`` narrows ``national_id``. Custom patterns and keyword lists
    become selectable type names.
    """

    types: Optional[List[str]] = Field(
        None,
        description="Default types to scan; omit for every built-in and custom type",
    )
    locales: List[str] = Field(
        default_factory=list,
        description="national_id locales (us, de, uk, fr, nl); empty means all",
    )
    custom_patterns: List[CustomPatternDefinition] = Field(default_factory=list)
    keywords: List[KeywordListDefinition] = Field(default_factory=list)
    medical_record_number_pattern: Optional[str] = Field(
        None,
        description="Identifier regex that follows an MRN keyword (default digits)",
    )

    @field_validator("types")
    @classmethod
    def validate_types(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        """Shape check; names are cross-checked in ``validate_names``."""
        if value is None:
            return None
        return _validate_type_names(value, label="sensitive_data.detectors.types")

    @field_validator("locales")
    @classmethod
    def validate_locales(cls, value: List[str]) -> List[str]:
        """Only locales with a detector are accepted."""
        lowered = [str(item).lower() for item in value]
        unknown = [
            item for item in lowered if item not in SUPPORTED_NATIONAL_ID_LOCALES
        ]
        if unknown:
            raise ValueError(
                f"Unknown national_id locales: {unknown}. "
                f"Supported: {list(SUPPORTED_NATIONAL_ID_LOCALES)}"
            )
        return list(dict.fromkeys(lowered))

    @field_validator("medical_record_number_pattern")
    @classmethod
    def validate_mrn_pattern(cls, value: Optional[str]) -> Optional[str]:
        """Same safety gate as custom patterns."""
        if value is None:
            return None
        try:
            compile_safe_regex(value)
        except UnsafePatternError as exc:
            raise ValueError(f"medical_record_number_pattern: {exc}") from exc
        return value

    @model_validator(mode="after")
    def validate_names(self) -> "SensitiveDataDetectorsConfig":
        """Custom names are unique and ``types`` only names known entries."""
        seen: set[str] = set()
        for name in self.custom_names():
            if name in seen:
                raise ValueError(f"Duplicate sensitive_data detector name: '{name}'")
            seen.add(name)
        if self.types:
            registered = set(registered_type_ids())
            unknown = [
                item
                for item in self.types
                if item not in BUILTIN_SENSITIVE_TYPES
                and item not in seen
                and item not in registered
            ]
            if unknown:
                raise ValueError(
                    f"sensitive_data.detectors.types references unknown types "
                    f"{unknown}. Define them under custom_patterns or keywords."
                )
        return self

    def custom_names(self) -> List[str]:
        """Names of custom patterns and keyword lists."""
        return [item.name for item in self.custom_patterns] + [
            item.name for item in self.keywords
        ]

    def known_types(self) -> List[str]:
        """Built-in ids, registered detector names and this block's custom names."""
        return (
            list(BUILTIN_SENSITIVE_TYPES) + registered_type_ids() + self.custom_names()
        )


class SensitiveDataTarget(str, Enum):
    """Payloads a sensitive-data rule can watch."""

    TOOL_ARGS = "tool.args"
    TOOL_RESULT = "tool.result"
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"


#: Targets evaluated on the MCP tool path.
TOOL_TARGETS = frozenset(
    {SensitiveDataTarget.TOOL_ARGS.value, SensitiveDataTarget.TOOL_RESULT.value}
)
#: Targets compiled into model I/O rules.
MODEL_TARGETS = frozenset(
    {SensitiveDataTarget.MODEL_REQUEST.value, SensitiveDataTarget.MODEL_RESPONSE.value}
)

#: Actions a sensitive-data rule may take. ``allow`` is not one of them: a
#: rule exists to react to a match, and ``redact`` arrives with #1123.
SENSITIVE_DATA_RULE_ACTIONS = frozenset(
    {
        ConditionAction.NOTIFY.value,
        ConditionAction.DENY.value,
        ConditionAction.REQUIRE_APPROVAL.value,
    }
)


class SensitiveDataScope(BaseModel):
    """Where a rule applies. Empty lists mean every agent, tool or server.

    ``agents`` holds managed agent ids. ``tools`` holds client-visible tool
    names. ``servers`` holds MCP server names; builtin tools belong to the
    ``preloop-mcp`` pseudo server. Scopes only narrow tool targets; model
    targets honour ``agents`` and ignore the other two.
    """

    agents: List[str] = Field(default_factory=list, description="Managed agent ids")
    tools: List[str] = Field(default_factory=list, description="Tool names")
    servers: List[str] = Field(default_factory=list, description="MCP server names")

    def is_empty(self) -> bool:
        """True when the rule applies everywhere."""
        return not (self.agents or self.tools or self.servers)

    def matches(
        self,
        *,
        tool_name: Optional[str] = None,
        server_name: Optional[str] = None,
        managed_agent_id: Optional[str] = None,
    ) -> bool:
        """Scope check for one call. Each non-empty list must match."""
        if self.agents and (
            managed_agent_id is None or str(managed_agent_id) not in self.agents
        ):
            return False
        if self.tools and (tool_name is None or tool_name not in self.tools):
            return False
        if self.servers and (
            server_name is None
            or server_name.lower() not in {item.lower() for item in self.servers}
        ):
            return False
        return True


class SensitiveDataRule(BaseModel):
    """One rule: which payloads to scan, for which types, and what to do.

    Model targets compile to ordinary model I/O rules so there is one
    evaluator per path. Tool targets run on MCP tool arguments before the
    call and on the result after it.
    """

    id: str = Field(..., min_length=1, description="Stable rule identifier")
    enabled: bool = Field(True, description="Whether this rule is evaluated")
    description: Optional[str] = Field(None, description="Human-readable description")
    on: List[SensitiveDataTarget] = Field(
        ..., min_length=1, description="Targets: tool.args, tool.result, model.*"
    )
    scope: SensitiveDataScope = Field(
        default_factory=SensitiveDataScope, description="Agents, tools, servers"
    )
    types: Optional[List[str]] = Field(
        None,
        description="Types to scan; default the detectors block types or every type",
    )
    action: ConditionAction = Field(..., description="notify, deny or require_approval")
    approval_workflow: Optional[str] = Field(
        None, description="Approval workflow name for require_approval"
    )
    detector_timeout_ms: int = Field(
        500, ge=50, le=30000, description="Hard timeout for detectors on this rule"
    )
    on_detector_timeout: DetectorTimeoutFailMode = Field(
        DetectorTimeoutFailMode.DENY,
        description="Fail mode when detectors time out (default deny)",
    )

    model_config = ConfigDict(use_enum_values=True)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """Rule ids appear in audit rows and approval tickets."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("sensitive_data rule id cannot be empty")
        return stripped

    @field_validator("on")
    @classmethod
    def validate_targets(cls, value: List[Any]) -> List[Any]:
        """Deduplicate targets, keeping order."""
        return list(dict.fromkeys(value))

    @field_validator("types")
    @classmethod
    def validate_types(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        """Shape check; names are cross-checked by ``SensitiveDataConfig``."""
        if value is None:
            return None
        return _validate_type_names(value, label="sensitive_data.rules[].types")

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: Any) -> Any:
        """Only reactions to a match are meaningful here."""
        action = getattr(value, "value", value)
        if action not in SENSITIVE_DATA_RULE_ACTIONS:
            raise ValueError(
                f"sensitive_data rules do not support action '{action}'. "
                f"Supported: {sorted(SENSITIVE_DATA_RULE_ACTIONS)}"
            )
        return value

    def target_values(self) -> List[str]:
        """Targets as plain strings."""
        return [getattr(item, "value", item) for item in self.on]

    def has_tool_target(self) -> bool:
        """True when the rule watches tool arguments or results."""
        return any(item in TOOL_TARGETS for item in self.target_values())

    def has_model_target(self) -> bool:
        """True when the rule watches model requests or responses."""
        return any(item in MODEL_TARGETS for item in self.target_values())

    def action_value(self) -> str:
        """Action as a plain string."""
        return str(getattr(self.action, "value", self.action))


class SensitiveDataConfig(BaseModel):
    """Top-level ``sensitive_data`` block.

    ``detectors`` is shared configuration; ``rules`` react to matches on
    tool and model payloads. The redact action (#1123) and reference-only
    logging (#1124) extend this block.
    """

    detectors: Optional[SensitiveDataDetectorsConfig] = Field(
        None, description="Types, locales, custom patterns and keyword lists"
    )
    rules: List[SensitiveDataRule] = Field(
        default_factory=list, description="Rules over tool and model payloads"
    )

    @model_validator(mode="after")
    def validate_rules(self) -> "SensitiveDataConfig":
        """Rule ids are unique and rule types are declared."""
        known = self.known_types()
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"Duplicate sensitive_data rule id: '{rule.id}'")
            seen.add(rule.id)
            unknown = [item for item in (rule.types or []) if item not in known]
            if unknown:
                raise ValueError(
                    f"sensitive_data rule '{rule.id}' scans unknown types {unknown}. "
                    "Define them under sensitive_data.detectors.custom_patterns "
                    "or keywords."
                )
        return self

    def known_types(self) -> List[str]:
        """Built-in ids, registered detector names and configured custom names."""
        if self.detectors is None:
            return list(BUILTIN_SENSITIVE_TYPES) + registered_type_ids()
        return self.detectors.known_types()

    def default_types(self) -> List[str]:
        """Types a rule without its own list scans."""
        if self.detectors is not None and self.detectors.types:
            return list(self.detectors.types)
        return self.known_types()

    def types_for_rule(self, rule: SensitiveDataRule) -> List[str]:
        """Resolved type list for ``rule``."""
        return list(rule.types) if rule.types else self.default_types()

    def enabled_rules(self) -> List[SensitiveDataRule]:
        """Rules that are switched on, in document order."""
        return [rule for rule in self.rules if rule.enabled]

    def has_tool_rules(self) -> bool:
        """True when any enabled rule watches a tool target."""
        return any(rule.has_tool_target() for rule in self.enabled_rules())


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


def _rule_pii_types(rule: ModelIORule) -> List[str]:
    """Types a model_io rule's ``pii`` detector lists (empty when implicit)."""
    detectors = rule.detectors
    if detectors is None or not isinstance(detectors.pii, PIIDetectorConfig):
        return []
    return list(detectors.pii.types)


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
        sensitive_data: Detector configuration shared by model and tool rules.
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
    sensitive_data: Optional[SensitiveDataConfig] = Field(
        None, description="Sensitive data detectors shared by model and tool rules"
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

        known_sensitive_types = (
            self.sensitive_data.known_types()
            if self.sensitive_data is not None
            else list(BUILTIN_SENSITIVE_TYPES) + registered_type_ids()
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
                unknown_types = [
                    item
                    for item in _rule_pii_types(rule)
                    if item not in known_sensitive_types
                ]
                if unknown_types:
                    raise ValueError(
                        f"model_io rule '{rule.id}' scans unknown PII types "
                        f"{unknown_types}. Define them under "
                        "sensitive_data.detectors.custom_patterns or keywords."
                    )

        if self.sensitive_data is not None:
            for sensitive_rule in self.sensitive_data.rules:
                if (
                    sensitive_rule.approval_workflow
                    and sensitive_rule.approval_workflow not in policy_names
                ):
                    raise ValueError(
                        f"sensitive_data rule '{sensitive_rule.id}' references "
                        f"unknown approval workflow "
                        f"'{sensitive_rule.approval_workflow}'. "
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
    sensitive_data_applied: bool = Field(
        False, description="Whether the sensitive_data block was applied"
    )
    warnings: List[str] = Field(default_factory=list, description="Non-fatal warnings")
    errors: List[str] = Field(default_factory=list, description="Errors that occurred")
