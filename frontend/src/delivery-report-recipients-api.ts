/** Enterprise report roles and recipients; omitted fields stay unchanged. */
import { extractErrorMessage, fetchWithAuth } from './api';
import { REST_API_PREFIX } from './api-path';
import {
  DeliveryReportPolicyError,
  type RecipientRule,
} from './delivery-report-policy-api';

export type ReportRole = 'owner' | 'lead' | 'member' | 'viewer';

export interface ReportRecipient {
  user_id: string;
  name: string | null;
  email: string | null;
  rbac_roles: string[];
  report_role: ReportRole;
  role_source: string;
  delivery: 'full_report' | 'short_update' | 'none';
  opted_out: boolean;
}

export interface ReportRecipients {
  per_member: boolean;
  recipients: RecipientRule;
  sponsoring_lead_user_id: string | null;
  opted_out_user_ids: string[];
  members: ReportRecipient[];
}

export interface ReportRecipientsChanges {
  per_member?: boolean;
  recipients?: RecipientRule;
  sponsoring_lead_user_id?: string | null;
  roles?: Record<string, ReportRole | null>;
  opted_out_user_ids?: string[];
}

const PATH = `${REST_API_PREFIX}/delivery-report/recipients`;

async function parse(response: Response): Promise<ReportRecipients> {
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new DeliveryReportPolicyError(
      body?.detail?.message ??
        extractErrorMessage(body, 'Could not load or save report recipients.'),
      response.status,
      body?.detail?.code ?? null
    );
  }
  return body as ReportRecipients;
}

export async function getReportRecipients(): Promise<ReportRecipients> {
  return parse(await fetchWithAuth(PATH));
}

export async function saveReportRecipients(
  changes: ReportRecipientsChanges
): Promise<ReportRecipients> {
  return parse(
    await fetchWithAuth(PATH, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(changes),
    })
  );
}
