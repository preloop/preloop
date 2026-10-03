/**
 * Console client for team budgets.
 *
 * Served by an extension plugin (capability `team_budgets`); the open-source
 * server has no such endpoints. A 404 on a collection means "capability off"
 * and is raised as {@link CapabilityOffError}. Only the gated Teams tab and
 * the Cost page (when the capability is on) call this module.
 */
import { extractErrorMessage, fetchWithAuth, getUserProfile } from './api';
import { CapabilityOffError } from './capabilities';

export type TeamBudgetPeriod =
  'hourly' | 'daily' | 'weekly' | 'monthly' | 'yearly' | 'all_time';

export interface TeamBudget {
  id: string;
  subject_type: 'team';
  team_id: string;
  team_name: string;
  period: TeamBudgetPeriod;
  hard_limit_usd: number | null;
  soft_limit_usd: number | null;
  notify_on_soft: boolean;
  notify_on_hard: boolean;
  model_alias: string | null;
  current_spend_usd: number | null;
}

export interface TeamBudgetCreate {
  team_id: string;
  period: TeamBudgetPeriod;
  hard_limit_usd?: number | null;
  soft_limit_usd?: number | null;
  notify_on_soft?: boolean;
  notify_on_hard?: boolean;
}

export interface TeamUsageRow {
  team_id: string;
  team_name: string;
  member_count: number;
  cost_usd: number;
}

const enc = encodeURIComponent;

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetchWithAuth(path, {
    ...init,
    ...(init.body !== undefined
      ? { headers: { 'Content-Type': 'application/json' } }
      : {}),
  });
  if (response.status === 404 && (init.method || 'GET') === 'GET') {
    throw new CapabilityOffError(path);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(extractErrorMessage(body, 'Request failed'));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function base(): Promise<string> {
  const accountId = (await getUserProfile()).account_id;
  return `/api/v1/accounts/${enc(accountId)}`;
}

export async function listTeamBudgets(): Promise<TeamBudget[]> {
  const body = await call<{ items?: TeamBudget[] }>(
    `${await base()}/team-budgets`
  );
  return Array.isArray(body?.items) ? body.items : [];
}

export async function createTeamBudget(
  payload: TeamBudgetCreate
): Promise<TeamBudget> {
  return call<TeamBudget>(`${await base()}/team-budgets`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function deleteTeamBudget(id: string): Promise<void> {
  await call<void>(`${await base()}/team-budgets/${enc(id)}`, {
    method: 'DELETE',
  });
}

export async function getTeamUsage(params: {
  start?: string;
  end?: string;
}): Promise<TeamUsageRow[]> {
  const query = new URLSearchParams();
  if (params.start) query.set('start', params.start);
  if (params.end) query.set('end', params.end);
  const suffix = query.toString() ? `?${query}` : '';
  const body = await call<{ rows?: TeamUsageRow[] }>(
    `${await base()}/usage/teams${suffix}`
  );
  return Array.isArray(body?.rows) ? body.rows : [];
}

/** Map of team id to team name, from the team budgets (for row labels). */
export function teamNamesFrom(budgets: TeamBudget[]): Record<string, string> {
  const names: Record<string, string> = {};
  for (const budget of budgets) names[budget.team_id] = budget.team_name;
  return names;
}
