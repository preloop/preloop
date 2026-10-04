"""Policies router for managing YAML-based policy definitions.

This module provides API endpoints for declarative policy-as-code management:
- Upload and apply YAML/JSON policy files
- Export current configuration as YAML policy
- Preview changes (diff) before applying
- Validate policy files without applying
- Version management (snapshots, rollback, tagging)
"""

import logging
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.models.schemas.mcp_server import redact_snapshot_credentials
from preloop.services.policy import (
    ModelIORule,
    PolicyApplier,
    PolicyDiffResult,
    PolicyDocument,
    PolicyImportResult,
    PolicyValidationResult,
    compute_policy_diff,
    export_current_policy,
    export_policy_to_json,
    export_policy_to_yaml,
    is_known_tool_source,
    load_policy_from_string,
)
from preloop.services.model_content_policy import (
    delete_model_io_rule,
    load_model_io_rules,
    replace_model_io_rules,
    serialize_model_io_rules,
    upsert_model_io_rule,
)
from preloop.services.policy.schema import (
    PIIDetectorConfig,
    SensitiveDataDetectorsConfig,
)
from preloop.services.sensitive_data.detectors import (
    DetectorConfig,
    DetectorTimeoutError,
    UnsafePatternError,
    detect,
    list_types,
    types_found,
)
from preloop.services.sensitive_data.policy_store import (
    detector_config_from,
    load_sensitive_data_config,
)
from preloop.services.sensitive_data.redact import redact_text
from preloop.services.policy_version_service import PolicyVersionService
from preloop.utils.permissions import require_permission


# Pydantic models for version management endpoints
class PolicyVersionMetadata(BaseModel):
    """Metadata for a policy version (without full snapshot data)."""

    id: UUID
    version_number: int
    tag: Optional[str] = None
    description: Optional[str] = None
    is_active: bool
    mcp_servers_count: int
    policies_count: int
    tools_count: int
    created_at: str
    created_by_user_id: Optional[UUID] = None


class PolicyVersionFull(PolicyVersionMetadata):
    """Full policy version including snapshot data."""

    snapshot_data: Dict[str, Any]


class PolicyVersionListResponse(BaseModel):
    """Response for listing policy versions."""

    versions: List[PolicyVersionMetadata]
    total: int


class CreateVersionRequest(BaseModel):
    """Request to create a new policy version."""

    description: Optional[str] = Field(None, description="Description of the version")
    tag: Optional[str] = Field(
        None, description="Tag for the version (e.g., 'production')"
    )


class UpdateTagRequest(BaseModel):
    """Request to update a version's tag."""

    tag: str = Field(..., description="New tag value")


class RollbackRequest(BaseModel):
    """Request to rollback to a previous version."""

    preview_only: bool = Field(
        False, description="If true, return diff without applying changes"
    )


class RollbackResponse(BaseModel):
    """Response from a rollback operation."""

    success: bool
    diff: Optional[PolicyDiffResult] = None
    error: Optional[str] = None


class PruneRequest(BaseModel):
    """Request to prune old versions."""

    older_than_days: int = Field(
        90, description="Delete versions older than this many days"
    )
    keep_tagged: bool = Field(
        True, description="Keep tagged versions regardless of age"
    )
    keep_count: int = Field(10, description="Always keep at least this many versions")


class PruneResponse(BaseModel):
    """Response from a prune operation."""

    deleted_count: int


class ModelIORuleListResponse(BaseModel):
    """List of model I/O content policy rules."""

    rules: List[Dict[str, Any]]


class ModelIORulePatchRequest(BaseModel):
    """Partial update for enable/disable."""

    enabled: Optional[bool] = None


class SensitiveDataTypeInfo(BaseModel):
    """One selectable sensitive-data type (feeds the console page)."""

    id: str
    label: str
    description: str
    example: str
    locales: List[str] = Field(default_factory=list)
    checksum: bool = False
    builtin: bool = True


class SensitiveDataTypesResponse(BaseModel):
    """Built-in, registered and account-defined types."""

    types: List[SensitiveDataTypeInfo]
    default_types: List[str] = Field(
        description="Types the pii detector scans when a rule lists none"
    )


class SensitiveDataTestRequest(BaseModel):
    """Run the detectors on sample text. The text is never logged or stored."""

    text: str = Field(..., max_length=20_000, description="Sample text to scan")
    types: Optional[List[str]] = Field(
        None, description="Types to scan; default every type in the config"
    )
    config: Optional[SensitiveDataDetectorsConfig] = Field(
        None,
        description=(
            "Detector configuration to test; default the account's stored block"
        ),
    )


class SensitiveDataHashCheckRequest(BaseModel):
    """Equality check of a candidate payload against a stored fingerprint."""

    payload: Any = Field(..., description="Candidate arguments or result")
    args_hmac: str = Field(
        ..., min_length=16, max_length=128, description="Stored HMAC"
    )
    salt_id: Optional[str] = Field(
        None, description="Salt id from the record; omit to try every account salt"
    )


class SensitiveDataHashCheckResponse(BaseModel):
    """Whether the candidate matches. The payload is never stored."""

    match: bool
    salt_id: Optional[str] = None


class SensitiveDataMatch(BaseModel):
    """One detected span (offsets into the submitted text)."""

    type: str
    start: int
    end: int
    confidence: float


class SensitiveDataTestResponse(BaseModel):
    """Detector output for the submitted text."""

    matches: List[SensitiveDataMatch]
    types_found: List[str]
    count: int
    redacted_preview: Optional[str] = Field(
        None, description="Text with each match replaced by [REDACTED:<type>]"
    )


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/policies/validate",
    response_model=PolicyValidationResult,
    summary="Validate a policy file",
    description="Validate a YAML/JSON policy file without applying any changes.",
)
@require_permission("view_policies")
async def validate_policy(
    file: UploadFile = File(..., description="YAML or JSON policy file to validate"),
    check_server_references: bool = Form(
        True,
        description=(
            "If true, validate that MCP server references resolve to a server "
            "defined in the file or configured in your account. If false, MCP "
            "server references are not checked. Approval workflow references "
            "are always resolved against the file and your account."
        ),
    ),
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PolicyValidationResult:
    """Validate a policy file without applying changes.

    This endpoint parses and validates the policy file against the schema,
    checking for:
    - Valid YAML/JSON syntax
    - Required fields
    - Approval workflow references (tools, model_io rules, defaults and
      escalation), resolved against the file and the account
    - Expression syntax
    - MCP server references, resolved against the file and the account
      (only if check_server_references=true)

    Args:
        file: The policy file to validate (YAML or JSON).
        check_server_references: If True, also validate that referenced MCP
            servers are defined in the file or configured in your account.
        account: Current user's account.
        db: Database session.

    Returns:
        PolicyValidationResult with validation status and any errors.
    """
    from preloop.models.crud import crud_approval_workflow, crud_mcp_server
    from preloop.services.policy.schema import PolicyValidationError

    # Read file content
    try:
        content = await file.read()
        content_str = content.decode("utf-8")
    except UnicodeDecodeError as e:
        logger.warning(f"Failed to decode policy file: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be UTF-8 encoded text",
        )

    # Determine format from filename
    filename = file.filename or ""
    if filename.lower().endswith(".json"):
        format = "json"
    else:
        format = "yaml"

    # Validate the policy schema
    policy, result = load_policy_from_string(content_str, format=format)

    # Cross-references are resolved against the policy file plus the account.
    # Approval workflow references are always checked; MCP server references
    # only when check_server_references is set.
    if policy and result.is_valid:
        policy_servers = {s.name.lower() for s in policy.mcp_servers or []}
        policy_approval_workflows = {w.name for w in policy.approval_workflows or []}

        all_available_workflows = (
            policy_approval_workflows
            | crud_approval_workflow.get_names_by_account(
                db, account_id=str(account.id)
            )
        )
        all_available_servers: set[str] = set()
        if check_server_references:
            existing_servers = crud_mcp_server.get_active_by_account(
                db, account_id=str(account.id)
            )
            all_available_servers = policy_servers | {
                s.name.lower() for s in existing_servers
            }

        missing_server_seen = False
        missing_workflow_seen = False

        def _workflow_error(path: str, owner: str, name: str) -> None:
            nonlocal missing_workflow_seen
            missing_workflow_seen = True
            result.errors.append(
                PolicyValidationError(
                    path=path,
                    message=(
                        f"{owner} references approval workflow '{name}' which "
                        f"is not defined. Either add the workflow to your "
                        f"policy file under 'approval_workflows', or configure "
                        f"it in the console first."
                    ),
                    value=name,
                )
            )

        for idx, tool in enumerate(policy.tools or []):
            source_lower = tool.source.lower()
            if (
                check_server_references
                and not is_known_tool_source(source_lower)
                and source_lower not in all_available_servers
            ):
                missing_server_seen = True
                result.errors.append(
                    PolicyValidationError(
                        path=f"$.tools[{idx}].source",
                        message=(
                            f"Tool '{tool.name}' references MCP server "
                            f"'{tool.source}' which is not configured. "
                            f"Either add the server to your policy file "
                            f"under 'mcp_servers', or configure it in the "
                            f"console first."
                        ),
                        value=tool.source,
                    )
                )
            if (
                tool.approval_workflow
                and tool.approval_workflow not in all_available_workflows
            ):
                _workflow_error(
                    f"$.tools[{idx}].approval_workflow",
                    f"Tool '{tool.name}'",
                    tool.approval_workflow,
                )

        for idx, rule in enumerate(policy.model_io or []):
            if (
                rule.approval_workflow
                and rule.approval_workflow not in all_available_workflows
            ):
                _workflow_error(
                    f"$.model_io[{idx}].approval_workflow",
                    f"model_io rule '{rule.id}'",
                    rule.approval_workflow,
                )

        if policy.defaults and policy.defaults.default_approval_workflow:
            name = policy.defaults.default_approval_workflow
            if name not in all_available_workflows:
                _workflow_error(
                    "$.defaults.default_approval_workflow", "Defaults", name
                )

        for idx, wf in enumerate(policy.approval_workflows or []):
            if (
                wf.escalation_workflow
                and wf.escalation_workflow not in all_available_workflows
            ):
                _workflow_error(
                    f"$.approval_workflows[{idx}].escalation_workflow",
                    f"Approval workflow '{wf.name}'",
                    wf.escalation_workflow,
                )

        if missing_server_seen and all_available_servers:
            result.warnings.append(
                f"Available MCP servers: [{', '.join(sorted(all_available_servers))}]"
            )
        if missing_workflow_seen and all_available_workflows:
            result.warnings.append(
                "Available approval workflows: "
                f"[{', '.join(sorted(all_available_workflows))}]"
            )

        if result.errors:
            result.is_valid = False

    if policy and result.is_valid:
        logger.info(
            f"Policy '{policy.metadata.name}' validated successfully "
            f"for account {account.id}"
        )

    return result


@router.post(
    "/policies/upload",
    response_model=PolicyImportResult,
    summary="Upload and apply a policy file",
    description="Upload a YAML/JSON policy file and apply it to your account.",
)
@require_permission("manage_policies")
async def upload_policy(
    file: UploadFile = File(..., description="YAML or JSON policy file to apply"),
    dry_run: bool = Form(
        False, description="If true, validate only without making changes"
    ),
    resolve_env: bool = Form(
        True, description="If true, resolve ${VAR} environment variable references"
    ),
    skip_missing_servers: bool = Form(
        False,
        description=(
            "If true, skip tools that reference MCP servers not configured in your "
            "account instead of failing. Skipped tools are reported as warnings."
        ),
    ),
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PolicyImportResult:
    """Upload and apply a policy file.

    This endpoint:
    1. Parses and validates the policy file
    2. Creates/updates MCP servers defined in the policy
    3. Creates/updates approval workflows
    4. Creates/updates tool configurations
    5. Applies default behavior settings

    When `mcp_servers` is omitted from the policy file, tools that reference
    server names (sources that are not a known ToolSource value) will be
    validated against servers already configured in your account. If a
    referenced server doesn't exist:
    - With `skip_missing_servers=false` (default): Returns an error
    - With `skip_missing_servers=true`: Skips the tool and adds a warning

    Args:
        file: The policy file to apply (YAML or JSON).
        dry_run: If True, validate without applying changes.
        resolve_env: If True, resolve environment variable references.
        skip_missing_servers: If True, skip tools with missing servers
            instead of failing.
        account: Current user's account.
        db: Database session.

    Returns:
        PolicyImportResult with details of what was created/updated.

    Raises:
        HTTPException: If validation fails or application fails.
    """
    from starlette.concurrency import run_in_threadpool
    from preloop.utils.permissions import ensure_permission_in_oss

    await run_in_threadpool(
        ensure_permission_in_oss, db, current_user, "manage_policies"
    )

    # Read file content
    try:
        content = await file.read()
        content_str = content.decode("utf-8")
    except UnicodeDecodeError as e:
        logger.warning(f"Failed to decode policy file: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be UTF-8 encoded text",
        )

    # Determine format from filename
    filename = file.filename or ""
    if filename.lower().endswith(".json"):
        format = "json"
    else:
        format = "yaml"

    # Load and validate the policy
    policy, validation_result = load_policy_from_string(content_str, format=format)

    if not validation_result.is_valid or policy is None:
        logger.warning(
            f"Policy validation failed for account {account.id}: "
            f"{validation_result.errors}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Policy validation failed",
                "errors": [e.model_dump() for e in validation_result.errors],
            },
        )

    # Apply the policy
    applier = PolicyApplier(db, account_id=account.id, actor_id=current_user.id)
    result = await run_in_threadpool(
        applier.apply,
        policy,
        dry_run=dry_run,
        resolve_env=resolve_env,
        skip_missing_servers=skip_missing_servers,
    )

    if not result.success:
        logger.warning(
            "Policy apply rejected for account %s policy '%s': %s",
            account.id,
            policy.metadata.name,
            result.errors,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Failed to apply policy",
                "errors": result.errors,
                "warnings": result.warnings,
            },
        )

    action = "validated (dry run)" if dry_run else "applied"
    logger.info(
        f"Policy '{policy.metadata.name}' {action} for account {account.id}: "
        f"{result.mcp_servers_created + result.mcp_servers_updated} servers, "
        f"{result.policies_created + result.policies_updated} policies, "
        f"{result.tools_created + result.tools_updated} tools"
    )

    return result


@router.get(
    "/policies/sensitive-data/types",
    response_model=SensitiveDataTypesResponse,
    summary="List sensitive-data detector types",
)
@require_permission("view_policies")
def list_sensitive_data_types(
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SensitiveDataTypesResponse:
    """Type id, label, description, example and locales for every detector.

    Includes the account's custom patterns and keyword lists so the console
    can offer them next to the built-ins.
    """
    from preloop.services.policy.schema import SUPPORTED_PII_TYPES

    config = detector_config_from(load_sensitive_data_config(db, account.id))
    return SensitiveDataTypesResponse(
        types=[SensitiveDataTypeInfo(**info.as_dict()) for info in list_types(config)],
        # What a rule without its own list scans: the account default when
        # set, else the legacy three.
        default_types=list(config.types) if config.types else list(SUPPORTED_PII_TYPES),
    )


@router.post(
    "/policies/sensitive-data/test",
    response_model=SensitiveDataTestResponse,
    summary="Test sensitive-data detectors on sample text",
)
@require_permission("view_policies")
def test_sensitive_data_detectors(
    request: SensitiveDataTestRequest,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SensitiveDataTestResponse:
    """Return match spans for ``text``. The input is never logged or stored.

    Account patterns run through the timeout-capable engine, so a
    pathological regex ends with a 422 instead of a blocked worker.
    """
    if request.config is not None:
        config = DetectorConfig.from_mapping(
            request.config.model_dump(exclude_none=True, mode="json")
        )
    else:
        config = detector_config_from(load_sensitive_data_config(db, account.id))
    if request.types is not None:
        try:
            PIIDetectorConfig(types=request.types)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        config = config.with_types(request.types)
    try:
        matches = detect(request.text, config)
        preview, _counts = redact_text(request.text, config)
    except DetectorTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"A custom pattern exceeded its match budget: {exc}",
        ) from exc
    except UnsafePatternError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return SensitiveDataTestResponse(
        matches=[
            SensitiveDataMatch(
                type=m.type, start=m.start, end=m.end, confidence=m.confidence
            )
            for m in matches
        ],
        types_found=types_found(matches),
        count=len(matches),
        redacted_preview=preview if matches else request.text,
    )


@router.post(
    "/policies/sensitive-data/hash-check",
    response_model=SensitiveDataHashCheckResponse,
    summary="Check a candidate payload against a reference-only fingerprint",
)
@require_permission("manage_policies")
def sensitive_data_hash_check(
    request: SensitiveDataHashCheckRequest,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SensitiveDataHashCheckResponse:
    """Return whether ``payload`` produces ``args_hmac`` under this account's salts.

    Account scoped: only the caller's salts are tried, so a fingerprint from
    another account never matches. Nothing is stored or logged.
    """
    from preloop.services.sensitive_data.reference import verify_hmac

    matched, salt_id = verify_hmac(
        account.id, request.payload, request.args_hmac, salt_id=request.salt_id, db=db
    )
    return SensitiveDataHashCheckResponse(match=matched, salt_id=salt_id)


def _reject_unknown_pii_types(db: Session, account: Account, rule: ModelIORule) -> None:
    """Standalone rule writes cannot see a YAML document; check the account."""
    detectors = rule.detectors
    if detectors is None or not isinstance(detectors.pii, PIIDetectorConfig):
        return
    known = load_sensitive_data_config(db, account.id).known_types()
    unknown = [item for item in detectors.pii.types if item not in known]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Unknown PII types {unknown}. Define custom patterns or keyword "
                "lists under sensitive_data.detectors first."
            ),
        )


@router.get(
    "/policies/model-io-rules",
    response_model=ModelIORuleListResponse,
    summary="List model I/O content policy rules",
)
@require_permission("view_policies")
def list_model_io_rules(
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ModelIORuleListResponse:
    """Return model.request and model.response rules for the console."""
    rules = load_model_io_rules(db, account.id)
    return ModelIORuleListResponse(rules=serialize_model_io_rules(rules))


@router.post(
    "/policies/model-io-rules",
    summary="Create or replace a model I/O content policy rule",
)
@require_permission("manage_policies")
def create_model_io_rule(
    rule: ModelIORule,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Save one model I/O rule from the Policies console form."""
    _reject_unknown_pii_types(db, account, rule)
    saved = upsert_model_io_rule(db, account.id, rule)
    db.commit()
    return saved.model_dump(exclude_none=True, mode="json")


@router.put(
    "/policies/model-io-rules/{rule_id}",
    summary="Replace a model I/O content policy rule",
)
@require_permission("manage_policies")
def update_model_io_rule(
    rule_id: str,
    rule: ModelIORule,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Replace an existing model I/O rule. The path id wins."""
    if rule.id != rule_id:
        rule = rule.model_copy(update={"id": rule_id})
    existing = {item.id: item for item in load_model_io_rules(db, account.id)}
    if rule_id not in existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"model_io rule '{rule_id}' not found",
        )
    _reject_unknown_pii_types(db, account, rule)
    saved = upsert_model_io_rule(db, account.id, rule)
    db.commit()
    return saved.model_dump(exclude_none=True, mode="json")


@router.patch(
    "/policies/model-io-rules/{rule_id}",
    summary="Enable or disable a model I/O content policy rule",
)
@require_permission("manage_policies")
def patch_model_io_rule(
    rule_id: str,
    patch: ModelIORulePatchRequest,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Toggle enabled without rewriting the rest of the rule."""
    rules = load_model_io_rules(db, account.id)
    updated: List[ModelIORule] = []
    found = False
    for item in rules:
        if item.id == rule_id:
            found = True
            if patch.enabled is not None:
                item = item.model_copy(update={"enabled": patch.enabled})
            updated.append(item)
        else:
            updated.append(item)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"model_io rule '{rule_id}' not found",
        )
    replace_model_io_rules(db, account.id, updated)
    db.commit()
    saved = next(item for item in updated if item.id == rule_id)
    return saved.model_dump(exclude_none=True, mode="json")


@router.delete(
    "/policies/model-io-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a model I/O content policy rule",
)
@require_permission("manage_policies")
def remove_model_io_rule(
    rule_id: str,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Response:
    """Delete one model I/O rule."""
    if not delete_model_io_rule(db, account.id, rule_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"model_io rule '{rule_id}' not found",
        )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/policies/export",
    summary="Export current configuration as policy",
    description=(
        "Export your current MCP servers, approval workflows, and tool "
        "configurations as a YAML or JSON policy file."
    ),
)
@require_permission("view_policies")
async def export_policy(
    format: Literal["yaml", "json"] = "yaml",
    policy_name: str = "Exported Policy",
    include_mcp_servers: bool = True,
    include_credentials: bool = False,  # Ignored for security, always False
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Response:
    """Export current configuration as a policy file.

    This endpoint exports:
    - MCP server configurations (without auth credentials) - optional
    - Approval policies
    - Tool configurations with approval conditions

    Args:
        format: Output format ('yaml' or 'json').
        policy_name: Name to give the exported policy.
        include_mcp_servers: Whether to include MCP server definitions.
        include_credentials: Ignored for security - credentials are never exported.
        account: Current user's account.
        db: Database session.

    Returns:
        YAML or JSON file response.
    """
    # Note: include_credentials is always treated as False for security
    _ = include_credentials  # Explicitly ignored

    # Export current configuration
    policy = export_current_policy(
        db,
        account_id=account.id,
        policy_name=policy_name,
        include_mcp_servers=include_mcp_servers,
    )

    # Get account name for header comment
    account_name = account.organization_name or str(account.id)

    # Convert to requested format
    if format == "json":
        content = export_policy_to_json(policy)
        media_type = "application/json"
        filename = "policy.json"
    else:
        content = export_policy_to_yaml(
            policy,
            account_name=account_name,
            include_mcp_servers=include_mcp_servers,
        )
        media_type = "application/x-yaml"
        filename = "policy.yaml"

    logger.info(f"Exported policy for account {account.id} as {format}")

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/policies/diff",
    response_model=PolicyDiffResult,
    summary="Preview changes from a policy file",
    description=(
        "Compare an uploaded policy file with your current configuration "
        "to see what would change."
    ),
)
@require_permission("view_policies")
async def diff_policy(
    file: UploadFile = File(..., description="YAML or JSON policy file to compare"),
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PolicyDiffResult:
    """Compare uploaded policy with current configuration.

    This endpoint shows what would change if the policy were applied:
    - Added items (new servers, policies, tools)
    - Removed items (items in current config but not in policy)
    - Modified items (items with different settings)

    Note: This does NOT apply any changes - use POST /policies/upload for that.

    Args:
        file: The policy file to compare (YAML or JSON).
        account: Current user's account.
        db: Database session.

    Returns:
        PolicyDiffResult showing all differences.

    Raises:
        HTTPException: If validation fails.
    """
    # Read file content
    try:
        content = await file.read()
        content_str = content.decode("utf-8")
    except UnicodeDecodeError as e:
        logger.warning(f"Failed to decode policy file: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be UTF-8 encoded text",
        )

    # Determine format from filename
    filename = file.filename or ""
    if filename.lower().endswith(".json"):
        format = "json"
    else:
        format = "yaml"

    # Load and validate the incoming policy
    incoming_policy, validation_result = load_policy_from_string(
        content_str, format=format
    )

    if not validation_result.is_valid or incoming_policy is None:
        logger.warning(
            f"Policy validation failed for diff, account {account.id}: "
            f"{validation_result.errors}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Policy validation failed",
                "errors": [e.model_dump() for e in validation_result.errors],
            },
        )

    # Export current configuration as a policy document
    current_policy = export_current_policy(
        db, account_id=account.id, policy_name="Current Configuration"
    )

    # Compute diff
    diff_result = compute_policy_diff(current_policy, incoming_policy)

    logger.info(f"Computed policy diff for account {account.id}: {diff_result.summary}")

    return diff_result


@router.get(
    "/policies/schema",
    summary="Get policy schema documentation",
    description="Get the JSON schema for policy files with documentation.",
)
async def get_policy_schema() -> dict:
    """Get the JSON schema for policy files.

    This endpoint returns the JSON schema that describes the structure
    of policy files, including:
    - All available fields and their types
    - Required vs optional fields
    - Enum values for constrained fields
    - Field descriptions

    This schema can be used for:
    - IDE autocompletion (with YAML/JSON language servers)
    - Documentation
    - Custom validation

    Returns:
        JSON schema for PolicyDocument.
    """
    schema = PolicyDocument.model_json_schema()

    # Add helpful metadata
    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
    schema["title"] = "Preloop Policy Schema"
    schema["description"] = (
        "Schema for Preloop policy-as-code YAML/JSON files. "
        "Define MCP servers, approval workflows, tool configurations, "
        "model I/O content rules, and defaults."
    )

    return schema


# ============================================================================
# Policy Version Management Endpoints
# ============================================================================


def _snapshot_to_metadata(snapshot) -> PolicyVersionMetadata:
    """Convert a PolicySnapshot to PolicyVersionMetadata."""
    return PolicyVersionMetadata(
        id=snapshot.id,
        version_number=snapshot.version_number,
        tag=snapshot.tag,
        description=snapshot.description,
        is_active=snapshot.is_active,
        mcp_servers_count=snapshot.mcp_servers_count,
        policies_count=snapshot.policies_count,
        tools_count=snapshot.tools_count,
        created_at=snapshot.created_at.isoformat(),
        created_by_user_id=snapshot.created_by_user_id,
    )


def _snapshot_to_full(snapshot) -> PolicyVersionFull:
    """Convert a PolicySnapshot to PolicyVersionFull with credentials masked."""
    return PolicyVersionFull(
        id=snapshot.id,
        version_number=snapshot.version_number,
        tag=snapshot.tag,
        description=snapshot.description,
        is_active=snapshot.is_active,
        mcp_servers_count=snapshot.mcp_servers_count,
        policies_count=snapshot.policies_count,
        tools_count=snapshot.tools_count,
        created_at=snapshot.created_at.isoformat(),
        created_by_user_id=snapshot.created_by_user_id,
        snapshot_data=redact_snapshot_credentials(snapshot.snapshot_data),
    )


@router.get(
    "/policies/versions",
    response_model=PolicyVersionListResponse,
    summary="List policy versions",
    description="List all policy versions for the account with optional pagination.",
)
@require_permission("view_policies")
def list_policy_versions(
    limit: int = Query(
        100, ge=1, le=1000, description="Maximum number of versions to return"
    ),
    offset: int = Query(0, ge=0, description="Number of versions to skip"),
    include_snapshots: bool = Query(False, description="Include full snapshot data"),
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PolicyVersionListResponse:
    """List all policy versions for the account.

    Args:
        limit: Maximum number of versions to return.
        offset: Number of versions to skip.
        include_snapshots: Whether to include full snapshot data.
        account: Current user's account.
        db: Database session.

    Returns:
        List of policy versions with metadata.
    """
    service = PolicyVersionService(db, str(account.id))
    snapshots = service.list_snapshots(
        limit=limit,
        offset=offset,
        include_snapshots=include_snapshots,
    )

    # Get total count
    from preloop.models.crud.policy_snapshot import crud_policy_snapshot

    total = crud_policy_snapshot.count_by_account(db, str(account.id))

    versions = [_snapshot_to_metadata(s) for s in snapshots]

    return PolicyVersionListResponse(versions=versions, total=total)


@router.get(
    "/policies/versions/{version_id}",
    response_model=PolicyVersionFull,
    summary="Get a specific policy version",
    description="Get a specific policy version with full snapshot data.",
)
@require_permission("view_policies")
def get_policy_version(
    version_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PolicyVersionFull:
    """Get a specific policy version with full snapshot data.

    Args:
        version_id: The ID of the version to retrieve.
        account: Current user's account.
        db: Database session.

    Returns:
        Complete policy version with snapshot data.

    Raises:
        HTTPException: If version not found.
    """
    service = PolicyVersionService(db, str(account.id))
    snapshot = service.get_snapshot(version_id)

    if not snapshot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Policy version not found",
        )

    return _snapshot_to_full(snapshot)


@router.post(
    "/policies/versions",
    response_model=PolicyVersionFull,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new policy version",
    description="Create a snapshot of the current policy state.",
)
@require_permission("manage_policies")
async def create_policy_version(
    request: CreateVersionRequest,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PolicyVersionFull:
    """Create a new policy version snapshot.

    Takes a snapshot of the current MCP servers, approval workflows,
    tool configurations, and defaults.

    Args:
        request: The version creation request with description and optional tag.
        account: Current user's account.
        user: Current user.
        db: Database session.

    Returns:
        The created policy version.
    """
    service = PolicyVersionService(db, str(account.id))
    snapshot = service.create_snapshot(
        description=request.description,
        tag=request.tag,
        user_id=current_user.id,
        set_active=True,
    )

    db.commit()

    logger.info(
        f"Created policy version v{snapshot.version_number} for account {account.id}"
    )

    return _snapshot_to_full(snapshot)


@router.put(
    "/policies/versions/{version_id}/tag",
    response_model=PolicyVersionMetadata,
    summary="Add or update tag on a version",
    description="Add or update the tag on a policy version.",
)
@require_permission("manage_policies")
async def update_version_tag(
    version_id: UUID,
    request: UpdateTagRequest,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PolicyVersionMetadata:
    """Add or update the tag on a policy version.

    Tags are unique per account - if the tag is already used on another
    version, it will be moved to this version.

    Args:
        version_id: The ID of the version to update.
        request: The tag update request.
        account: Current user's account.
        db: Database session.

    Returns:
        Updated policy version metadata.

    Raises:
        HTTPException: If version not found.
    """
    service = PolicyVersionService(db, str(account.id))
    snapshot, error = service.update_tag(version_id, request.tag)

    if error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error,
        )

    db.commit()

    logger.info(
        f"Updated tag to '{request.tag}' on version {version_id} for account {account.id}"
    )

    return _snapshot_to_metadata(snapshot)


@router.delete(
    "/policies/versions/{version_id}/tag",
    response_model=PolicyVersionMetadata,
    summary="Remove tag from a version",
    description="Remove the tag from a policy version.",
)
@require_permission("manage_policies")
async def remove_version_tag(
    version_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PolicyVersionMetadata:
    """Remove the tag from a policy version.

    Args:
        version_id: The ID of the version to update.
        account: Current user's account.
        db: Database session.

    Returns:
        Updated policy version metadata.

    Raises:
        HTTPException: If version not found.
    """
    service = PolicyVersionService(db, str(account.id))
    snapshot, error = service.remove_tag(version_id)

    if error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error,
        )

    db.commit()

    logger.info(f"Removed tag from version {version_id} for account {account.id}")

    return _snapshot_to_metadata(snapshot)


@router.post(
    "/policies/versions/{version_id}/rollback",
    response_model=RollbackResponse,
    summary="Rollback to a previous version",
    description="Apply a previous policy version to restore that configuration.",
)
@require_permission("manage_policies")
async def rollback_to_version(
    version_id: UUID,
    request: RollbackRequest,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> RollbackResponse:
    """Rollback to a previous policy version.

    This endpoint applies the snapshot from a previous version, restoring
    MCP servers, approval workflows, and tool configurations to that state.

    If preview_only is True, returns the diff without making changes.

    Args:
        version_id: The ID of the version to rollback to.
        request: The rollback request with preview_only flag.
        account: Current user's account.
        db: Database session.

    Returns:
        RollbackResponse with diff and success status.

    Raises:
        HTTPException: If version not found.
    """
    service = PolicyVersionService(db, str(account.id))
    diff, success, error = service.rollback_to_snapshot(
        version_id,
        preview_only=request.preview_only,
    )

    if error and not diff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error,
        )

    if not request.preview_only and success:
        db.commit()
        logger.info(f"Rolled back to version {version_id} for account {account.id}")

    return RollbackResponse(success=success, diff=diff, error=error)


@router.delete(
    "/policies/versions/{version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a policy version",
    description="Delete a policy version. Cannot delete the active version.",
)
@require_permission("manage_policies")
async def delete_policy_version(
    version_id: UUID,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> None:
    """Delete a policy version.

    Cannot delete the currently active version.

    Args:
        version_id: The ID of the version to delete.
        account: Current user's account.
        db: Database session.

    Raises:
        HTTPException: If version not found or is active.
    """
    service = PolicyVersionService(db, str(account.id))
    success, error = service.delete_snapshot(version_id)

    if not success:
        if "not found" in error.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error,
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error,
            )

    db.commit()

    logger.info(f"Deleted version {version_id} for account {account.id}")


@router.post(
    "/policies/versions/prune",
    response_model=PruneResponse,
    summary="Prune old policy versions",
    description="Delete old unused policy versions based on age and count criteria.",
)
@require_permission("manage_policies")
async def prune_policy_versions(
    request: PruneRequest,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PruneResponse:
    """Delete old unused policy versions.

    Deletes versions that are:
    - Older than older_than_days
    - Not the active version
    - Not tagged (if keep_tagged is True)
    - Beyond the keep_count most recent versions

    Args:
        request: The prune request with criteria.
        account: Current user's account.
        db: Database session.

    Returns:
        PruneResponse with count of deleted versions.
    """
    service = PolicyVersionService(db, str(account.id))
    deleted_count = service.prune_snapshots(
        older_than_days=request.older_than_days,
        keep_tagged=request.keep_tagged,
        keep_count=request.keep_count,
    )

    db.commit()

    logger.info(f"Pruned {deleted_count} versions for account {account.id}")

    return PruneResponse(deleted_count=deleted_count)


# ============================================================================
# Policy Generation Endpoints
# ============================================================================


class GeneratePolicyRequest(BaseModel):
    """Request to generate a policy from a natural-language prompt."""

    prompt: str = Field(
        ..., description="Natural-language description of the desired policy"
    )
    include_current_config: bool = Field(
        True,
        description=(
            "Include the account's current MCP servers and tools as context "
            "for the LLM (recommended for more accurate generation)"
        ),
    )
    scope_mcp_server_name: Optional[str] = Field(
        None,
        description=(
            "When set, starter-policy generation scopes LLM context and "
            "raw model output to this MCP server. The merge still restores "
            "all current tools so the diff preview only shows this "
            "server's changes. Older clients that omit the field fall "
            "back to matching the prompt text."
        ),
    )


class GeneratePolicyFromAuditRequest(BaseModel):
    """Request to generate a policy from audit-log patterns."""

    start_date: Optional[str] = Field(
        None, description="Only consider logs after this ISO date (e.g. 2026-01-01)"
    )
    end_date: Optional[str] = Field(
        None, description="Only consider logs before this ISO date"
    )
    audit_logs_json: Optional[str] = Field(
        None,
        description=(
            "Raw JSON dump of audit logs to analyse instead of querying "
            "the database. Must be a JSON array of log entries."
        ),
    )


class GeneratePolicyResponse(BaseModel):
    """Response from a policy generation endpoint."""

    yaml: str = Field(..., description="Generated policy YAML")
    warnings: List[str] = Field(
        default_factory=list, description="Non-fatal warnings from validation"
    )


@router.post(
    "/policies/generate",
    response_model=GeneratePolicyResponse,
    summary="Generate a policy from a description",
    description=(
        "Use an AI model to generate a valid Preloop policy YAML from a "
        "natural-language description. Requires at least one AI model "
        "configured on the account."
    ),
)
@require_permission("view_policies")
async def generate_policy(
    request: GeneratePolicyRequest,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> GeneratePolicyResponse:
    """Generate a policy YAML from a natural-language description.

    The endpoint picks the account's default AI model (or the most recently
    added one) and asks it to produce a valid policy YAML matching the
    Preloop schema.  The generated YAML is validated before being returned.

    Args:
        request: The generation request containing the prompt.
        account: Current user's account.
        db: Database session.

    Returns:
        GeneratePolicyResponse with the YAML and any warnings.

    Raises:
        HTTPException: If no AI model is configured or generation fails.
    """
    from preloop.services.policy_generation import (
        PolicyGenerationError,
        PolicyGenerationService,
    )

    import asyncio

    try:
        service = PolicyGenerationService(db, str(account.id))

        # Do all DB reads on the main (async) thread — Sessions
        # are not thread-safe and must not be shared across threads.
        model = service._resolve_model()
        context_block = (
            service._build_context_block(
                request.prompt,
                scope_mcp_server_name=request.scope_mcp_server_name,
            )
            if request.include_current_config
            else ""
        )

        # Only the LLM call (network I/O, CPU-bound tokenization)
        # runs in a worker thread.
        import json
        from preloop.services.policy.schema import PolicyDocument

        schema_json = json.dumps(PolicyDocument.model_json_schema(), indent=2)
        system_prompt = service._build_system_prompt(schema_json, context_block)

        yaml_output = await asyncio.to_thread(
            service._call_llm, model, system_prompt, request.prompt
        )
        if request.include_current_config:
            yaml_output = service._merge_preserving_unrelated(
                yaml_output,
                request.prompt,
                scope_mcp_server_name=request.scope_mcp_server_name,
            )
        warnings = service._validate_output(yaml_output)
        result = {"yaml": yaml_output, "warnings": warnings}
    except PolicyGenerationError as exc:
        logger.warning(
            "Policy generation failed for account %s: %s",
            account.id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    logger.info("Generated policy from prompt for account %s", account.id)
    return GeneratePolicyResponse(**result)


@router.post(
    "/policies/generate-from-audit",
    response_model=GeneratePolicyResponse,
    summary="Generate a policy from audit-log patterns",
    description=(
        "Analyse historical MCP tool-call audit logs and generate a policy "
        "that allows observed-normal calls and requires approval for "
        "outliers. Requires at least one AI model configured on the account."
    ),
)
@require_permission("view_policies")
async def generate_policy_from_audit(
    request: GeneratePolicyFromAuditRequest,
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> GeneratePolicyResponse:
    """Generate a policy from audit-log tool-call patterns.

    Args:
        request: The generation request with optional date range or raw logs.
        account: Current user's account.
        db: Database session.

    Returns:
        GeneratePolicyResponse with the YAML and any warnings.

    Raises:
        HTTPException: If no AI model is configured, no logs found, or
            generation fails.
    """
    from datetime import datetime as dt

    from preloop.services.policy_generation import (
        PolicyGenerationError,
        PolicyGenerationService,
    )

    start = None
    end = None
    if request.start_date:
        try:
            start = dt.fromisoformat(request.start_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid start_date format: {request.start_date}",
            )
    if request.end_date:
        try:
            end = dt.fromisoformat(request.end_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid end_date format: {request.end_date}",
            )

    import asyncio

    try:
        service = PolicyGenerationService(db, str(account.id))

        # Do all DB reads on the main thread (Session is not thread-safe).
        model = service._resolve_model()

        if request.audit_logs_json:
            summary = service._summarise_external_logs(request.audit_logs_json)
        else:
            summary = service._summarise_account_logs(start, end)

        if not summary:
            raise PolicyGenerationError(
                "No tool-call audit logs found for the specified criteria. "
                "Run some MCP tool calls first, then retry."
            )

        import json
        from preloop.services.policy.schema import PolicyDocument

        schema_json = json.dumps(PolicyDocument.model_json_schema(), indent=2)
        context_block = service._build_context_block()
        system_prompt = service._build_audit_system_prompt(schema_json, context_block)

        # Only the LLM call runs in a worker thread.
        yaml_output = await asyncio.to_thread(
            service._call_llm, model, system_prompt, summary
        )
        warnings = service._validate_output(yaml_output)
        result = {"yaml": yaml_output, "warnings": warnings}
    except PolicyGenerationError as exc:
        logger.warning(
            "Audit-based policy generation failed for account %s: %s",
            account.id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    logger.info("Generated policy from audit logs for account %s", account.id)
    return GeneratePolicyResponse(**result)
