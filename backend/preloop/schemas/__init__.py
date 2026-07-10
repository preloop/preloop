"""API schemas for request and response validation.

This module contains Pydantic models used for API request/response validation.
These are not database models - all database models are imported from preloop.models.
"""

from preloop.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    Token,
    TokenData,
    User,
    UserInDB,
)
from preloop.schemas.comment import (
    CommentBase,
    CommentCreate,
    CommentList,
    CommentResponse,
)
from preloop.schemas.issue import (
    IssueBase,
    IssueCreate,
    IssueResponse,
    IssueSearchResults,
    IssueUpdate,
)
from preloop.schemas.organization import (
    OrganizationBase,
    OrganizationCreate,
    OrganizationResponse,
    OrganizationUpdate,
)
from preloop.schemas.project import (
    ProjectBase,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    TestConnectionRequest,
    TestConnectionResponse,
)
from preloop.schemas.duplicates import (
    DuplicateIssuePair,
    ProjectDuplicatesResponse,
)
from preloop.schemas.ai_model import (
    AIModelBase,
    AIModelCreate,
    AIModelRead,
    AIModelUpdate,
)
from preloop.schemas.audio import SpeechToTextResponse, TextToSpeechRequest
from preloop.schemas.issue_duplicate import (
    IssueDuplicateProjectStats,
    IssueDuplicateStats,
    IssueDuplicateResolutionRequest,
    IssueDuplicateResolutionResponse,
    IssueDuplicateSuggestionRequest,
    IssueDuplicateSuggestionResponse,
    IssueDuplicateUpdate,
)
from preloop.schemas.issue_compliance import (
    IssueComplianceResultBase,
    IssueComplianceResultCreate,
    IssueComplianceResultResponse,
)
from preloop.schemas.issue_dependency import (
    CommitDependenciesRequest,
    DependencyRequest,
    DependencyResponse,
    ExtendScanRequest,
    DependencyPair,
)
from preloop.schemas.notification_preferences import (
    NotificationPreferencesBase,
    NotificationPreferencesUpdate,
    NotificationPreferencesResponse,
    MobileDeviceRegistration,
    QRCodeResponse,
)
from preloop.schemas.tool_approval_condition import (
    ToolApprovalConditionBase,
    ToolApprovalConditionCreate,
    ToolApprovalConditionUpdate,
    ToolApprovalConditionResponse,
    ConditionTestRequest,
    ConditionTestResponse,
)

__all__ = [
    # Auth schemas
    "LoginRequest",
    "RefreshRequest",
    "Token",
    "TokenData",
    "User",
    "UserInDB",
    # Comment schemas
    "CommentBase",
    "CommentCreate",
    "CommentList",
    "CommentResponse",
    # Issue schemas
    "IssueBase",
    "IssueCreate",
    "IssueResponse",
    "IssueSearchResults",
    "IssueUpdate",
    # Organization schemas
    "OrganizationBase",
    "OrganizationCreate",
    "OrganizationResponse",
    "OrganizationUpdate",
    # Project schemas
    "ProjectBase",
    "ProjectCreate",
    "ProjectResponse",
    "ProjectUpdate",
    "TestConnectionRequest",
    "TestConnectionResponse",
    # Duplicates schemas
    "DuplicateIssuePair",
    "ProjectDuplicatesResponse",
    # AIModel schemas
    "AIModelBase",
    "AIModelCreate",
    "AIModelRead",
    "AIModelUpdate",
    "SpeechToTextResponse",
    "TextToSpeechRequest",
    # IssueDuplicate schemas
    "IssueDuplicateStats",
    "IssueDuplicateProjectStats",
    "IssueDuplicateResolutionRequest",
    "IssueDuplicateResolutionResponse",
    "IssueDuplicateSuggestionRequest",
    "IssueDuplicateSuggestionResponse",
    "IssueDuplicateUpdate",
    # IssueCompliance schemas
    "IssueComplianceResultBase",
    "IssueComplianceResultCreate",
    "IssueComplianceResultResponse",
    # IssueDependency schemas
    "CommitDependenciesRequest",
    "DependencyRequest",
    "DependencyResponse",
    "ExtendScanRequest",
    "DependencyPair",
    # NotificationPreferences schemas
    "NotificationPreferencesBase",
    "NotificationPreferencesUpdate",
    "NotificationPreferencesResponse",
    "MobileDeviceRegistration",
    "QRCodeResponse",
    # ToolApprovalCondition schemas
    "ToolApprovalConditionBase",
    "ToolApprovalConditionCreate",
    "ToolApprovalConditionUpdate",
    "ToolApprovalConditionResponse",
    "ConditionTestRequest",
    "ConditionTestResponse",
]
