import { REST_API_PREFIX } from '../api-path';
import { fetchWithAuth } from '../api';
import type { DeliveryReportAttention } from '../delivery-reports-api';
import {
  fleetAccess,
  fleetFindingTitle,
  type FleetFinding,
} from '../device-fleet-api';

/** Generic plugin contract; processing and wording belong to the provider. */
export interface ExtensionAttentionItem {
  id: string;
  source: string;
  title: string;
  detail: string;
  href: string;
  severity: 'info' | 'action';
  opened_at: string;
  delivery_report?: DeliveryReportAttention | null;
}

export interface LoadedExtensionAttention {
  items: PreparedExtensionAttentionItem[];
  deliveryReports: DeliveryReportAttention[];
}

export interface PreparedExtensionAttentionItem extends ExtensionAttentionItem {
  dismissalId: string;
  fingerprint: string;
}

export async function prepareExtensionAttentionItem(
  item: ExtensionAttentionItem
): Promise<PreparedExtensionAttentionItem> {
  const digest = async (value: unknown): Promise<string> => {
    const bytes = new TextEncoder().encode(JSON.stringify(value));
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    return Array.from(new Uint8Array(hash), (byte) =>
      byte.toString(16).padStart(2, '0')
    ).join('');
  };
  return {
    ...item,
    dismissalId: `extension:${await digest([item.source, item.id])}`,
    fingerprint: await digest([
      item.opened_at,
      item.title,
      item.detail,
      item.href,
      item.severity,
    ]),
  };
}

export async function loadExtensionAttentionItems(): Promise<LoadedExtensionAttention> {
  const empty: LoadedExtensionAttention = { items: [], deliveryReports: [] };
  try {
    const response = await fetchWithAuth(
      REST_API_PREFIX +
        '/attention/extension-items?include_delivery_reports=true',
      {
        passive: true,
      }
    );
    if (!response.ok) return empty;
    const items: unknown = await response.json();
    if (!Array.isArray(items)) return empty;
    const reports = items.filter(
      (item): item is ExtensionAttentionItem =>
        item?.source === 'core:delivery_report' && item.delivery_report != null
    );
    const extensions = items.filter(
      (item) => item?.source !== 'core:delivery_report'
    ) as ExtensionAttentionItem[];
    const prepared = await Promise.all(
      extensions.map(prepareExtensionAttentionItem)
    );
    return {
      items: await nameFleetFindings(prepared),
      deliveryReports: reports.map((item) => item.delivery_report!),
    };
  } catch {
    return empty;
  }
}

/** Enrich only existing authorized contributions; never add findings to the inbox. */
async function nameFleetFindings(
  items: PreparedExtensionAttentionItem[]
): Promise<PreparedExtensionAttentionItem[]> {
  const source = 'fleet_evidence:findings';
  if (!items.some((item) => item.source === source)) return items;
  try {
    if (!(await fleetAccess()).view) return items;
    const response = await fetchWithAuth(
      REST_API_PREFIX + '/fleet/findings?state=open&rule=unapproved_extension',
      { passive: true }
    );
    if (!response.ok) return items;
    const data = await response.json();
    if (!Array.isArray(data.items)) return items;
    const findings = new Map<string, FleetFinding>(
      data.items
        .filter(
          (finding: FleetFinding) => finding?.rule === 'unapproved_extension'
        )
        .map((finding: FleetFinding) => [finding.id, finding])
    );
    return items.map((item) => {
      const finding =
        item.source === source ? findings.get(item.id) : undefined;
      // Keep the provider's incident fingerprint: a failed evidence read must
      // not revive a dismissed item or make its dismissal stop matching.
      return finding ? { ...item, title: fleetFindingTitle(finding) } : item;
    });
  } catch {
    return items;
  }
}
