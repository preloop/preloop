"""CRUD operation implementations."""

# Create CRUD instances for each model
from .callback_receipt import crud_callback_key_binding, crud_callback_receipt
from ..models import (
    Account,
    AccountDiscoverySalt,
    AgentControlCommand,
    AIModel,
    ApiKey,
    GatewaySubject,
    ApiUsage,
    AuditLog,
    DiscoveredAgentCandidate,
    EmbeddingModel,
    GatewayUsageSearchDocument,
    Issue,
    IssueEmbedding,
    IssueRelationship,
    IssueSet,
    ManagedAgent,
    ManagedAgentAIModelBinding,
    ManagedAgentCredential,
    ManagedAgentEnrollment,
    ModelPriceOverride,
    OptimizationJob,
    Organization,
    Project,
    ProviderBillingConnection,
    ProviderBillingSnapshot,
    RuntimeSession,
    RuntimeSessionActivity,
    RuntimeSessionOptimizationAction,
    RuntimeSessionOptimizationResult,
    RuntimeSessionReplayRun,
    SessionEmbeddingSetting,
    SessionSavedSearch,
    SessionSearchDocument,
    TrackerScopeRule,
    WebAuthnCredential,
    Webhook,
)
from . import restricted_runtime as crud_restricted_runtime
from . import notification_preferences
from . import runtime_session_artifact as crud_runtime_session_artifact
from . import tool_approval_condition
from .account import CRUDAccount
from .account_halt import CRUDAccountHalt, crud_account_halt
from .account_milestone import CRUDAccountMilestone, crud_account_milestone
from .agent_control_command import CRUDAgentControlCommand
from .ai_model import CRUDAIModel
from .api_key import CRUDApiKey
from .gateway_subject import CRUDGatewaySubject
from .api_usage import CRUDApiUsage
from .approval_bypass import (
    CRUDApprovalBypass,
    crud_approval_bypass,
    get_active_bypass_async,
    record_bypass_use_async,
)
from .approval_event import CRUDApprovalEvent, crud_approval_event
from .approval_request import CRUDApprovalRequest, crud_approval_request
from .approval_workflow import CRUDApprovalWorkflow
from .attention_dismissal import (
    CRUDAttentionDismissal,
    crud_attention_dismissal,
)
from .audit_log import CRUDAuditLog
from .base import CRUDBase
from .budget import (
    CRUDBudgetPolicy,
    CRUDBudgetSpendActivity,
    crud_budget_policy,
    crud_budget_spend,
)
from .chat import crud_chat
from .ci_administration import crud_ci_administration
from .ci_execution import CRUDCiExecution, crud_ci_execution
from .ci_principal import CiAuthorizationContext, CRUDCiPrincipal, crud_ci_principal
from .ci_subscription import CRUDCiSubscription, crud_ci_subscription
from .cli_client import CRUDCliClient, crud_cli_client
from .cli_session import CRUDCliSession, crud_cli_session
from .comment import CRUDComment, crud_comment
from .copilot_import import (
    CRUDCopilotImportConnection,
    CRUDCopilotUsage,
    CRUDCopilotUserMapping,
    crud_copilot_import_connection,
    crud_copilot_usage,
    crud_copilot_user_mapping,
)
from .discovered_agent_candidate import (
    CRUDAccountDiscoverySalt,
    CRUDDiscoveredAgentCandidate,
)
from .discovery_observation import crud_discovery_observation
from .embedding import CRUDEmbeddingModel, CRUDIssueEmbedding
from .event import CRUDEvent, crud_event
from .flow import CRUDFlow  # Import CRUDFlow class
from .flow_execution import CRUDFlowExecution
from .flow_execution_log import CRUDFlowExecutionLog
from .flow_feedback import crud_flow_feedback
from .flow_runner import CRUDFlowRunner, crud_flow_runner
from .gateway_usage_search_document import CRUDGatewayUsageSearchDocument
from .identity_link import CRUDIdentityLink, crud_identity_link
from .instance import CRUDInstance, crud_instance
from .issue import CRUDIssue
from .issue_compliance_result import (
    CRUDIssueComplianceResult,
    issue_compliance_result,
)
from .issue_cost import crud_issue_cost
from .issue_duplicate import CRUDIssueDuplicate, crud_issue_duplicate
from .issue_lifecycle import crud_issue_lifecycle
from .issue_relationship import CRUDIssueRelationship
from .issue_set import CRUDIssueSet
from .managed_agent import CRUDManagedAgent
from .managed_agent_ai_model_binding import CRUDManagedAgentAIModelBinding
from .managed_agent_credential import CRUDManagedAgentCredential
from .managed_agent_enrollment import CRUDManagedAgentEnrollment
from .managed_oauth import CRUDManagedOAuth, crud_managed_oauth
from .mcp_server import CRUDMCPServer
from .mcp_tool import CRUDMCPTool
from .model_price_override import CRUDModelPriceOverride
from .oauth_app_installation import (
    CRUDOAuthAppInstallation,
    crud_oauth_app_installation,
)
from .oauth_token import CRUDOAuthToken, crud_oauth_token
from .optimization_job import CRUDOptimizationJob
from .organization import CRUDOrganization  # Removed create_organization import
from .permission import (
    CRUDPermission,
    CRUDRole,
    CRUDTeamRole,
    CRUDUserRole,
    crud_permission,
    crud_role,
    crud_team_role,
    crud_user_role,
)
from .plan import (
    CRUDMonthlyUsage,
    CRUDPlan,
    CRUDSubscription,
    monthly_usage,
    plan,
    subscription,
)
from .policy_notice_hit import CRUDPolicyNoticeHit, crud_policy_notice_hit
from .policy_snapshot import CRUDPolicySnapshot, crud_policy_snapshot
from .project import CRUDProject
from .provider_billing import (
    CRUDProviderBillingConnection,
    CRUDProviderBillingSnapshot,
)
from .registration_token import CRUDRegistrationToken, crud_registration_token
from .repricing_job import crud_repricing_job
from .runtime_session import CRUDRuntimeSession
from .runtime_session_activity import CRUDRuntimeSessionActivity
from .runtime_session_optimization_action import (
    CRUDRuntimeSessionOptimizationAction,
)
from .runtime_session_optimization_result import (
    CRUDRuntimeSessionOptimizationResult,
)
from .runtime_session_replay_run import CRUDRuntimeSessionReplayRun
from .secret_reference import CRUDSecretReference, crud_secret_reference
from .security_maintenance import crud_security_maintenance
from .session_embedding_setting import (
    CRUDSessionEmbeddingSetting,
    SessionEmbeddingConfigError,
)
from .session_saved_search import (
    CRUDSessionSavedSearch,
    SessionSavedSearchNameConflictError,
)
from .session_search_backfill_state import (
    CRUDSessionSearchBackfillState,
    crud_session_search_backfill_state,
)
from .session_search_document import (
    CRUDSessionSearchDocument,
    SessionSearchChunk,
)
from .spend_outlier import (
    CRUDSpendOutlierFinding,
    CRUDSpendOutlierSettings,
    crud_spend_outlier_finding,
    crud_spend_outlier_settings,
)
from .team import CRUDTeam, crud_team
from .tool_access_rule import CRUDToolAccessRule
from .tool_configuration import CRUDToolConfiguration
from .tool_cost_flag import CRUDToolCostFlag, crud_tool_cost_flag
from .tool_output_filter import CRUDToolOutputFilter, crud_tool_output_filter
from .tracker import CRUDTracker, crud_tracker
from .tracker_scope_rule import CRUDTrackerScopeRule
from .user import AmbiguousEmailError, CRUDUser, crud_user
from .user_invitation import CRUDUserInvitation, crud_user_invitation
from .visitor import CRUDVisitor, crud_visitor
from .webauthn_credential import CRUDWebAuthnCredential
from .webhook import CRUDWebhook

crud_account = CRUDAccount(Account)
crud_agent_control_command = CRUDAgentControlCommand(AgentControlCommand)
# crud_tracker is already instantiated in tracker.py
crud_organization = CRUDOrganization(Organization)
crud_project = CRUDProject(Project)
crud_issue = CRUDIssue(Issue)
crud_embedding_model = CRUDEmbeddingModel(EmbeddingModel)
crud_issue_embedding = CRUDIssueEmbedding(IssueEmbedding)
crud_api_key = CRUDApiKey(ApiKey)
crud_gateway_subject = CRUDGatewaySubject(GatewaySubject)
crud_api_usage = CRUDApiUsage(ApiUsage)
crud_audit_log = CRUDAuditLog(AuditLog)
crud_ai_model = CRUDAIModel(AIModel)
# crud_comment is already instantiated in its own file
crud_webauthn_credential = CRUDWebAuthnCredential(WebAuthnCredential)
crud_webhook = CRUDWebhook(Webhook)
crud_flow = CRUDFlow()  # Instantiate CRUDFlow
crud_flow_execution = CRUDFlowExecution()  # Instantiate CRUDFlowExecution
crud_flow_execution_log = CRUDFlowExecutionLog()
crud_tracker_scope_rule = CRUDTrackerScopeRule(TrackerScopeRule)
crud_issue_relationship = CRUDIssueRelationship(IssueRelationship)
crud_issue_set = CRUDIssueSet(IssueSet)
crud_gateway_usage_search_document = CRUDGatewayUsageSearchDocument(
    GatewayUsageSearchDocument
)
crud_account_discovery_salt = CRUDAccountDiscoverySalt(AccountDiscoverySalt)
crud_discovered_agent_candidate = CRUDDiscoveredAgentCandidate(DiscoveredAgentCandidate)
crud_managed_agent = CRUDManagedAgent(ManagedAgent)
crud_managed_agent_ai_model_binding = CRUDManagedAgentAIModelBinding(
    ManagedAgentAIModelBinding
)
crud_managed_agent_credential = CRUDManagedAgentCredential(ManagedAgentCredential)
crud_managed_agent_enrollment = CRUDManagedAgentEnrollment(ManagedAgentEnrollment)
crud_model_price_override = CRUDModelPriceOverride(ModelPriceOverride)
crud_provider_billing_connection = CRUDProviderBillingConnection(
    ProviderBillingConnection
)
crud_provider_billing_snapshot = CRUDProviderBillingSnapshot(ProviderBillingSnapshot)
crud_runtime_session = CRUDRuntimeSession(RuntimeSession)
crud_runtime_session_activity = CRUDRuntimeSessionActivity(RuntimeSessionActivity)
crud_session_search_document = CRUDSessionSearchDocument(SessionSearchDocument)
crud_session_embedding_setting = CRUDSessionEmbeddingSetting(SessionEmbeddingSetting)
crud_session_saved_search = CRUDSessionSavedSearch(SessionSavedSearch)
crud_runtime_session_optimization_action = CRUDRuntimeSessionOptimizationAction(
    RuntimeSessionOptimizationAction
)
crud_runtime_session_optimization_result = CRUDRuntimeSessionOptimizationResult(
    RuntimeSessionOptimizationResult
)
crud_runtime_session_replay_run = CRUDRuntimeSessionReplayRun(RuntimeSessionReplayRun)
crud_optimization_job = CRUDOptimizationJob(OptimizationJob)
crud_tool_configuration = CRUDToolConfiguration()  # Instantiate CRUDToolConfiguration
crud_mcp_server = CRUDMCPServer()  # Instantiate CRUDMCPServer
crud_mcp_tool = CRUDMCPTool()  # Instantiate CRUDMCPTool
crud_approval_workflow = CRUDApprovalWorkflow()  # Instantiate CRUDApprovalWorkflow
crud_tool_access_rule = CRUDToolAccessRule()  # Instantiate CRUDToolAccessRule


__all__ = [
    "crud_callback_key_binding",
    "crud_callback_receipt",
    "crud_ci_administration",
    "crud_chat",
    "CRUDManagedOAuth",
    "crud_managed_oauth",
    "crud_flow_feedback",
    "crud_issue_cost",
    "CRUDCliSession",
    "crud_cli_session",
    "crud_issue_lifecycle",
    "crud_security_maintenance",
    "CRUDBase",
    "CRUDAccount",
    "CRUDAccountHalt",
    "crud_account_halt",
    "CRUDAgentControlCommand",
    "crud_agent_control_command",
    "CRUDTracker",
    "CRUDTrackerScopeRule",
    "CRUDOrganization",
    # "crud_create_organization", # Removed export
    "CRUDProject",
    "CRUDIssue",
    "CRUDEmbeddingModel",
    "CRUDIssueEmbedding",
    "CRUDApiKey",
    "CiAuthorizationContext",
    "CRUDCiExecution",
    "crud_ci_execution",
    "CRUDCiSubscription",
    "crud_ci_subscription",
    "CRUDCiPrincipal",
    "crud_ci_principal",
    "CRUDApiUsage",
    "CRUDAuditLog",
    "CRUDComment",
    "CRUDAIModel",
    "CRUDSecretReference",
    "CRUDFlow",
    "CRUDFlowExecution",
    "CRUDFlowExecutionLog",
    "CRUDFlowRunner",
    "crud_flow_runner",
    "CRUDIssueComplianceResult",
    "CRUDIssueSet",
    "CRUDGatewayUsageSearchDocument",
    "CRUDAccountDiscoverySalt",
    "CRUDDiscoveredAgentCandidate",
    "crud_discovery_observation",
    "CRUDManagedAgent",
    "CRUDManagedAgentAIModelBinding",
    "CRUDManagedAgentCredential",
    "CRUDManagedAgentEnrollment",
    "CRUDModelPriceOverride",
    "CRUDToolConfiguration",
    "CRUDMCPServer",
    "CRUDMCPTool",
    "CRUDApprovalWorkflow",
    "CRUDApprovalRequest",
    "CRUDToolAccessRule",
    "CRUDPlan",
    "CRUDSubscription",
    "CRUDMonthlyUsage",
    "CRUDUser",
    "CRUDPermission",
    "CRUDRole",
    "CRUDUserRole",
    "CRUDTeamRole",
    "CRUDTeam",
    "CRUDUserInvitation",
    "CRUDRegistrationToken",
    "CRUDIssueDuplicate",
    "crud_account",
    "crud_tracker",
    "crud_tracker_scope_rule",
    "crud_organization",
    "crud_project",
    "crud_issue",
    "crud_embedding_model",
    "crud_issue_embedding",
    "crud_api_key",
    "crud_gateway_subject",
    "crud_api_usage",
    "crud_audit_log",
    "crud_comment",
    "crud_ai_model",
    "crud_secret_reference",
    "crud_restricted_runtime",
    "crud_webauthn_credential",
    "crud_webhook",
    "crud_flow",
    "crud_flow_execution",
    "crud_flow_execution_log",
    "crud_issue_relationship",
    "issue_compliance_result",
    "crud_issue_set",
    "crud_gateway_usage_search_document",
    "CRUDSessionSearchBackfillState",
    "crud_session_search_backfill_state",
    "crud_session_search_document",
    "crud_session_embedding_setting",
    "crud_session_saved_search",
    "SessionEmbeddingConfigError",
    "SessionSavedSearchNameConflictError",
    "SessionSearchChunk",
    "crud_account_discovery_salt",
    "crud_discovered_agent_candidate",
    "crud_managed_agent",
    "crud_managed_agent_ai_model_binding",
    "crud_managed_agent_credential",
    "crud_managed_agent_enrollment",
    "crud_model_price_override",
    "crud_provider_billing_connection",
    "crud_provider_billing_snapshot",
    "CRUDCopilotImportConnection",
    "CRUDCopilotUsage",
    "CRUDCopilotUserMapping",
    "crud_copilot_import_connection",
    "crud_copilot_usage",
    "crud_copilot_user_mapping",
    "crud_tool_configuration",
    "crud_mcp_server",
    "crud_mcp_tool",
    "crud_approval_workflow",
    "crud_approval_request",
    "CRUDApprovalEvent",
    "crud_approval_event",
    "CRUDApprovalBypass",
    "crud_approval_bypass",
    "get_active_bypass_async",
    "record_bypass_use_async",
    "crud_tool_access_rule",
    "plan",
    "subscription",
    "monthly_usage",
    "AmbiguousEmailError",
    "crud_user",
    "crud_permission",
    "crud_role",
    "crud_user_role",
    "crud_team_role",
    "crud_team",
    "crud_user_invitation",
    "crud_registration_token",
    "crud_issue_duplicate",
    "CRUDInstance",
    "CRUDCliClient",
    "crud_instance",
    "crud_cli_client",
    "CRUDEvent",
    "crud_event",
    "CRUDVisitor",
    "crud_visitor",
    "CRUDIdentityLink",
    "crud_identity_link",
    "CRUDPolicyNoticeHit",
    "crud_policy_notice_hit",
    "CRUDAccountMilestone",
    "crud_account_milestone",
    "CRUDAttentionDismissal",
    "crud_attention_dismissal",
    "CRUDSpendOutlierFinding",
    "CRUDSpendOutlierSettings",
    "crud_spend_outlier_finding",
    "crud_spend_outlier_settings",
    "CRUDOAuthAppInstallation",
    "crud_oauth_app_installation",
    "CRUDOAuthToken",
    "crud_oauth_token",
    "tool_approval_condition",
    "notification_preferences",
    "CRUDPolicySnapshot",
    "CRUDRuntimeSession",
    "CRUDRuntimeSessionActivity",
    "crud_policy_snapshot",
    "crud_runtime_session",
    "crud_runtime_session_activity",
    "crud_runtime_session_artifact",
    "crud_runtime_session_optimization_action",
    "crud_runtime_session_replay_run",
    "crud_runtime_session_optimization_result",
    "CRUDOptimizationJob",
    "crud_optimization_job",
    "crud_repricing_job",
    "CRUDBudgetPolicy",
    "CRUDBudgetSpendActivity",
    "crud_budget_policy",
    "crud_budget_spend",
    "CRUDToolCostFlag",
    "crud_tool_cost_flag",
    "CRUDToolOutputFilter",
    "crud_tool_output_filter",
]
