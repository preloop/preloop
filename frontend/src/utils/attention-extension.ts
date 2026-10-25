import { REST_API_PREFIX } from '../api-path';
import { fetchWithAuth } from '../api';

/** Generic plugin contract; processing and wording belong to the provider. */
export interface ExtensionAttentionItem {
  id: string;
  source: string;
  title: string;
  detail: string;
  href: string;
  severity: 'info' | 'action';
  opened_at: string;
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

export async function loadExtensionAttentionItems(): Promise<
  PreparedExtensionAttentionItem[]
> {
  try {
    const response = await fetchWithAuth(
      REST_API_PREFIX + '/attention/extension-items',
      {
        passive: true,
      }
    );
    if (!response.ok) return [];
    const items: unknown = await response.json();
    return Array.isArray(items)
      ? await Promise.all(items.map(prepareExtensionAttentionItem))
      : [];
  } catch {
    return [];
  }
}
