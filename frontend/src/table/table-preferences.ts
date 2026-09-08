/**
 * What a list remembers about its own columns, per browser.
 *
 * The console's lists are configurable now (which columns are on, in which
 * order, how wide), and a configuration the operator has to redo on every
 * visit is not a configuration. It lives in `localStorage` rather than on the
 * server because it is a per-person, per-device view preference like
 * `preloop.<collection>.view_mode`, and because there is no endpoint for it.
 *
 * Every record carries a version. Column ids are part of the contract, so a
 * list that renames or drops a column bumps the version and every stored
 * record for it is dropped rather than half-applied: a stale record that
 * hides a column which no longer exists is invisible, and a stale record that
 * hides one that does is a bug report about a missing column.
 */

/** Bumped whenever the stored shape or the column-id contract changes. */
export const TABLE_PREFERENCES_VERSION = 1;

/** One column's sort direction, in the order the sorts were applied. */
export interface StoredSort {
  id: string;
  desc: boolean;
}

export interface StoredTablePreferences {
  /** Which columns are on. Absent ids keep the column's own default. */
  columnVisibility?: Record<string, boolean>;
  /** Ids in display order. Ids the list no longer has are ignored. */
  columnOrder?: string[];
  /** Pixel widths from the resize handles. */
  columnSizing?: Record<string, number>;
  /** Sorts, when the list opts into remembering them. */
  sorting?: StoredSort[];
}

interface StoredRecord extends StoredTablePreferences {
  v: number;
}

/** `preloop.table.<listId>`, one record per list. */
export function tablePreferencesKey(listId: string): string {
  return `preloop.table.${listId}`;
}

/**
 * The stored record, or null when there is none, it is unreadable, or it was
 * written by an older version of the list.
 *
 * Storage can throw (Safari private browsing) and the value can be anything a
 * user or an older build put there, so every failure reads as "no preference"
 * and the list renders its defaults.
 */
export function readTablePreferences(
  listId: string
): StoredTablePreferences | null {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(tablePreferencesKey(listId));
  } catch {
    return null;
  }
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  const record = parsed as StoredRecord;
  if (record.v !== TABLE_PREFERENCES_VERSION) return null;
  return {
    columnVisibility: plainBooleans(record.columnVisibility),
    columnOrder: plainStrings(record.columnOrder),
    columnSizing: plainNumbers(record.columnSizing),
    sorting: plainSorts(record.sorting),
  };
}

/** Writes the record, silently doing nothing when storage refuses. */
export function writeTablePreferences(
  listId: string,
  preferences: StoredTablePreferences
): void {
  const record: StoredRecord = {
    v: TABLE_PREFERENCES_VERSION,
    ...preferences,
  };
  try {
    localStorage.setItem(tablePreferencesKey(listId), JSON.stringify(record));
  } catch {
    // A full or disabled store costs the operator their column layout, not
    // their page.
  }
}

export function clearTablePreferences(listId: string): void {
  try {
    localStorage.removeItem(tablePreferencesKey(listId));
  } catch {
    // See writeTablePreferences.
  }
}

function plainBooleans(value: unknown): Record<string, boolean> | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const out: Record<string, boolean> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry === 'boolean') out[key] = entry;
  }
  return out;
}

function plainNumbers(value: unknown): Record<string, number> | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const out: Record<string, number> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry === 'number' && Number.isFinite(entry)) out[key] = entry;
  }
  return out;
}

function plainStrings(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.filter((entry): entry is string => typeof entry === 'string');
}

function plainSorts(value: unknown): StoredSort[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const out: StoredSort[] = [];
  for (const entry of value) {
    if (!entry || typeof entry !== 'object') continue;
    const sort = entry as Partial<StoredSort>;
    if (typeof sort.id !== 'string') continue;
    out.push({ id: sort.id, desc: Boolean(sort.desc) });
  }
  return out;
}
