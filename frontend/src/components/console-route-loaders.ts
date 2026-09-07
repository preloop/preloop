/** Console views register only when their route is visited. */
export const consoleRouteLoaders = {
  'console-shell': () => import('../views/authed/console-shell'),
  'oauth-consent-view': () => import('../views/authed/oauth-consent-view'),
  'dashboard-view': () =>
    import('../views/authed/dashboard-control-plane-view'),
  'trackers-view': () => import('../views/authed/trackers-view'),
  'tracker-detail-view': () => import('../views/authed/tracker-detail-view'),
  'tracker-issue-view': () => import('../views/authed/tracker-issue-view'),
  'tools-view': () => import('../views/authed/tools-view'),
  'issues-view': () => import('../views/authed/issues-view'),
  'issues-compliance-view': () =>
    import('../views/authed/issues-compliance-view'),
  'issues-dependencies-view': () =>
    import('../views/authed/issues-dependencies-view'),
  'duplicates-view': () => import('../views/authed/issues/duplicates-view'),
  'assignments-view': () => import('../views/authed/issues/assignments-view'),
  'api-usage-view': () => import('../views/authed/api-usage-view'),
  'cost-view': () => import('../views/authed/cost-view'),
  'api-keys-view': () => import('../views/authed/settings/api-keys-view'),
  'api-key-view': () => import('../views/authed/settings/api-key-view'),
  'ai-models-view': () => import('../views/authed/settings/ai-models-view'),
  'ai-model-detail-view': () =>
    import('../views/authed/settings/ai-model-detail-view'),
  'profile-view': () => import('../views/authed/settings/profile-view'),
  'security-view': () => import('../views/authed/settings/security-view'),
  'appearance-view': () => import('../views/authed/settings/appearance-view'),
  'account-view': () => import('../views/authed/settings/account-view'),
  'user-management-view': () =>
    import('../views/authed/settings/user-management-view'),
  'team-management-view': () =>
    import('../views/authed/settings/team-management-view'),
  'invitation-management-view': () =>
    import('../views/authed/settings/invitation-management-view'),
  'notification-preferences-view': () =>
    import('../views/authed/notification-preferences-view'),
  'flows-view': () => import('../views/authed/flows-view'),
  'runners-view': () => import('../views/authed/runners-view'),
  'flow-view': () => import('../views/authed/flow-view'),
  'flow-executions-view': () => import('../views/authed/flow-executions-view'),
  'flow-execution-view': () => import('../views/authed/flow-execution-view'),
  'runtime-sessions-view': () =>
    import('../views/authed/runtime-sessions-view'),
  'approval-view': () => import('../views/authed/approval-view'),
  'approvals-view': () => import('../views/authed/approvals-view'),
  'policies-view': () => import('../views/authed/policies-view'),
  'audit-view': () => import('../views/authed/audit-view'),
  'agents-view': () => import('../views/authed/agents-view'),
  'agent-detail-view': () => import('../views/authed/agent-detail-view'),
  'agent-talk-view': () => import('../views/authed/agent-talk-view'),
  'attention-view': () => import('../views/authed/attention-view'),
};
