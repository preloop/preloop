import type { Tags } from '../../../hierarchy-api';

const KEY = /^[a-z0-9._/-]{1,63}$/;

/**
 * Parse `key=value` pairs separated by commas or new lines. Keys follow the
 * server's rule (lowercase, 1 to 63 of `a-z 0-9 . _ / -`); anything else is
 * reported, not dropped.
 */
export function parseTags(input: string): { tags: Tags; errors: string[] } {
  const tags: Tags = {};
  const errors: string[] = [];
  for (const raw of input.split(/[,\n]/)) {
    const part = raw.trim();
    if (!part) continue;
    const eq = part.indexOf('=');
    const key = (eq === -1 ? part : part.slice(0, eq)).trim();
    const value = eq === -1 ? '' : part.slice(eq + 1).trim();
    if (!KEY.test(key)) {
      errors.push(`"${key}" is not a valid tag key`);
      continue;
    }
    if (value.length > 128) {
      errors.push(`The value of "${key}" is longer than 128 characters`);
      continue;
    }
    tags[key] = value;
  }
  return { tags, errors };
}

export function formatTags(tags: Tags | undefined | null): string {
  return Object.entries(tags ?? {})
    .map(([key, value]) => `${key}=${value}`)
    .join(', ');
}
