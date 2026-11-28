/**
 * Data for Settings > Compliance.
 *
 * The account-wide counts and the decision history are core routes. The team
 * register (people, devices, open findings) is assembled from the device
 * fleet routes, which exist only where the fleet capability is reported; the
 * page never calls them otherwise.
 */
import { REST_API_PREFIX } from './api-path';
import { fetchWithAuth } from './api';
import {
  deviceLabel,
  extensionFindingHref,
  type DeviceFleetDevice,
  type DeviceFleetPage,
  type FleetFinding,
} from './device-fleet-api';

export interface ExtensionStateCounts {
  approved: number;
  unapproved: number;
  unknown: number;
  total: number;
}

export interface ExtensionDecisionEvent {
  id: string;
  created_at: string;
  agent_kind: string;
  extension_kind: string;
  extension_id: string;
  action: 'state_changed' | 'pin_added' | 'pin_removed';
  previous_state: string | null;
  new_state: string | null;
  content_hash: string | null;
  note: string | null;
  decided_by_user_id: string | null;
  decided_by: string | null;
}

async function agentsGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetchWithAuth(`${REST_API_PREFIX}/agents${path}`, {
    signal,
  });
  if (!response.ok)
    throw new Error(
      response.status === 403
        ? 'You do not have permission to view agent extensions.'
        : 'Compliance data could not be loaded. Retry to refresh it.'
    );
  return response.json();
}

export const getExtensionStateCounts = (
  signal?: AbortSignal
): Promise<ExtensionStateCounts> =>
  agentsGet('/discovery-extensions/state-counts', signal);

export async function getExtensionDecisions(
  limit = 20,
  signal?: AbortSignal
): Promise<ExtensionDecisionEvent[]> {
  return (
    await agentsGet<{ items: ExtensionDecisionEvent[] }>(
      `/extension-decisions?limit=${limit}`,
      signal
    )
  ).items;
}

/** One person (or Unassigned) on one device with what is out of policy. */
export interface ComplianceRow {
  person: string;
  personIds: string[];
  deviceKey: string;
  device: string;
  agentKinds: string[];
  extensions: string[];
  reasons: string[];
  since: string;
  lastReport: string | null;
  decideHref: string;
  findingIds: string[];
}

export interface ComplianceSummary {
  devicesReporting: number;
  devicesStale: number;
  devicesNeverReported: number;
  agentsOnboarded: number;
  agentsNotOnboarded: number;
  openFindings: number;
  findingsByReason: Record<string, number>;
}

export interface ComplianceRegister {
  summary: ComplianceSummary;
  rows: ComplianceRow[];
}

export const UNASSIGNED = 'Unassigned';

const text = (value: unknown): string =>
  typeof value === 'string' ? value.trim() : '';

/** The reason a finding is out of policy: the evaluator's verdict, else its rule. */
export function findingReason(finding: FleetFinding): string {
  if (finding.rule === 'unapproved_extension')
    return text(finding.evidence?.reason) || 'unapproved';
  return finding.rule;
}

export function reasonLabel(reason: string): string {
  const spaced = reason.replace(/_/g, ' ');
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function agentLabel(kind: string): string {
  const known: Record<string, string> = {
    claude_code: 'Claude Code',
    codex: 'Codex',
    codex_cli: 'Codex CLI',
    cursor: 'Cursor',
    gemini_cli: 'Gemini CLI',
    copilot: 'Copilot',
  };
  return known[kind] ?? reasonLabel(kind);
}

function personOf(device: DeviceFleetDevice | undefined): {
  label: string;
  ids: string[];
} {
  const users = device?.users ?? [];
  const labels = [...new Set(users.map((u) => u.label).filter(Boolean))];
  if (labels.length === 0) return { label: UNASSIGNED, ids: [] };
  return {
    label: labels.join(', '),
    ids: [
      ...new Set(users.map((u) => u.id).filter((id): id is string => !!id)),
    ],
  };
}

function agentsOf(
  findings: FleetFinding[],
  device?: DeviceFleetDevice
): string[] {
  const kinds = findings
    .map((f) => text(f.evidence?.agent_kind))
    .filter(Boolean);
  if (kinds.length === 0 && device)
    kinds.push(
      ...device.agents
        .filter((a) => !a.managed_agent_id && a.status !== 'dismissed')
        .map((a) => a.kind)
    );
  return [...new Set(kinds)].sort();
}

/**
 * Join open findings to their devices: one row per device and its people,
 * oldest condition first. Findings with no device fall under "Unassigned".
 */
export function buildRegister(
  fleet: Pick<DeviceFleetPage, 'items'>,
  findings: FleetFinding[]
): ComplianceRegister {
  const devices = new Map(fleet.items.map((d) => [d.device_key, d]));
  const open = findings.filter((f) => f.state === 'open');
  const byReason: Record<string, number> = {};
  for (const finding of open) {
    const reason = findingReason(finding);
    byReason[reason] = (byReason[reason] ?? 0) + 1;
  }
  const groups = new Map<string, FleetFinding[]>();
  for (const finding of open) {
    const key = finding.device_key ?? '';
    groups.set(key, [...(groups.get(key) ?? []), finding]);
  }
  const rows: ComplianceRow[] = [...groups.entries()].map(([key, items]) => {
    const device = devices.get(key);
    const person = personOf(device);
    const owners = items
      .map((f) => f.user_id)
      .filter((id): id is string => !!id);
    const since = items
      .map((f) => f.opened_at)
      .sort((a, b) => Date.parse(a) - Date.parse(b))[0];
    const oldest = items.find((f) => f.opened_at === since) ?? items[0];
    const extensions = items
      .filter((f) => f.rule === 'unapproved_extension')
      .map((f) => {
        const id = text(f.evidence?.extension_id);
        const kind = text(f.evidence?.kind);
        return id && kind ? `${id} (${kind})` : id;
      })
      .filter(Boolean);
    const decide =
      items.find((f) => f.rule === 'unapproved_extension') ?? oldest;
    return {
      person: person.label,
      personIds: [...new Set([...person.ids, ...owners])],
      deviceKey: key,
      device: key
        ? deviceLabel(device ?? { hostname: null, device_key: key })
        : 'No device',
      agentKinds: agentsOf(items, device),
      extensions: [...new Set(extensions)].sort(),
      reasons: [...new Set(items.map(findingReason))].sort(),
      since,
      lastReport: device?.last_seen_at ?? null,
      decideHref:
        decide.rule === 'unapproved_extension'
          ? extensionFindingHref(decide)
          : decide.href || extensionFindingHref(decide),
      findingIds: items.map((f) => f.id),
    };
  });
  rows.sort((a, b) => Date.parse(a.since) - Date.parse(b.since));
  let reporting = 0;
  let stale = 0;
  let never = 0;
  let onboarded = 0;
  let notOnboarded = 0;
  for (const device of fleet.items) {
    if (device.collection_state === 'stale') stale += 1;
    else if (device.collection_state === 'none' || !device.collection_state)
      never += 1;
    else reporting += 1;
    onboarded += device.agents_onboarded ?? 0;
    notOnboarded += device.agents_not_onboarded ?? 0;
  }
  return {
    summary: {
      devicesReporting: reporting,
      devicesStale: stale,
      devicesNeverReported: never,
      agentsOnboarded: onboarded,
      agentsNotOnboarded: notOnboarded,
      openFindings: open.length,
      findingsByReason: byReason,
    },
    rows,
  };
}

export interface ComplianceFilters {
  person?: string;
  device?: string;
  agent?: string;
  reason?: string;
}

export function filterRows(
  rows: ComplianceRow[],
  filters: ComplianceFilters
): ComplianceRow[] {
  return rows.filter(
    (row) =>
      (!filters.person || row.person === filters.person) &&
      (!filters.device || row.deviceKey === filters.device) &&
      (!filters.agent || row.agentKinds.includes(filters.agent)) &&
      (!filters.reason || row.reasons.includes(filters.reason))
  );
}

function csvCell(value: string): string {
  // A leading formula character is neutralised so a spreadsheet never runs it.
  const safe = /^[=+\-@\t\r]/.test(value) ? `'${value}` : value;
  return /[",\n\r]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

export const CSV_COLUMNS = [
  'person',
  'device',
  'device_key',
  'agents',
  'extensions',
  'reasons',
  'out_of_policy_since',
  'last_report',
  'finding_count',
] as const;

/** The register as CSV; the first line records when it was taken. */
export function registerCsv(rows: ComplianceRow[], asOf: Date): string {
  const lines = [
    `# Compliance register as of ${asOf.toISOString()}`,
    CSV_COLUMNS.join(','),
    ...rows.map((row) =>
      [
        row.person,
        row.device,
        row.deviceKey,
        row.agentKinds.join('; '),
        row.extensions.join('; '),
        row.reasons.join('; '),
        row.since,
        row.lastReport ?? '',
        String(row.findingIds.length),
      ]
        .map(csvCell)
        .join(',')
    ),
  ];
  return lines.join('\n') + '\n';
}
