/**
 * Icon names that flows and presets store but the vendored Bootstrap icon
 * set does not ship. Each one used to cost a 404 per render and left a blank
 * slot; they map to the closest shipped glyph instead.
 */
export const ICON_ALIASES: Readonly<Record<string, string>> = {
  broom: 'eraser',
  beaker: 'clipboard-check',
  checklist: 'list-check',
  'document-report': 'file-earmark-text',
};

/** The shipped icon to render for a stored icon name. */
export function safeIconName(
  name: string | null | undefined
): string | undefined {
  if (!name) return name ?? undefined;
  return ICON_ALIASES[name] ?? name;
}

/** Copy of a flow or preset whose `icon` is a shipped icon. */
export function withSafeIcon<T>(item: T): T {
  if (!item || typeof item !== 'object') return item;
  const icon = (item as { icon?: unknown }).icon;
  if (typeof icon !== 'string' || !(icon in ICON_ALIASES)) return item;
  return { ...item, icon: ICON_ALIASES[icon] };
}
