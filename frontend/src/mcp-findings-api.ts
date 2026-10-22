import { REST_API_PREFIX } from './api-path';
import { fetchWithAuth, getFeatures } from './api';

/** Metadata only. Registry names, URLs and tool payloads never cross this API. */
export interface MCPFinding {
  id: string;
  server_id: string;
  registry_revision: string;
  evidence_class: 'configured_route' | 'observed_call';
  source: 'account_registry' | 'preloop_mcp_dispatch';
  approval_state: 'approved' | 'unknown' | 'unapproved';
  state: 'open' | 'resolved' | 'ignored';
  first_seen_at: string;
  last_seen_at: string;
  ignored_until: string | null;
  agent_id: string | null;
  person_id: string | null;
  limitation: string;
}

export async function loadMCPFindings(): Promise<MCPFinding[]> {
  try {
    const features = await getFeatures();
    if (features?.features?.mcp_findings !== true) return [];
    const response = await fetchWithAuth(REST_API_PREFIX + '/mcp-findings', {
      passive: true,
    });
    if (!response.ok) return [];
    const body = await response.json();
    return Array.isArray(body.items) ? body.items : [];
  } catch {
    return [];
  }
}

export async function decideMCPFinding(
  id: string,
  action: 'approve' | 'ignore'
): Promise<void> {
  const body =
    action === 'approve'
      ? { scope: 'account_registry_revision' }
      : {
          scope: 'finding',
          expires_at: new Date(Date.now() + 7 * 86400000).toISOString(),
        };
  const response = await fetchWithAuth(
    `${REST_API_PREFIX}/mcp-findings/${encodeURIComponent(id)}/${action}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }
  );
  if (!response.ok)
    throw new Error('Could not save the MCP inventory decision');
}
