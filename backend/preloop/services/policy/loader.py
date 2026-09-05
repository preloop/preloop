"""Policy loader for loading and validating YAML/JSON policy files.

This module provides functionality to:
- Load policy documents from YAML or JSON files/strings
- Validate policies against the schema
- Apply policies to the database (create/update entities)
- Export current configuration as a policy document
- Compute diffs between policies
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

import yaml
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.services.policy.schema import (
    ApprovalWorkflowDefinition,
    ConditionAction,
    ConditionType,
    DefaultsDefinition,
    MCPServerDefinition,
    ModelIORule,
    PolicyDiffItem,
    PolicyDiffResult,
    PolicyDocument,
    PolicyImportResult,
    PolicyMetadata,
    PolicyValidationError,
    PolicyValidationResult,
    PolicyVersion,
    ToolCondition,
    ToolDefinition,
    is_known_tool_source,
)

# CEL functions that indicate a complex expression requiring CEL evaluator
CEL_FUNCTIONS = [
    "contains(",
    "startsWith(",
    "endsWith(",
    "matches(",
    "exists(",
    "all(",
    "filter(",
    "map(",
    "size(",
    "type(",
    "has(",
    ".in(",
    " in ",
]

# CEL operators that indicate a complex expression requiring CEL evaluator
# These are distinct from simple Python-like comparisons (==, !=, >, <, >=, <=)
CEL_OPERATORS = [
    "&&",  # Logical AND
    "||",  # Logical OR
    "!",  # Logical NOT (but not !=)
    "?",  # Ternary operator (condition ? true : false)
    "[",  # List/map access
    "{",  # Map literal
]


def _detect_condition_type(expression: str) -> str:
    """Detect whether an expression is simple or requires CEL.

    Args:
        expression: The condition expression to analyze.

    Returns:
        'cel' if expression uses CEL-specific functions or operators, 'simple' otherwise.
    """
    if not expression:
        return "simple"

    expression_lower = expression.lower()

    # Check for CEL functions
    for func in CEL_FUNCTIONS:
        if func.lower() in expression_lower:
            return "cel"

    # Check for CEL operators
    for op in CEL_OPERATORS:
        if op == "!":
            # Match '!' but not '!=' (which is a simple operator)
            import re

            if re.search(r"(?<!=)!(?!=)", expression):
                return "cel"
        elif op in expression:
            return "cel"

    return "simple"


def _get_cel_validation_service():
    """Get the CEL validation service if available (lazy import to avoid circular deps).

    Returns:
        CEL validation service instance or None if not available.
    """
    try:
        from plugins.cel_validation.service import get_cel_validation_service

        return get_cel_validation_service()
    except ImportError:
        return None


def _validate_cel_expressions_in_policy(
    policy: "PolicyDocument",
) -> List["PolicyValidationError"]:
    """Validate all CEL expressions in a policy document.

    This function checks all CEL expressions in tool conditions for syntax errors.
    It's called during policy loading to catch errors early.

    Args:
        policy: The policy document to validate.

    Returns:
        List of validation errors for invalid CEL expressions.
    """
    errors: List[PolicyValidationError] = []

    # Get the CEL validation service (if available)
    cel_service = _get_cel_validation_service()
    if cel_service is None:
        # CEL validation plugin not available, skip validation
        return errors

    if policy.model_io:
        for rule_idx, rule in enumerate(policy.model_io):
            for cond_idx, condition in enumerate(rule.conditions or []):
                condition_type = condition.condition_type
                if isinstance(condition_type, str):
                    is_cel = condition_type == "cel"
                else:
                    is_cel = condition_type.value == "cel"
                if not is_cel:
                    detected_type = _detect_condition_type(condition.expression)
                    is_cel = detected_type == "cel"
                if not is_cel:
                    continue
                is_valid, error_message = cel_service.validate_cel_expression(
                    condition.expression
                )
                if not is_valid:
                    errors.append(
                        PolicyValidationError(
                            path=(
                                f"$.model_io[{rule_idx}].conditions"
                                f"[{cond_idx}].expression"
                            ),
                            message=f"Invalid CEL expression: {error_message}",
                            value=condition.expression,
                        )
                    )

    if not policy.tools:
        return errors

    for tool_idx, tool in enumerate(policy.tools):
        if not tool.conditions:
            continue

        for cond_idx, condition in enumerate(tool.conditions):
            # Only validate CEL expressions
            # Check both explicit 'cel' type and auto-detected type
            condition_type = condition.condition_type
            if isinstance(condition_type, str):
                is_cel = condition_type == "cel"
            else:
                is_cel = condition_type.value == "cel"

            # Also check if expression uses CEL functions even if marked as simple
            if not is_cel:
                detected_type = _detect_condition_type(condition.expression)
                is_cel = detected_type == "cel"

            if not is_cel:
                continue

            # Validate the CEL expression
            is_valid, error_message = cel_service.validate_cel_expression(
                condition.expression
            )

            if not is_valid:
                errors.append(
                    PolicyValidationError(
                        path=f"$.tools[{tool_idx}].conditions[{cond_idx}].expression",
                        message=f"Invalid CEL expression: {error_message}",
                        value=condition.expression,
                    )
                )

    return errors


logger = logging.getLogger(__name__)


class PolicyLoadError(Exception):
    """Exception raised when policy loading fails."""

    def __init__(
        self, message: str, errors: Optional[List[PolicyValidationError]] = None
    ):
        super().__init__(message)
        self.errors = errors or []


def load_policy_from_string(
    content: str,
    format: str = "yaml",
) -> Tuple[Optional[PolicyDocument], PolicyValidationResult]:
    """Load a policy document from a string.

    Args:
        content: The policy content as a string.
        format: Format of the content ('yaml' or 'json').

    Returns:
        Tuple of (PolicyDocument or None, PolicyValidationResult).
        If validation fails, PolicyDocument will be None.
    """
    errors: List[PolicyValidationError] = []
    warnings: List[str] = []

    # Parse the content
    try:
        if format.lower() == "json":
            data = json.loads(content)
        else:
            data = yaml.safe_load(content)
    except json.JSONDecodeError as e:
        errors.append(
            PolicyValidationError(
                path="$",
                message=f"Invalid JSON: {e.msg} at line {e.lineno}, column {e.colno}",
                value=None,
            )
        )
        return None, PolicyValidationResult(is_valid=False, errors=errors)
    except yaml.YAMLError as e:
        error_msg = str(e)
        if hasattr(e, "problem_mark"):
            mark = e.problem_mark
            error_msg = (
                f"Invalid YAML at line {mark.line + 1}, "
                f"column {mark.column + 1}: {e.problem}"
            )
        errors.append(
            PolicyValidationError(
                path="$",
                message=error_msg,
                value=None,
            )
        )
        return None, PolicyValidationResult(is_valid=False, errors=errors)

    if data is None:
        errors.append(
            PolicyValidationError(
                path="$",
                message="Empty policy document",
                value=None,
            )
        )
        return None, PolicyValidationResult(is_valid=False, errors=errors)

    # Validate against schema
    try:
        policy = PolicyDocument.model_validate(data)
    except ValidationError as e:
        for error in e.errors():
            path = ".".join(str(loc) for loc in error["loc"])
            errors.append(
                PolicyValidationError(
                    path=f"$.{path}" if path else "$",
                    message=error["msg"],
                    value=error.get("input"),
                )
            )
        return None, PolicyValidationResult(is_valid=False, errors=errors)

    # Add warnings for deprecated or unusual configurations
    if policy.tools:
        for tool in policy.tools:
            if tool.conditions and not tool.approval_workflow:
                warnings.append(
                    f"Tool '{tool.name}' has conditions but no approval_workflow set. "
                    "Conditions with 'require_approval' action will have no effect."
                )

    if policy.model_io:
        for rule in policy.model_io:
            has_require = any(
                (
                    condition.action == ConditionAction.REQUIRE_APPROVAL
                    or condition.action == "require_approval"
                )
                for condition in (rule.conditions or [])
            )
            if has_require and not rule.approval_workflow:
                warnings.append(
                    f"model_io rule '{rule.id}' has require_approval but no "
                    "approval_workflow set."
                )

    # Add warnings for AI-driven policies with escalate behavior but no escalation_workflow
    if policy.approval_workflows:
        for ap in policy.approval_workflows:
            if (
                ap.approval_type == "ai_driven"
                and ap.ai_fallback_behavior == "escalate"
                and not ap.escalation_workflow
            ):
                warnings.append(
                    f"AI-driven policy '{ap.name}' has fallback_behavior='escalate' "
                    "but no escalation_workflow specified. Requests will fail to escalate "
                    "when AI confidence is below threshold."
                )

    # Validate CEL expressions if the CEL validation plugin is available
    cel_errors = _validate_cel_expressions_in_policy(policy)
    if cel_errors:
        errors.extend(cel_errors)
        return None, PolicyValidationResult(
            is_valid=False, errors=errors, warnings=warnings
        )

    return policy, PolicyValidationResult(is_valid=True, errors=[], warnings=warnings)


def load_policy_from_file(
    file_path: str,
) -> Tuple[Optional[PolicyDocument], PolicyValidationResult]:
    """Load a policy document from a file.

    Args:
        file_path: Path to the policy file.

    Returns:
        Tuple of (PolicyDocument or None, PolicyValidationResult).

    Raises:
        PolicyLoadError: If the file cannot be read.
    """
    if not os.path.exists(file_path):
        return None, PolicyValidationResult(
            is_valid=False,
            errors=[
                PolicyValidationError(
                    path="$",
                    message=f"File not found: {file_path}",
                    value=None,
                )
            ],
        )

    # Determine format from extension
    ext = os.path.splitext(file_path)[1].lower()
    if ext in [".json"]:
        format = "json"
    elif ext in [".yaml", ".yml"]:
        format = "yaml"
    else:
        format = "yaml"  # Default to YAML

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except IOError as e:
        return None, PolicyValidationResult(
            is_valid=False,
            errors=[
                PolicyValidationError(
                    path="$",
                    message=f"Failed to read file: {e}",
                    value=None,
                )
            ],
        )

    return load_policy_from_string(content, format=format)


def export_policy_to_yaml(
    policy: PolicyDocument,
    account_name: Optional[str] = None,
    include_mcp_servers: bool = True,
) -> str:
    """Export a policy document to YAML string.

    Args:
        policy: The policy document to export.
        account_name: Optional account name for header comment.
        include_mcp_servers: Whether MCP servers were included (affects header comment).

    Returns:
        YAML string representation with header comments.
    """
    from datetime import datetime, timezone

    # Convert to dict, excluding None values for cleaner output
    # mode='json' ensures enums are serialized as their values, not Python objects
    data = policy.model_dump(exclude_none=True, mode="json")

    # Use safe_dump to avoid Python-specific YAML tags
    yaml_content = yaml.safe_dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )

    # Build header comment
    header_lines = [
        "# Preloop Policy Export",
        f"# Exported at: {datetime.now(timezone.utc).isoformat()}",
    ]

    if account_name:
        header_lines.append(f"# Account: {account_name}")

    header_lines.append("#")

    if include_mcp_servers:
        header_lines.extend(
            [
                "# NOTE: MCP server credentials have been redacted for security.",
                "# Configure auth_config before importing.",
            ]
        )
    else:
        header_lines.extend(
            [
                "# NOTE: MCP server definitions were excluded from this export.",
                "# Add mcp_servers section if needed.",
            ]
        )

    header_lines.append("")  # Empty line before content

    header = "\n".join(header_lines)
    return header + yaml_content


def export_policy_to_json(policy: PolicyDocument, indent: int = 2) -> str:
    """Export a policy document to JSON string.

    Args:
        policy: The policy document to export.
        indent: Indentation level for pretty printing.

    Returns:
        JSON string representation.
    """
    return policy.model_dump_json(exclude_none=True, indent=indent)


def compute_policy_diff(
    current: PolicyDocument,
    incoming: PolicyDocument,
) -> PolicyDiffResult:
    """Compute the diff between two policy documents.

    Args:
        current: The current/existing policy.
        incoming: The incoming/new policy.

    Returns:
        PolicyDiffResult with list of changes.
    """
    changes: List[PolicyDiffItem] = []

    # Helper to compare lists by name
    def diff_named_lists(
        path: str,
        current_items: Optional[List[Any]],
        incoming_items: Optional[List[Any]],
        name_field: str = "name",
    ) -> None:
        current_map = {
            getattr(item, name_field): item for item in (current_items or [])
        }
        incoming_map = {
            getattr(item, name_field): item for item in (incoming_items or [])
        }

        # Removed items
        for name in set(current_map.keys()) - set(incoming_map.keys()):
            changes.append(
                PolicyDiffItem(
                    path=f"{path}[name={name}]",
                    operation="remove",
                    old_value=current_map[name].model_dump(exclude_none=True),
                    new_value=None,
                )
            )

        # Added items
        for name in set(incoming_map.keys()) - set(current_map.keys()):
            changes.append(
                PolicyDiffItem(
                    path=f"{path}[name={name}]",
                    operation="add",
                    old_value=None,
                    new_value=incoming_map[name].model_dump(exclude_none=True),
                )
            )

        # Modified items
        for name in set(current_map.keys()) & set(incoming_map.keys()):
            current_dict = current_map[name].model_dump(exclude_none=True)
            incoming_dict = incoming_map[name].model_dump(exclude_none=True)
            if current_dict != incoming_dict:
                changes.append(
                    PolicyDiffItem(
                        path=f"{path}[name={name}]",
                        operation="modify",
                        old_value=current_dict,
                        new_value=incoming_dict,
                    )
                )

    # Compare metadata
    if current.metadata.model_dump(exclude_none=True) != incoming.metadata.model_dump(
        exclude_none=True
    ):
        changes.append(
            PolicyDiffItem(
                path="$.metadata",
                operation="modify",
                old_value=current.metadata.model_dump(exclude_none=True),
                new_value=incoming.metadata.model_dump(exclude_none=True),
            )
        )

    # Compare MCP servers
    diff_named_lists("$.mcp_servers", current.mcp_servers, incoming.mcp_servers)

    # Compare approval workflows
    diff_named_lists(
        "$.approval_workflows", current.approval_workflows, incoming.approval_workflows
    )

    # Compare tools
    diff_named_lists("$.tools", current.tools, incoming.tools)

    # Compare model I/O rules by id
    def _model_io_name(item: Any) -> str:
        return item.id

    current_map = {_model_io_name(item): item for item in (current.model_io or [])}
    incoming_map = {_model_io_name(item): item for item in (incoming.model_io or [])}
    for name in set(current_map.keys()) - set(incoming_map.keys()):
        changes.append(
            PolicyDiffItem(
                path=f"$.model_io[id={name}]",
                operation="remove",
                old_value=current_map[name].model_dump(exclude_none=True),
                new_value=None,
            )
        )
    for name in set(incoming_map.keys()) - set(current_map.keys()):
        changes.append(
            PolicyDiffItem(
                path=f"$.model_io[id={name}]",
                operation="add",
                old_value=None,
                new_value=incoming_map[name].model_dump(exclude_none=True),
            )
        )
    for name in set(current_map.keys()) & set(incoming_map.keys()):
        current_dict = current_map[name].model_dump(exclude_none=True)
        incoming_dict = incoming_map[name].model_dump(exclude_none=True)
        if current_dict != incoming_dict:
            changes.append(
                PolicyDiffItem(
                    path=f"$.model_io[id={name}]",
                    operation="modify",
                    old_value=current_dict,
                    new_value=incoming_dict,
                )
            )

    # Compare defaults
    current_defaults = (
        current.defaults.model_dump(exclude_none=True) if current.defaults else {}
    )
    incoming_defaults = (
        incoming.defaults.model_dump(exclude_none=True) if incoming.defaults else {}
    )
    if current_defaults != incoming_defaults:
        changes.append(
            PolicyDiffItem(
                path="$.defaults",
                operation="modify" if current_defaults else "add",
                old_value=current_defaults or None,
                new_value=incoming_defaults or None,
            )
        )

    # Generate summary
    add_count = sum(1 for c in changes if c.operation == "add")
    remove_count = sum(1 for c in changes if c.operation == "remove")
    modify_count = sum(1 for c in changes if c.operation == "modify")

    summary_parts = []
    if add_count:
        summary_parts.append(f"{add_count} addition(s)")
    if remove_count:
        summary_parts.append(f"{remove_count} removal(s)")
    if modify_count:
        summary_parts.append(f"{modify_count} modification(s)")

    summary = ", ".join(summary_parts) if summary_parts else "No changes"

    return PolicyDiffResult(
        has_changes=len(changes) > 0,
        changes=changes,
        summary=summary,
    )


def resolve_env_vars(value: Any) -> Any:
    """Recursively resolve environment variable references in values.

    Supports ${VAR_NAME} syntax for environment variable substitution.

    Args:
        value: The value to process (can be dict, list, or scalar).

    Returns:
        The value with environment variables resolved.
    """
    if isinstance(value, str):
        # Match ${VAR_NAME} pattern
        import re

        pattern = r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"

        def replacer(match: re.Match) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))

        return re.sub(pattern, replacer, value)
    elif isinstance(value, dict):
        return {k: resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env_vars(item) for item in value]
    else:
        return value


class MissingServerError:
    """Details about a missing MCP server reference."""

    def __init__(self, tool_name: str, server_name: str, suggestion: str):
        self.tool_name = tool_name
        self.server_name = server_name
        self.suggestion = suggestion

    def to_message(self) -> str:
        """Format as a user-friendly error message."""
        return (
            f"Tool '{self.tool_name}' references MCP server '{self.server_name}' "
            f"which is not configured. {self.suggestion}"
        )


class MissingWorkflowError:
    """Details about a missing approval workflow reference."""

    def __init__(self, tool_name: str, workflow_name: str, suggestion: str):
        self.tool_name = tool_name
        self.workflow_name = workflow_name
        self.suggestion = suggestion

    def to_message(self) -> str:
        """Format as a user-friendly error message."""
        return (
            f"Tool '{self.tool_name}' references approval workflow '{self.workflow_name}' "
            f"which is not defined. {self.suggestion}"
        )


class PolicyApplier:
    """Apply a policy document to the database.

    This class handles the creation and update of database entities
    based on a policy document.
    """

    def __init__(self, db: Session, account_id: Union[str, UUID]):
        """Initialize the policy applier.

        Args:
            db: SQLAlchemy database session.
            account_id: The account ID to apply the policy to.
        """
        self.db = db
        self.account_id = str(account_id)

        # Track created entities for rollback and reporting
        self._result = PolicyImportResult(
            success=False,
            policy_name="",
        )

        # Caches for lookups
        self._mcp_server_map: Dict[str, UUID] = {}
        self._policy_map: Dict[str, UUID] = {}

        # Track skipped tools for reporting
        self._skipped_tools: List[str] = []

    def apply(
        self,
        policy: PolicyDocument,
        dry_run: bool = False,
        resolve_env: bool = True,
        skip_missing_servers: bool = False,
    ) -> PolicyImportResult:
        """Apply a policy document to the database.

        Args:
            policy: The policy document to apply.
            dry_run: If True, validate only without making changes.
            resolve_env: If True, resolve environment variable references.
            skip_missing_servers: If True, skip tools that reference missing
                MCP servers instead of failing. Skipped tools are reported
                in warnings.

        Returns:
            PolicyImportResult with details of what was created/updated.
        """
        self._result.policy_name = policy.metadata.name
        self._skipped_tools = []

        try:
            # Resolve environment variables if requested
            if resolve_env:
                policy_dict = resolve_env_vars(policy.model_dump())
                policy = PolicyDocument.model_validate(policy_dict)

            # Pre-validation phase: check all references before making changes
            validation_errors = self._validate_references(policy, skip_missing_servers)
            if validation_errors:
                for error in validation_errors:
                    self._result.errors.append(error)
                return self._result

            # Apply in order: servers, policies, tools, defaults
            if policy.mcp_servers:
                self._apply_mcp_servers(policy.mcp_servers, dry_run)

            if policy.approval_workflows:
                self._apply_approval_workflows(policy.approval_workflows, dry_run)

            if policy.tools:
                self._apply_tools(policy.tools, dry_run, skip_missing_servers)

            if policy.model_io is not None:
                self._apply_model_io(policy.model_io, dry_run)

            if policy.defaults and not self._apply_defaults(policy.defaults, dry_run):
                return self._result

            if not dry_run:
                self.db.commit()

            self._result.success = True
            self._result.tools_skipped = len(self._skipped_tools)

        except Exception as e:
            logger.error(f"Failed to apply policy: {e}", exc_info=True)
            self._result.errors.append(str(e))
            if not dry_run:
                self.db.rollback()

        return self._result

    def _validate_references(
        self,
        policy: PolicyDocument,
        skip_missing_servers: bool = False,
    ) -> List[str]:
        """Validate all server and policy references before applying.

        This pre-check phase validates that:
        1. All MCP server references can be resolved (either defined in the
           policy file or already configured in the account)
        2. All approval workflow references can be resolved (either defined
           in the policy file or already configured in the account)

        Args:
            policy: The policy document to validate.
            skip_missing_servers: If True, don't error on missing servers
                (they will be skipped during apply).

        Returns:
            List of error messages. Empty list if validation passes.
        """
        from preloop.models.crud import crud_approval_workflow, crud_mcp_server

        errors: List[str] = []

        # Build set of servers defined in the policy file
        policy_servers = set()
        if policy.mcp_servers:
            policy_servers = {server.name.lower() for server in policy.mcp_servers}

        # Build set of policies defined in the policy file
        policy_approval_workflows = set()
        if policy.approval_workflows:
            policy_approval_workflows = {p.name for p in policy.approval_workflows}

        # Get existing servers from the database
        existing_servers = crud_mcp_server.get_active_by_account(
            self.db, account_id=self.account_id
        )
        existing_server_names = {s.name.lower() for s in existing_servers}
        all_available_servers = policy_servers | existing_server_names

        # Get existing policies from the database
        existing_workflows = crud_approval_workflow.get_multi_by_account(
            self.db, account_id=self.account_id
        )
        existing_workflow_names = {p.name for p in existing_workflows}
        all_available_workflows = policy_approval_workflows | existing_workflow_names

        # Validate tool references
        if policy.tools:
            missing_servers: List[MissingServerError] = []
            missing_workflows: List[MissingWorkflowError] = []

            for tool in policy.tools:
                # Check MCP server references
                source_lower = tool.source.lower()
                if not is_known_tool_source(source_lower):
                    # It's a custom MCP server name reference
                    if source_lower not in all_available_servers:
                        suggestion = self._get_server_suggestion(
                            tool.source, all_available_servers
                        )
                        missing_servers.append(
                            MissingServerError(
                                tool_name=tool.name,
                                server_name=tool.source,
                                suggestion=suggestion,
                            )
                        )

                # Check approval workflow references
                if tool.approval_workflow:
                    if tool.approval_workflow not in all_available_workflows:
                        suggestion = self._get_workflow_suggestion(
                            tool.approval_workflow, all_available_workflows
                        )
                        missing_workflows.append(
                            MissingWorkflowError(
                                tool_name=tool.name,
                                workflow_name=tool.approval_workflow,
                                suggestion=suggestion,
                            )
                        )

            # Handle missing servers
            if missing_servers:
                if skip_missing_servers:
                    # Add warnings instead of errors
                    for missing in missing_servers:
                        self._result.warnings.append(
                            f"Skipping tool '{missing.tool_name}': "
                            f"MCP server '{missing.server_name}' is not configured. "
                            f"Configure the server first to enable this tool."
                        )
                        self._skipped_tools.append(missing.tool_name)
                else:
                    # Add errors
                    for missing in missing_servers:
                        errors.append(missing.to_message())

            # Missing policies are always errors (can't be skipped)
            for missing in missing_workflows:
                errors.append(missing.to_message())

        if policy.model_io:
            for rule in policy.model_io:
                if rule.approval_workflow:
                    if rule.approval_workflow not in all_available_workflows:
                        suggestion = self._get_workflow_suggestion(
                            rule.approval_workflow, all_available_workflows
                        )
                        errors.append(
                            f"model_io rule '{rule.id}' references approval "
                            f"workflow '{rule.approval_workflow}' which is "
                            f"not defined. {suggestion}"
                        )

        # Validate defaults.default_approval_workflow
        if policy.defaults and policy.defaults.default_approval_workflow:
            if policy.defaults.default_approval_workflow not in all_available_workflows:
                suggestion = self._get_workflow_suggestion(
                    policy.defaults.default_approval_workflow, all_available_workflows
                )
                errors.append(
                    f"Default approval workflow '{policy.defaults.default_approval_workflow}' "
                    f"is not defined. {suggestion}"
                )

        return errors

    def _get_server_suggestion(self, server_name: str, available_servers: set) -> str:
        """Generate a helpful suggestion for missing server references.

        Args:
            server_name: The missing server name.
            available_servers: Set of available server names.

        Returns:
            A suggestion string for how to fix the issue.
        """
        if available_servers:
            available_list = ", ".join(sorted(available_servers))
            return (
                f"Either add the server to your policy file under 'mcp_servers', "
                f"or configure it in the console first. "
                f"Available servers: [{available_list}]"
            )
        return (
            "Add the server to your policy file under 'mcp_servers', "
            "or configure it in the console first."
        )

    def _get_workflow_suggestion(
        self, workflow_name: str, available_workflows: set
    ) -> str:
        """Generate a helpful suggestion for missing policy references.

        Args:
            workflow_name: The missing approval workflow name.
            available_workflows: Set of available policy names.

        Returns:
            A suggestion string for how to fix the issue.
        """
        if available_workflows:
            available_list = ", ".join(sorted(available_workflows))
            return (
                f"Either add the policy to your policy file under 'approval_workflows', "
                f"or configure it in the console first. "
                f"Available policies: [{available_list}]"
            )
        return (
            "Add the policy to your policy file under 'approval_workflows', "
            "or configure it in the console first."
        )

    def _apply_mcp_servers(
        self,
        servers: List[MCPServerDefinition],
        dry_run: bool,
    ) -> None:
        """Apply MCP server definitions."""
        from preloop.models.crud import crud_mcp_server
        from preloop.models.models.mcp_server import MCPServer

        for server_def in servers:
            # Check if server exists by name
            existing = crud_mcp_server.get_by_name(
                self.db, account_id=self.account_id, name=server_def.name
            )

            # Check if auth_config is a redaction marker (from exported snapshots)
            # If so, we should NOT overwrite existing credentials
            auth_config_is_redacted = (
                isinstance(server_def.auth_config, dict)
                and server_def.auth_config.get("redacted") is True
            )

            if existing:
                # Update existing server
                if not dry_run:
                    existing.url = server_def.url
                    existing.transport = server_def.transport
                    existing.auth_type = server_def.auth_type
                    # Only update auth_config if:
                    # 1. It's provided AND
                    # 2. It's NOT a redaction marker
                    # This prevents rollbacks from wiping credentials
                    if server_def.auth_config and not auth_config_is_redacted:
                        existing.auth_config = server_def.auth_config
                    elif auth_config_is_redacted:
                        logger.debug(
                            f"Skipping auth_config update for {server_def.name} "
                            "(redacted in snapshot, preserving existing credentials)"
                        )
                self._mcp_server_map[server_def.name] = existing.id
                self._result.mcp_servers_updated += 1
                logger.info(f"Updated MCP server: {server_def.name}")
            else:
                # Create new server
                if not dry_run:
                    # For new servers with redacted auth, set to None
                    # (user will need to configure credentials)
                    actual_auth_config = (
                        None if auth_config_is_redacted else server_def.auth_config
                    )
                    if auth_config_is_redacted:
                        logger.warning(
                            f"Creating MCP server {server_def.name} without credentials "
                            "(redacted in snapshot). Configure auth_config manually."
                        )
                    new_server = MCPServer(
                        account_id=self.account_id,
                        name=server_def.name,
                        url=server_def.url,
                        transport=server_def.transport,
                        auth_type=server_def.auth_type,
                        auth_config=actual_auth_config,
                        status="active",
                    )
                    self.db.add(new_server)
                    self.db.flush()  # Get the ID
                    self._mcp_server_map[server_def.name] = new_server.id
                self._result.mcp_servers_created += 1
                logger.info(f"Created MCP server: {server_def.name}")

    def _apply_approval_workflows(
        self,
        policies: List[ApprovalWorkflowDefinition],
        dry_run: bool,
    ) -> None:
        """Apply approval workflow definitions.

        Note: notification_channels is no longer used. Approvers configure their
        own notification preferences in user settings.

        Handles both standard (human) and AI-driven approval workflows.
        """
        from preloop.models.crud import crud_approval_workflow
        from preloop.models.models.tool_configuration import ApprovalWorkflow

        for policy_def in policies:
            # Map YAML approval_type to database approval_mode
            # The model has:
            # - approval_type: mechanism (slack, mattermost, webhook, manual)
            # - approval_mode: who approves (standard=human, ai_driven=AI)
            db_approval_mode = (
                "ai_driven" if policy_def.approval_type == "ai_driven" else "standard"
            )

            # Check if policy exists by name
            existing = crud_approval_workflow.get_by_name(
                self.db, account_id=self.account_id, name=policy_def.name
            )

            if existing:
                # Update existing policy
                if not dry_run:
                    existing.description = policy_def.description
                    existing.timeout_seconds = policy_def.timeout_seconds
                    existing.require_reason = policy_def.require_reason
                    existing.is_default = policy_def.is_default
                    existing.workflow_type = policy_def.workflow_type
                    existing.approvals_required = policy_def.approvals_required
                    existing.approval_mode = db_approval_mode  # standard or ai_driven
                    if policy_def.channel_configs:
                        existing.channel_configs = policy_def.channel_configs

                    # Update AI-driven settings
                    existing.ai_model = policy_def.ai_model
                    existing.ai_guidelines = policy_def.ai_guidelines
                    existing.ai_context = policy_def.ai_context
                    existing.ai_confidence_threshold = (
                        policy_def.ai_confidence_threshold
                    )
                    existing.ai_fallback_behavior = policy_def.ai_fallback_behavior
                    existing.async_approval_enabled = policy_def.async_approval
                    # Store escalation_workflow name for second-pass resolution
                    existing._pending_escalation_workflow = (
                        policy_def.escalation_workflow
                    )
                self._policy_map[policy_def.name] = existing.id
                self._result.policies_updated += 1
                logger.info(f"Updated approval workflow: {policy_def.name}")
            else:
                # Create new policy
                if not dry_run:
                    new_policy = ApprovalWorkflow(
                        account_id=self.account_id,
                        name=policy_def.name,
                        description=policy_def.description,
                        approval_type="manual",  # Mechanism: manual approval via UI
                        approval_mode=db_approval_mode,  # Who approves: standard or ai_driven
                        timeout_seconds=policy_def.timeout_seconds,
                        require_reason=policy_def.require_reason,
                        is_default=policy_def.is_default,
                        workflow_type=policy_def.workflow_type,
                        approvals_required=policy_def.approvals_required,
                        channel_configs=policy_def.channel_configs,
                        # AI-driven settings
                        ai_model=policy_def.ai_model,
                        ai_guidelines=policy_def.ai_guidelines,
                        ai_context=policy_def.ai_context,
                        ai_confidence_threshold=policy_def.ai_confidence_threshold,
                        ai_fallback_behavior=policy_def.ai_fallback_behavior,
                        async_approval_enabled=policy_def.async_approval,
                        # escalation_workflow_id resolved in second pass
                    )
                    # Store escalation_workflow name for second-pass resolution
                    new_policy._pending_escalation_workflow = (
                        policy_def.escalation_workflow
                    )
                    self.db.add(new_policy)
                    self.db.flush()
                    self._policy_map[policy_def.name] = new_policy.id
                self._result.policies_created += 1
                logger.info(f"Created approval workflow: {policy_def.name}")

        # Second pass: resolve escalation_workflow references
        if not dry_run:
            self._resolve_escalation_workflows(policies)

    def _resolve_escalation_workflows(
        self, policies: List[ApprovalWorkflowDefinition]
    ) -> None:
        """Resolve escalation_workflow names to IDs (second pass).

        This is called after all policies are created/updated, so we can
        resolve cross-references between policies.
        """
        from preloop.models.crud import crud_approval_workflow

        for policy_def in policies:
            if not policy_def.escalation_workflow:
                continue

            # Get the policy we just created/updated
            policy = crud_approval_workflow.get_by_name(
                self.db, account_id=self.account_id, name=policy_def.name
            )
            if not policy:
                continue

            # Look up the escalation workflow by name
            escalation_workflow_id = self._policy_map.get(
                policy_def.escalation_workflow
            )
            if not escalation_workflow_id:
                # Try to find it in the database
                escalation_workflow = crud_approval_workflow.get_by_name(
                    self.db,
                    account_id=self.account_id,
                    name=policy_def.escalation_workflow,
                )
                if escalation_workflow:
                    escalation_workflow_id = escalation_workflow.id

            if escalation_workflow_id:
                policy.escalation_workflow_id = escalation_workflow_id
                logger.info(
                    f"Resolved escalation workflow '{policy_def.escalation_workflow}' "
                    f"for policy '{policy_def.name}'"
                )
            else:
                logger.warning(
                    f"Escalation policy '{policy_def.escalation_workflow}' not found "
                    f"for policy '{policy_def.name}'"
                )
                self._result.warnings.append(
                    f"Escalation policy '{policy_def.escalation_workflow}' not found "
                    f"for policy '{policy_def.name}'"
                )

    def _apply_tools(
        self,
        tools: List[ToolDefinition],
        dry_run: bool,
        skip_missing_servers: bool = False,
    ) -> None:
        """Apply tool configuration definitions.

        Args:
            tools: List of tool definitions from the policy.
            dry_run: If True, don't make database changes.
            skip_missing_servers: If True, skip tools that reference
                missing MCP servers (they were validated in pre-check).
        """
        from preloop.models.crud import crud_tool_configuration
        from preloop.models.models.tool_configuration import ToolConfiguration

        for tool_def in tools:
            # Skip tools that were marked for skipping during validation
            if tool_def.name in self._skipped_tools:
                logger.info(
                    f"Skipping tool '{tool_def.name}' due to missing MCP server"
                )
                continue
            # Determine tool source and MCP server ID
            source_lower = tool_def.source.lower()
            if is_known_tool_source(source_lower) and source_lower != "mcp":
                tool_source = source_lower
                mcp_server_id = None
            elif source_lower == "mcp":
                # Generic MCP source (requires mcp_server_id to be set elsewhere)
                tool_source = "mcp"
                mcp_server_id = None
                self._result.warnings.append(
                    f"Tool '{tool_def.name}' has source 'mcp' but no server specified. "
                    "Use a specific server name instead."
                )
            else:
                # It's a server name reference
                tool_source = "mcp"
                if tool_def.source in self._mcp_server_map:
                    mcp_server_id = self._mcp_server_map[tool_def.source]
                else:
                    # Try to find in database
                    from preloop.models.crud import crud_mcp_server

                    server = crud_mcp_server.get_by_name(
                        self.db, account_id=self.account_id, name=tool_def.source
                    )
                    if server:
                        mcp_server_id = server.id
                    else:
                        self._result.warnings.append(
                            f"MCP server '{tool_def.source}' not found "
                            f"for tool '{tool_def.name}'"
                        )
                        mcp_server_id = None

            # Look up approval workflow ID
            approval_workflow_id = None
            if tool_def.approval_workflow:
                if tool_def.approval_workflow in self._policy_map:
                    approval_workflow_id = self._policy_map[tool_def.approval_workflow]
                else:
                    # Try to find in database
                    from preloop.models.crud import crud_approval_workflow

                    policy = crud_approval_workflow.get_by_name(
                        self.db,
                        account_id=self.account_id,
                        name=tool_def.approval_workflow,
                    )
                    if policy:
                        approval_workflow_id = policy.id
                    else:
                        self._result.warnings.append(
                            f"Approval workflow '{tool_def.approval_workflow}' not found "
                            f"for tool '{tool_def.name}'"
                        )

            # Check if tool config exists
            existing = crud_tool_configuration.get_by_tool_name_and_source(
                self.db,
                account_id=self.account_id,
                tool_name=tool_def.name,
                tool_source=tool_source,
            )

            if existing:
                # Update existing config
                if not dry_run:
                    existing.is_enabled = tool_def.enabled
                    existing.approval_workflow_id = approval_workflow_id
                    # Always update tool_source and mcp_server_id to keep consistent
                    # with YAML (including None to clear old references)
                    existing.tool_source = tool_source
                    existing.mcp_server_id = mcp_server_id
                    if tool_def.description:
                        existing.tool_description = tool_def.description
                    if tool_def.custom_config:
                        existing.custom_config = tool_def.custom_config
                    existing.justification_mode = tool_def.justification

                    # Handle conditions - always apply to clear stale rules
                    # when conditions is empty/None
                    self._apply_tool_conditions(
                        existing.id, tool_def.conditions or [], dry_run
                    )

                self._result.tools_updated += 1
                logger.info(f"Updated tool config: {tool_def.name}")
            else:
                # Create new config
                if not dry_run:
                    new_config = ToolConfiguration(
                        account_id=self.account_id,
                        tool_name=tool_def.name,
                        tool_source=tool_source,
                        mcp_server_id=mcp_server_id,
                        is_enabled=tool_def.enabled,
                        approval_workflow_id=approval_workflow_id,
                        tool_description=tool_def.description,
                        custom_config=tool_def.custom_config,
                        justification_mode=tool_def.justification,
                    )
                    self.db.add(new_config)
                    self.db.flush()

                    # Handle conditions
                    if tool_def.conditions:
                        self._apply_tool_conditions(
                            new_config.id, tool_def.conditions, dry_run
                        )

                self._result.tools_created += 1
                logger.info(f"Created tool config: {tool_def.name}")

    def _apply_tool_conditions(
        self,
        tool_config_id: UUID,
        conditions: List[ToolCondition],
        dry_run: bool,
    ) -> None:
        """Apply tool access rules for a tool configuration.

        Creates ToolAccessRule records for each condition defined in the policy.
        Each condition is stored as a separate rule with:
        - priority: Based on order in YAML (first = 0, second = 1, etc.)
        - action: The action to take (allow, deny, require_approval)
        - condition_type: 'simple' or 'cel' based on expression complexity
        - condition_expression: The CEL or simple expression

        Existing rules for the tool are replaced with the new set.

        Args:
            tool_config_id: The tool configuration ID to attach rules to.
            conditions: List of ToolCondition from the policy YAML.
            dry_run: If True, don't make database changes.
        """
        from preloop.models.models.tool_access_rule import ToolAccessRule

        if dry_run:
            return

        # Delete existing rules for this tool configuration
        existing_rules = (
            self.db.query(ToolAccessRule)
            .filter(
                ToolAccessRule.tool_configuration_id == tool_config_id,
                ToolAccessRule.account_id == self.account_id,
            )
            .all()
        )
        for rule in existing_rules:
            self.db.delete(rule)

        if not conditions:
            return

        # Create a new ToolAccessRule for each condition
        for priority, condition in enumerate(conditions):
            # Determine action string from enum or string value
            if isinstance(condition.action, ConditionAction):
                action = condition.action.value
            else:
                action = str(condition.action)

            # Determine condition type:
            # 1. If explicitly set to 'cel' in the policy, always honor it
            # 2. If 'simple' (or default), auto-detect and upgrade to 'cel' if needed
            # This prevents CEL expressions from being silently downgraded to 'simple'
            # which would cause them to fail parsing and fall back to default-allow
            explicit_type = getattr(condition, "condition_type", None)
            if isinstance(explicit_type, ConditionType):
                explicit_type = explicit_type.value

            detected_type = _detect_condition_type(condition.expression)

            if explicit_type == "cel":
                # Explicitly marked as CEL - always honor
                condition_type = "cel"
            elif detected_type == "cel":
                # Detection suggests CEL - upgrade to prevent policy bypass
                if explicit_type == "simple":
                    logger.warning(
                        f"Condition expression appears to be CEL but was marked as 'simple': "
                        f"{condition.expression[:50]}... Upgrading to 'cel' to prevent policy bypass."
                    )
                condition_type = "cel"
            else:
                # Use the explicit type or default to simple
                condition_type = explicit_type if explicit_type else "simple"

            new_rule = ToolAccessRule(
                account_id=self.account_id,
                tool_configuration_id=tool_config_id,
                condition_expression=condition.expression,
                condition_type=condition_type,
                action=action,
                priority=priority,
                description=condition.description,
                is_enabled=True,
            )
            self.db.add(new_rule)

    def _apply_defaults(
        self,
        defaults: DefaultsDefinition,
        dry_run: bool,
    ) -> bool:
        """Apply default behavior settings.

        NOTE: Default settings are not yet implemented. This method will raise
        an error if restrictive defaults are specified that would be silently
        ignored (leading to fail-open behavior).

        Safe defaults (that match current behavior):
        - unknown_tools: "allow"
        - require_approval_for_new_tools: false

        Restrictive defaults (not yet supported, will error):
        - unknown_tools: "deny" or "require_approval"
        - require_approval_for_new_tools: true
        """
        # Check for restrictive settings that would be silently ignored
        unsupported_settings = []

        # Check unknown_tools - only "allow" is supported (current default behavior)
        unknown_tools_value = (
            defaults.unknown_tools.value
            if hasattr(defaults.unknown_tools, "value")
            else defaults.unknown_tools
        )
        if unknown_tools_value != "allow":
            unsupported_settings.append(
                f"unknown_tools='{unknown_tools_value}' (only 'allow' is currently supported)"
            )

        # Check require_approval_for_new_tools - only False is supported
        if defaults.require_approval_for_new_tools:
            unsupported_settings.append(
                "require_approval_for_new_tools=true (not yet implemented)"
            )

        # Check default_approval_workflow - not yet enforced
        if defaults.default_approval_workflow:
            unsupported_settings.append(
                f"default_approval_workflow='{defaults.default_approval_workflow}' (not yet implemented)"
            )

        if unsupported_settings:
            error_msg = (
                "Policy contains restrictive default settings that are not yet supported. "
                "These settings would be silently ignored, leading to permissive behavior. "
                f"Unsupported settings: {'; '.join(unsupported_settings)}. "
                "Remove these settings or wait for implementation."
            )
            self._result.errors.append(error_msg)
            return False

        # Safe defaults - just log and continue
        logger.info(
            f"Default settings validated (using built-in defaults): "
            f"unknown_tools={unknown_tools_value}"
        )
        return True

    def _apply_model_io(
        self,
        rules: List[ModelIORule],
        dry_run: bool,
    ) -> None:
        """Persist model I/O rules onto account.meta_data."""
        from preloop.services.model_content_policy import replace_model_io_rules

        if dry_run:
            self._result.model_io_rules_applied = len(rules)
            return
        replace_model_io_rules(self.db, self.account_id, rules)
        self._result.model_io_rules_applied = len(rules)
        logger.info("Applied %s model I/O content rules", len(rules))


def export_current_policy(
    db: Session,
    account_id: Union[str, UUID],
    policy_name: str = "Exported Policy",
    include_mcp_servers: bool = True,
    include_credentials: bool = False,
) -> PolicyDocument:
    """Export the current configuration as a policy document.

    Args:
        db: SQLAlchemy database session.
        account_id: The account ID to export from.
        policy_name: Name for the exported policy.
        include_mcp_servers: Whether to include MCP server definitions.
            When True, servers are included but credentials are redacted by default.
            When False, the mcp_servers section is omitted entirely.
        include_credentials: Whether to include MCP server credentials.
            When False (default), auth_config is set to {"redacted": True}.
            When True, actual credentials are included (for internal snapshots).
            SECURITY: Only use True for internal operations like versioning.

    Returns:
        PolicyDocument representing the current configuration.
    """
    from datetime import datetime, timezone

    from preloop.models.crud import (
        crud_account,
        crud_approval_workflow,
        crud_mcp_server,
        crud_tool_configuration,
    )

    account_id_str = str(account_id)

    # Export MCP servers (if requested)
    server_defs: Optional[List[MCPServerDefinition]] = None
    server_name_map: Dict[str, str] = {}

    if include_mcp_servers:
        mcp_servers = crud_mcp_server.get_active_by_account(
            db, account_id=account_id_str
        )
        server_defs = []
        for server in mcp_servers:
            if include_credentials:
                # Include actual credentials (for internal snapshots/versioning)
                auth_config = server.auth_config
            else:
                # Redact credentials for security - set to placeholder indicating
                # they need to be configured
                auth_config = {"redacted": True} if server.auth_config else None

            server_defs.append(
                MCPServerDefinition(
                    name=server.name,
                    url=server.url,
                    transport=server.transport,
                    auth_type=server.auth_type,
                    auth_config=auth_config,
                )
            )
        server_name_map = {str(s.id): s.name for s in mcp_servers}
    else:
        # Still need to build the server name map for tool source references
        mcp_servers = crud_mcp_server.get_active_by_account(
            db, account_id=account_id_str
        )
        server_name_map = {str(s.id): s.name for s in mcp_servers}

    # Export approval workflows
    policies = crud_approval_workflow.get_multi_by_account(
        db, account_id=account_id_str
    )

    # Build workflow name lookup first (needed for escalation workflow resolution)
    workflow_name_map = {str(p.id): p.name for p in policies}

    policy_defs = []
    for policy in policies:
        # Map database approval_mode to YAML approval_type
        # approval_mode controls who approves: 'standard' (human) or 'ai_driven' (AI)
        yaml_approval_type = (
            "ai_driven"
            if getattr(policy, "approval_mode", None) == "ai_driven"
            else "standard"
        )

        # Resolve escalation workflow name from ID
        escalation_workflow_name = None
        escalation_workflow_id = getattr(policy, "escalation_workflow_id", None)
        if escalation_workflow_id:
            escalation_workflow_name = workflow_name_map.get(
                str(escalation_workflow_id)
            )

        policy_def = ApprovalWorkflowDefinition(
            name=policy.name,
            description=policy.description,
            timeout_seconds=policy.timeout_seconds,
            require_reason=policy.require_reason,
            is_default=policy.is_default,
            workflow_type=policy.workflow_type,
            approvals_required=policy.approvals_required,
            channel_configs=policy.channel_configs,
            # AI-driven settings
            approval_type=yaml_approval_type,
            ai_model=getattr(policy, "ai_model", None),
            ai_guidelines=getattr(policy, "ai_guidelines", None),
            ai_context=getattr(policy, "ai_context", None),
            ai_confidence_threshold=getattr(policy, "ai_confidence_threshold", 0.8),
            ai_fallback_behavior=getattr(policy, "ai_fallback_behavior", "escalate"),
            escalation_workflow=escalation_workflow_name,
            async_approval=getattr(policy, "async_approval_enabled", False),
        )
        policy_defs.append(policy_def)

    # Export tool configurations
    tool_configs = crud_tool_configuration.get_multi_by_account(
        db, account_id=account_id_str
    )
    tool_defs = []

    for config in tool_configs:
        # Determine source name
        if config.tool_source == "mcp" and config.mcp_server_id:
            source = server_name_map.get(str(config.mcp_server_id), "mcp")
        else:
            source = config.tool_source

        # Get access rules and convert to conditions
        # Rules are stored with priority, so sort by priority for consistent export
        conditions = []
        if config.access_rules:
            sorted_rules = sorted(config.access_rules, key=lambda r: r.priority)
            for rule in sorted_rules:
                if rule.is_enabled and rule.condition_expression:
                    # Map action string to ConditionAction enum
                    action_map = {
                        "allow": ConditionAction.ALLOW,
                        "deny": ConditionAction.DENY,
                        "require_approval": ConditionAction.REQUIRE_APPROVAL,
                    }
                    action = action_map.get(
                        rule.action, ConditionAction.REQUIRE_APPROVAL
                    )
                    conditions.append(
                        ToolCondition(
                            expression=rule.condition_expression,
                            action=action,
                            description=rule.description,
                        )
                    )

        tool_def = ToolDefinition(
            name=config.tool_name,
            source=source,
            enabled=config.is_enabled,
            approval_workflow=(
                workflow_name_map.get(str(config.approval_workflow_id))
                if config.approval_workflow_id
                else None
            ),
            conditions=conditions if conditions else None,
            description=config.tool_description,
            justification=getattr(config, "justification_mode", None),
            custom_config=config.custom_config,
        )
        tool_defs.append(tool_def)

    from preloop.services.model_content_policy import (
        MODEL_IO_META_KEY,
        parse_model_io_rules,
    )

    account = crud_account.get(db, id=account_id_str)
    model_io_rules = parse_model_io_rules(
        ((account.meta_data or {}) if account else {}).get(MODEL_IO_META_KEY)
    )

    return PolicyDocument(
        version=PolicyVersion.V1_0,
        metadata=PolicyMetadata(
            name=policy_name,
            description="Exported from current configuration",
            created_at=datetime.now(timezone.utc),
        ),
        mcp_servers=server_defs if server_defs else None,
        approval_workflows=policy_defs if policy_defs else None,
        tools=tool_defs if tool_defs else None,
        model_io=model_io_rules if model_io_rules else None,
        defaults=DefaultsDefinition(),  # Default settings
    )
