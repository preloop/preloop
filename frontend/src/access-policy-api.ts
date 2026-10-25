import { REST_API_PREFIX } from './api-path';
/** Secret-free DTOs from the enforced account-hierarchy access API. */
import {
  extractErrorMessage,
  fetchWithAuth,
  getRoles,
  getTeams,
  getUsers,
  hasPermission,
} from './api';
import { ConflictError, NotFoundError } from './hierarchy-api';
import { CapabilityOffError } from './capabilities';

export type Action =
  | 'resource:view'
  | 'model:invoke'
  | 'tool:call'
  | 'flow:run'
  | 'runner:accept'
  | 'resource:share';
export const RESOURCE_ACTIONS: Record<string, Action[]> = {
  ai_model: ['resource:view', 'model:invoke'],
  mcp_server: ['resource:view'],
  managed_agent: ['resource:view'],
  flow: ['resource:view', 'flow:run'],
  runner: ['resource:view', 'runner:accept'],
  mcp_tool: ['tool:call'],
};
export interface Selector {
  ids?: string[];
  kind?: 'user' | 'agent' | 'api_key' | 'flow' | null;
  teams?: string[];
  roles?: string[];
  matchLabels?: Record<string, string>;
  match_labels?: Record<string, string>;
  [key: string]: unknown;
}
export interface PolicyRule {
  id?: string | null;
  name: string;
  effect: 'permit' | 'forbid';
  actions: Action[];
  subject_selector: Selector;
  resource_type: string | null;
  resource_selector: Selector;
  conditions: Record<string, unknown>;
  scope: 'self' | 'subaccounts' | 'self_and_subaccounts';
  priority: number;
  is_enabled: boolean;
}
export interface StoredRule extends PolicyRule {
  id: string;
  version: number;
  account_id: string;
  inherited: boolean;
  editable: boolean;
}
export interface Requirement {
  action: Action;
  mode: 'require_permit';
  permit_rule_ids: string[];
}
export interface ResourceUpdate {
  rules: PolicyRule[];
  requirements: Requirement[];
}
export interface ResourceDocument extends ResourceUpdate {
  account_id: string;
  resource_type: string;
  resource_id: string;
  version: number;
  generation: number;
}
export interface ResourceSummary {
  document: ResourceDocument;
  supported_actions: Action[];
  can_manage: boolean;
  inherited_policies: Array<{
    account_id: string;
    resource_type: string;
    resource_id: string;
    rule_ids: string[];
    requirements: Requirement[];
  }>;
  applied_rules: Array<
    PolicyRule & { account_id: string; inherited: boolean; editable: false }
  >;
  sharing: { supported: boolean; can_manage: boolean; intents: unknown[] };
}
export type Subject = {
  kind: 'user' | 'agent' | 'api_key' | 'flow';
  id: string;
};
export interface ResourcePreview {
  version: number;
  generation: number;
  changes: Array<
    Subject & {
      flow_id: string | null;
      action: Action;
      before: 'allow' | 'deny';
      after: 'allow' | 'deny';
    }
  >;
  scope: 'policy_only';
  runtime_context: 'unknown';
  expires_at: string;
  preview_token: string;
}
export interface ResourceExplanation {
  decision: 'deny' | 'conditional';
  policy_decision: 'allow' | 'deny';
  reason: string;
  rule_ids: string[];
  rbac: 'allow' | 'deny' | 'unknown';
  runtime_context: 'unknown';
  unverified_ceilings: string[];
}
export interface SubjectOption {
  kind: 'user' | 'team' | 'role';
  id: string;
  label: string;
}
export async function accessRequest<T>(
  path: string,
  init: RequestInit = {},
  onNotFound: 'not-found' | 'capability-off' = 'not-found'
): Promise<T> {
  const response = await fetchWithAuth(path, {
    ...init,
    ...(init.body ? { headers: { 'Content-Type': 'application/json' } } : {}),
  });
  if (response.status === 409) throw new ConflictError();
  if (response.status === 404) {
    if (onNotFound === 'capability-off') throw new CapabilityOffError(path);
    throw new NotFoundError('This resource or access endpoint is unavailable.');
  }
  if (!response.ok)
    throw new Error(
      extractErrorMessage(
        await response.json().catch(() => null),
        'Could not load or save access policy'
      )
    );
  return response.status === 204 ? (undefined as T) : response.json();
}
const base = (kind: string, id: string) =>
  `${REST_API_PREFIX}/access/resources/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`;
export const getResourceAccess = (
  kind: string,
  id: string
): Promise<ResourceSummary> => accessRequest(base(kind, id));
export const previewResourceAccess = (
  kind: string,
  id: string,
  document: ResourceUpdate,
  version: number
): Promise<ResourcePreview> =>
  accessRequest(`${base(kind, id)}/preview`, {
    method: 'POST',
    body: JSON.stringify({ document, expected_version: version }),
  });
export const putResourceAccess = (
  kind: string,
  id: string,
  document: ResourceUpdate,
  version: number,
  token: string
): Promise<ResourceSummary> =>
  accessRequest(base(kind, id), {
    method: 'PUT',
    body: JSON.stringify({
      document,
      expected_version: version,
      preview_token: token,
    }),
  });
export const explainResourceAccess = (
  kind: string,
  id: string,
  subject: Subject,
  action: Action,
  flow_id?: string
): Promise<ResourceExplanation> =>
  accessRequest(`${base(kind, id)}/explain`, {
    method: 'POST',
    body: JSON.stringify({ subject, action, ...(flow_id ? { flow_id } : {}) }),
  });

/** Explicit fields only: CRUD metadata and unrelated resource/secret data never enter writes. */
export function ruleDefinition(rule: PolicyRule): PolicyRule {
  return structuredClone(
    Object.fromEntries(
      [
        'id',
        'name',
        'effect',
        'actions',
        'subject_selector',
        'resource_type',
        'resource_selector',
        'conditions',
        'scope',
        'priority',
        'is_enabled',
      ]
        .filter((key) => key in rule)
        .map((key) => [key, rule[key as keyof PolicyRule]])
    )
  ) as unknown as PolicyRule;
}
export const emptyRule = (): PolicyRule => ({
  name: '',
  effect: 'permit',
  actions: [],
  subject_selector: {},
  resource_type: null,
  resource_selector: {},
  conditions: {},
  scope: 'self',
  priority: 0,
  is_enabled: true,
});
export const labelsOf = (selector: Selector) =>
  selector.matchLabels ?? selector.match_labels ?? {};
export const listPolicyRules = (): Promise<StoredRule[]> =>
  accessRequest(REST_API_PREFIX + '/access/rules', {}, 'capability-off');
export const writePolicyRule = (
  rule: PolicyRule,
  version?: number
): Promise<StoredRule> => {
  const value = ruleDefinition(rule);
  if (version === undefined) delete value.id;
  return accessRequest(
    `${REST_API_PREFIX}/access/rules${version === undefined ? '' : '/' + encodeURIComponent(String(rule.id))}`,
    {
      method: version === undefined ? 'POST' : 'PUT',
      body: JSON.stringify({
        ...value,
        ...(version === undefined ? {} : { expected_version: version }),
      }),
    }
  );
};
export const deletePolicyRule = (rule: StoredRule): Promise<void> =>
  accessRequest(
    `${REST_API_PREFIX}/access/rules/${encodeURIComponent(rule.id)}?expected_version=${rule.version}`,
    { method: 'DELETE' }
  );
export interface ModePreview {
  action: Action;
  mode: 'additive' | 'require_permit';
  generation: number;
  losing_subjects: Array<
    Subject & {
      resources: Array<{ type: string; id: string; flow_id?: string }>;
    }
  >;
  expires_at: string;
  preview_token: string;
}
export const previewAccountMode = (
  action: Action,
  mode: string
): Promise<ModePreview> =>
  accessRequest(REST_API_PREFIX + '/access/modes/preview', {
    method: 'POST',
    body: JSON.stringify({ action, mode }),
  });
export const saveAccountMode = (preview: ModePreview): Promise<unknown> =>
  accessRequest(REST_API_PREFIX + '/access/modes', {
    method: 'PUT',
    body: JSON.stringify({
      action: preview.action,
      mode: preview.mode,
      preview_token: preview.preview_token,
    }),
  });
export async function getAccountModes(): Promise<
  Record<string, 'additive' | 'require_permit'>
> {
  const policy = await accessRequest<{
    access_rule_mode?: Record<string, 'additive' | 'require_permit'>;
  }>(
    REST_API_PREFIX + '/policies/export?format=json&include_mcp_servers=false'
  );
  return policy.access_rule_mode ?? {};
}

export const SUBJECT_DIRECTORY_LIMIT = 500;
const SUBJECT_PAGE_SIZE = 100;
const SUBJECT_MAX_PAGES = SUBJECT_DIRECTORY_LIMIT / SUBJECT_PAGE_SIZE;
/** At most five pages per directory; use advanced UUID selectors beyond the name list. */
export async function loadSubjectOptions(
  permissions: string[] | null
): Promise<SubjectOption[]> {
  const options: SubjectOption[] = [];
  if (hasPermission(permissions, 'view_users')) {
    for (
      let skip = 0, pageIndex = 0;
      pageIndex < SUBJECT_MAX_PAGES;
      pageIndex++
    ) {
      const page = await getUsers(skip, SUBJECT_PAGE_SIZE);
      options.push(
        ...page.users.slice(0, SUBJECT_PAGE_SIZE).map((user) => ({
          kind: 'user' as const,
          id: user.id,
          label: user.full_name || user.username || user.email || user.id,
        }))
      );
      skip += page.users.length;
      if (!page.users.length || skip >= page.total) break;
    }
    const roles = await getRoles();
    options.push(
      ...roles.roles.slice(0, SUBJECT_DIRECTORY_LIMIT).map((role) => ({
        kind: 'role' as const,
        id: role.id,
        label: role.name,
      }))
    );
  }
  if (hasPermission(permissions, 'view_teams')) {
    for (
      let skip = 0, pageIndex = 0;
      pageIndex < SUBJECT_MAX_PAGES;
      pageIndex++
    ) {
      const page = await getTeams(skip, SUBJECT_PAGE_SIZE);
      options.push(
        ...page.teams.slice(0, SUBJECT_PAGE_SIZE).map((team) => ({
          kind: 'team' as const,
          id: team.id,
          label: team.name,
        }))
      );
      skip += page.teams.length;
      if (!page.teams.length || skip >= page.total) break;
    }
  }
  return options;
}
