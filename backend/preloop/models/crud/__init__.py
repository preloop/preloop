"""CRUD operation implementations."""

# Create CRUD instances for each model
from ..models import (
    Account,
    AgentControlCommand,
    ApiKey,
    ApiUsage,
    AuditLog,
    EmbeddingModel,
    Issue,
    IssueEmbedding,
    AIModel,
    Organization,
    Project,
    TrackerScopeRule,
    Webhook,
    IssueRelationship,
    IssueSet,
    GatewayUsageSearchDocument,
    ManagedAgent,
    ManagedAgentAIModelBinding,
    ManagedAgentCredential,
    ManagedAgentEnrollment,
    ModelPriceOverride,
    ProviderBillingConnection,
    ProviderBillingSnapshot,
    RuntimeSession,
    RuntimeSessionActivity,
    RuntimeSessionOptimizationAction,
    RuntimeSessionReplayRun,
    RuntimeSessionOptimizationResult,
)
from .account import CRUDAccount
from .agent_control_command import CRUDAgentControlCommand
from .api_key import CRUDApiKey
from .api_usage import CRUDApiUsage
from .audit_log import CRUDAuditLog
from .base import CRUDBase
from .comment import CRUDComment, crud_comment
from .embedding import CRUDEmbeddingModel, CRUDIssueEmbedding
from .flow import CRUDFlow  # Import CRUDFlow class
from .flow_execution import CRUDFlowExecution
from .flow_execution_log import CRUDFlowExecutionLog
from .issue import CRUDIssue
from .organization import CRUDOrganization  # Removed create_organization import
from .project import CRUDProject
from .tracker import CRUDTracker, crud_tracker
from .tracker_scope_rule import CRUDTrackerScopeRule
from .ai_model import CRUDAIModel
from .webhook import CRUDWebhook
from .issue_compliance_result import (
    CRUDIssueComplianceResult,
    issue_compliance_result,
)
from .issue_relationship import CRUDIssueRelationship
from .issue_set import CRUDIssueSet
from .managed_agent import CRUDManagedAgent
from .managed_agent_ai_model_binding import CRUDManagedAgentAIModelBinding
from .managed_agent_credential import CRUDManagedAgentCredential
from .managed_agent_enrollment import CRUDManagedAgentEnrollment
from .model_price_override import CRUDModelPriceOverride
from .provider_billing import (
    CRUDProviderBillingConnection,
    CRUDProviderBillingSnapshot,
)
from .tool_configuration import CRUDToolConfiguration
from .mcp_server import CRUDMCPServer
from .mcp_tool import CRUDMCPTool
from .approval_workflow import CRUDApprovalWorkflow
from .approval_bypass import (
    CRUDApprovalBypass,
    crud_approval_bypass,
    get_active_bypass_async,
    record_bypass_use_async,
)
from .approval_request import CRUDApprovalRequest, crud_approval_request
from .tool_access_rule import CRUDToolAccessRule
from .gateway_usage_search_document import CRUDGatewayUsageSearchDocument
from .plan import (
    CRUDPlan,
    CRUDSubscription,
    CRUDMonthlyUsage,
    plan,
    subscription,
    monthly_usage,
)
from .user import CRUDUser, crud_user
from .permission import (
    CRUDPermission,
    CRUDRole,
    CRUDUserRole,
    CRUDTeamRole,
    crud_permission,
    crud_role,
    crud_user_role,
    crud_team_role,
)
from .team import CRUDTeam, crud_team
from .user_invitation import CRUDUserInvitation, crud_user_invitation
from .registration_token import CRUDRegistrationToken, crud_registration_token
from .issue_duplicate import CRUDIssueDuplicate, crud_issue_duplicate
from .instance import CRUDInstance, crud_instance
from .cli_client import CRUDCliClient, crud_cli_client
from .event import CRUDEvent, crud_event
from .visitor import CRUDVisitor, crud_visitor
from .identity_link import CRUDIdentityLink, crud_identity_link
from .account_milestone import CRUDAccountMilestone, crud_account_milestone
from .oauth_app_installation import (
    CRUDOAuthAppInstallation,
    crud_oauth_app_installation,
)
from .oauth_token import CRUDOAuthToken, crud_oauth_token
from . import tool_approval_condition
from . import notification_preferences
from .policy_snapshot import CRUDPolicySnapshot, crud_policy_snapshot
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
from .tool_cost_flag import CRUDToolCostFlag, crud_tool_cost_flag
from .tool_output_filter import CRUDToolOutputFilter, crud_tool_output_filter
from .budget import (
    CRUDBudgetPolicy,
    CRUDBudgetSpendActivity,
    crud_budget_policy,
    crud_budget_spend,
)

crud_account = CRUDAccount(Account)
crud_agent_control_command = CRUDAgentControlCommand(AgentControlCommand)
# crud_tracker is already instantiated in tracker.py
crud_organization = CRUDOrganization(Organization)
crud_project = CRUDProject(Project)
crud_issue = CRUDIssue(Issue)
crud_embedding_model = CRUDEmbeddingModel(EmbeddingModel)
crud_issue_embedding = CRUDIssueEmbedding(IssueEmbedding)
crud_api_key = CRUDApiKey(ApiKey)
crud_api_usage = CRUDApiUsage(ApiUsage)
crud_audit_log = CRUDAuditLog(AuditLog)
crud_ai_model = CRUDAIModel(AIModel)
# crud_comment is already instantiated in its own file
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
crud_runtime_session_optimization_action = CRUDRuntimeSessionOptimizationAction(
    RuntimeSessionOptimizationAction
)
crud_runtime_session_optimization_result = CRUDRuntimeSessionOptimizationResult(
    RuntimeSessionOptimizationResult
)
crud_runtime_session_replay_run = CRUDRuntimeSessionReplayRun(RuntimeSessionReplayRun)
crud_tool_configuration = CRUDToolConfiguration()  # Instantiate CRUDToolConfiguration
crud_mcp_server = CRUDMCPServer()  # Instantiate CRUDMCPServer
crud_mcp_tool = CRUDMCPTool()  # Instantiate CRUDMCPTool
crud_approval_workflow = CRUDApprovalWorkflow()  # Instantiate CRUDApprovalWorkflow
crud_tool_access_rule = CRUDToolAccessRule()  # Instantiate CRUDToolAccessRule

__all__ = [
    "CRUDBase",
    "CRUDAccount",
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
    "CRUDApiUsage",
    "CRUDAuditLog",
    "CRUDComment",
    "CRUDAIModel",
    "CRUDSecretReference",
    "CRUDFlow",
    "CRUDFlowExecution",
    "CRUDFlowExecutionLog",
    "CRUDIssueComplianceResult",
    "CRUDIssueSet",
    "CRUDGatewayUsageSearchDocument",
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
    "crud_api_usage",
    "crud_audit_log",
    "crud_comment",
    "crud_ai_model",
    "crud_secret_reference",
    "crud_webhook",
    "crud_flow",
    "crud_flow_execution",
    "crud_flow_execution_log",
    "crud_issue_relationship",
    "issue_compliance_result",
    "crud_issue_set",
    "crud_gateway_usage_search_document",
    "crud_managed_agent",
    "crud_managed_agent_ai_model_binding",
    "crud_managed_agent_credential",
    "crud_managed_agent_enrollment",
    "crud_model_price_override",
    "crud_provider_billing_connection",
    "crud_provider_billing_snapshot",
    "crud_tool_configuration",
    "crud_mcp_server",
    "crud_mcp_tool",
    "crud_approval_workflow",
    "crud_approval_request",
    "CRUDApprovalBypass",
    "crud_approval_bypass",
    "get_active_bypass_async",
    "record_bypass_use_async",
    "crud_tool_access_rule",
    "plan",
    "subscription",
    "monthly_usage",
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
    "CRUDAccountMilestone",
    "crud_account_milestone",
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
    "crud_runtime_session_optimization_action",
    "crud_runtime_session_replay_run",
    "crud_runtime_session_optimization_result",
    "CRUDBudgetPolicy",
    "CRUDBudgetSpendActivity",
    "crud_budget_policy",
    "crud_budget_spend",
    "CRUDToolCostFlag",
    "crud_tool_cost_flag",
    "CRUDToolOutputFilter",
    "crud_tool_output_filter",
]
