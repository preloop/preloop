/**
 * Pure helpers for the records console.
 *
 * The chain proves order after sealing. It does not prove a row was true
 * when it was written. That sentence is product copy, not a comment.
 */

export const CHAIN_HONESTY =
  'Shows the rows were not reordered, removed or edited after sealing; not that they were true when written.';

export const OBJECT_LOCK_NOTE =
  'Object lock is false. A legal hold is a Preloop control, not a storage guarantee. Preloop cannot verify a property of the storage layer beneath it.';

export const HOLD_DOES =
  'A hold tells the purge and the evidence janitor to leave one record alone, including its ciphertext past the payload window.';

export const HOLD_DOES_NOT =
  'It is not WORM and it is not object lock. An operator with database access can still delete the row, and a backup restore can still bring a purged row back.';

export const DEFAULT_VERIFY_ROWS = 10_000;
export const MIN_HOLD_REASON = 8;
export const MAX_HOLD_REASON = 2000;
export const MAX_EXPORT_DAYS = 366;

export interface VerifyRange {
  start: number;
  end: number;
}

export function defaultVerifyRange(status: {
  head_seq: number;
  pruned_below_seq: number;
}): VerifyRange {
  const end = Math.max(0, Math.trunc(status.head_seq));
  const floor = Math.max(0, Math.trunc(status.pruned_below_seq));
  if (end <= floor) {
    return { start: end, end };
  }
  const start = Math.max(floor + 1, end - (DEFAULT_VERIFY_ROWS - 1));
  return { start, end };
}

export function wholeChainRange(status: {
  head_seq: number;
  pruned_below_seq: number;
}): VerifyRange {
  const end = Math.max(0, Math.trunc(status.head_seq));
  const floor = Math.max(0, Math.trunc(status.pruned_below_seq));
  if (end <= floor) {
    return { start: end, end };
  }
  return { start: Math.max(1, floor + 1), end };
}

export function clampRetentionDays(
  days: number,
  floorDays: number,
  maxDays: number
): number {
  if (!Number.isFinite(days)) {
    return floorDays;
  }
  return Math.min(maxDays, Math.max(floorDays, Math.trunc(days)));
}

export function retentionRowEditable(row: {
  days: number;
  source: string;
}): boolean {
  return row.days !== -1 && row.source !== 'subscription_history';
}

export interface RetentionDraftRow {
  record_class: string;
  label: string;
  days: number;
  source: string;
}

/** Classes the PUT must name so an untouched account override is not cleared. */
export function retentionUpdatePayload(
  rows: RetentionDraftRow[],
  drafts: Record<string, number>
): Record<string, number> {
  const payload: Record<string, number> = {};
  for (const row of rows) {
    if (!retentionRowEditable(row)) {
      continue;
    }
    const draft = drafts[row.record_class] ?? row.days;
    if (row.source === 'account' || draft !== row.days) {
      payload[row.record_class] = draft;
    }
  }
  return payload;
}

export function retentionDiff(
  rows: RetentionDraftRow[],
  drafts: Record<string, number>
): { record_class: string; label: string; from: number; to: number }[] {
  const changes = [];
  for (const row of rows) {
    if (!retentionRowEditable(row)) {
      continue;
    }
    const draft = drafts[row.record_class] ?? row.days;
    if (draft !== row.days) {
      changes.push({
        record_class: row.record_class,
        label: row.label,
        from: row.days,
        to: draft,
      });
    }
  }
  return changes;
}

export function truncateMiddle(value: string, keep = 18): string {
  if (value.length <= keep) {
    return value;
  }
  const head = Math.ceil(keep / 2);
  const tail = Math.floor(keep / 2);
  return `${value.slice(0, head)}...${value.slice(-tail)}`;
}

export function holdResourceHref(
  resourceType: string,
  resourceId: string
): string | null {
  switch (resourceType) {
    case 'execution':
      return `/console/flows/executions/${resourceId}`;
    case 'approval':
      return `/console/approval/${resourceId}`;
    case 'runtime_session':
      return `/console/runtime-sessions?sessionId=${encodeURIComponent(resourceId)}`;
    default:
      return null;
  }
}

export function formatBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) {
    return 'Unknown';
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KiB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

export function offlineAuditCommand(range: VerifyRange | null): string {
  if (!range || range.end <= 0 || range.start > range.end) {
    return 'preloop audit verify';
  }
  return `preloop audit verify --start-seq ${range.start} --end-seq ${range.end}`;
}

export function evidenceVerifyCommand(
  filename: string,
  keyId: string | null
): string {
  const file = filename || 'export.tar.gz';
  if (!keyId) {
    return `preloop evidence verify ${file}`;
  }
  return `preloop evidence verify ${file} --public-key ./${keyId}.pub`;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: string): boolean {
  return UUID_RE.test(value.trim());
}

export function isoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** Last 30 UTC days, end exclusive so today is included. */
export function periodDefaultRange(now = new Date()): {
  start: string;
  end: string;
} {
  const end = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1)
  );
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - 30);
  return { start: isoDate(start), end: isoDate(end) };
}

export function filenameFromDisposition(
  header: string | null,
  fallback: string
): string {
  if (!header) {
    return fallback;
  }
  const quoted = /filename="([^"]+)"/.exec(header);
  if (quoted) {
    return quoted[1];
  }
  const plain = /filename=([^;]+)/.exec(header);
  return plain ? plain[1].trim() : fallback;
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function downloadText(filename: string, text: string): void {
  downloadBlob(
    new Blob([text.endsWith('\n') ? text : `${text}\n`], {
      type: 'text/plain',
    }),
    filename
  );
}
