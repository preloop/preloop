// Codex rollout observer. The scan/tail loop follows
// runtime-plugins/claude-preloop/src/observer.ts. The JSONL shape does not:
// Codex writes `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` records with
// `type` + `payload` (`session_meta`, `event_msg`, `response_item`,
// `turn_context`), not Claude's flat `sessionId`/`type` lines.

import fs from "node:fs";
import path from "node:path";

export type SessionActivity = {
  session_id: string;
  transcript_path: string;
  cwd?: string;
  last_role?: string;
  last_event_at: string;
  /** `task_started` events seen in the tail window. Not a full-file count. */
  turn_count?: number;
};

export type ActivityListener = (activity: SessionActivity) => void;

const DEFAULT_POLL_MS = 5_000;
/** Ignore transcripts idle for longer than this on the initial scan. */
const INITIAL_IDLE_CUTOFF_MS = 15 * 60 * 1000;
/**
 * Read at most this many trailing bytes when summarizing a transcript.
 * The window is then aligned to a newline / valid UTF-8 start so a
 * multi-byte character at the cut cannot produce a replacement character
 * that would hide the first complete record in the window.
 */
const TAIL_BYTES = 64 * 1024;
/** sessions/YYYY/MM/DD/*.jsonl is depth 4. Cap so a symlink loop cannot walk. */
const MAX_DEPTH = 6;

const THREAD_ID_RE =
  /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;

export class TranscriptObserver {
  private sizes = new Map<string, number>();
  private timer?: ReturnType<typeof setInterval>;
  private primed = false;

  constructor(
    private readonly root: string,
    private readonly listener: ActivityListener,
    private readonly pollMs: number = DEFAULT_POLL_MS,
  ) {}

  start(): void {
    this.scanOnce();
    this.timer = setInterval(() => this.scanOnce(), this.pollMs);
    (this.timer as { unref?: () => void }).unref?.();
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  /** One scan pass; exposed for tests. */
  scanOnce(): void {
    const files = this.listTranscripts();
    const now = Date.now();
    for (const file of files) {
      let stat: fs.Stats;
      try {
        stat = fs.statSync(file);
      } catch {
        this.sizes.delete(file);
        continue;
      }
      const known = this.sizes.get(file);
      this.sizes.set(file, stat.size);
      if (known === stat.size) {
        continue;
      }
      // First pass only reports recently-active transcripts, so startup does
      // not flood the control channel with the full session history.
      if (
        !this.primed &&
        known === undefined &&
        now - stat.mtimeMs > INITIAL_IDLE_CUTOFF_MS
      ) {
        continue;
      }
      const activity = this.summarize(file, stat);
      if (activity) {
        this.listener(activity);
      }
    }
    this.primed = true;
  }

  private listTranscripts(): string[] {
    const results: string[] = [];
    const walk = (dir: string, depth: number): void => {
      if (depth > MAX_DEPTH) {
        return;
      }
      let entries: fs.Dirent[];
      try {
        entries = fs.readdirSync(dir, { withFileTypes: true });
      } catch {
        return;
      }
      for (const entry of entries) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full, depth + 1);
          continue;
        }
        if (entry.isFile() && entry.name.endsWith(".jsonl")) {
          results.push(full);
        }
      }
    };
    walk(this.root, 0);
    return results;
  }

  private summarize(
    file: string,
    stat: fs.Stats,
  ): SessionActivity | undefined {
    const records = readTailRecords(file, stat.size);
    const summary = summarizeRolloutRecords(records, path.basename(file));
    return {
      session_id: summary.session_id,
      transcript_path: file,
      cwd: summary.cwd,
      last_role: summary.last_role,
      last_event_at: new Date(stat.mtimeMs).toISOString(),
      turn_count: summary.turn_count,
    };
  }
}

export type RolloutSummary = {
  session_id: string;
  cwd?: string;
  last_role?: string;
  turn_count: number;
};

/**
 * Tolerant summary of Codex rollout records.
 *
 * A record is `{ type, payload, timestamp }`. `session_meta.payload` carries
 * `id` / `session_id` and `cwd`. Turns show up as `event_msg` payloads whose
 * `type` is `task_started`. Message roles live on `response_item.payload.role`.
 * Anything else is ignored. Missing fields fall back to the filename UUID
 * and to mtime-only presence (no role, turn_count 0).
 */
export function summarizeRolloutRecords(
  records: Record<string, unknown>[],
  filename: string,
): RolloutSummary {
  let sessionId: string | undefined;
  let cwd: string | undefined;
  let lastRole: string | undefined;
  let turnCount = 0;
  for (const record of records) {
    const type = typeof record.type === "string" ? record.type : "";
    const payload = asRecord(record.payload);
    if (type === "session_meta") {
      sessionId = stringField(payload, "id") ?? stringField(payload, "session_id") ?? sessionId;
      cwd = stringField(payload, "cwd") ?? cwd;
      continue;
    }
    if (type === "turn_context") {
      cwd = stringField(payload, "cwd") ?? cwd;
      continue;
    }
    if (type === "event_msg") {
      const eventType = stringField(payload, "type");
      if (eventType === "task_started") {
        turnCount += 1;
      }
      sessionId = stringField(payload, "thread_id") ?? sessionId;
      if (eventType) {
        lastRole = eventType;
      }
      continue;
    }
    if (type === "response_item") {
      const role = stringField(payload, "role");
      if (role) {
        lastRole = role;
      }
    }
  }
  return {
    session_id: sessionId ?? sessionIdFromFilename(filename),
    cwd,
    last_role: lastRole,
    turn_count: turnCount,
  };
}

function sessionIdFromFilename(filename: string): string {
  const match = THREAD_ID_RE.exec(filename);
  if (match) {
    return match[1];
  }
  return path.basename(filename, ".jsonl");
}

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

function stringField(
  record: Record<string, unknown>,
  key: string,
): string | undefined {
  const value = record[key];
  return typeof value === "string" && value.trim() !== "" ? value : undefined;
}

function readTailRecords(
  file: string,
  size: number,
): Record<string, unknown>[] {
  let fd: number;
  try {
    fd = fs.openSync(file, "r");
  } catch {
    return [];
  }
  try {
    const length = Math.min(size, TAIL_BYTES);
    const start = size - length;
    const prefix = start > 0 ? 1 : 0;
    const buffer = Buffer.alloc(length + prefix);
    fs.readSync(fd, buffer, 0, length + prefix, start - prefix);
    const slice = alignTailBuffer(buffer, prefix > 0);
    const records: Record<string, unknown>[] = [];
    for (const line of slice.toString("utf8").split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const parsed = JSON.parse(trimmed) as unknown;
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          records.push(parsed as Record<string, unknown>);
        }
      } catch {
        // Partial or corrupt line; keep walking.
      }
    }
    return records;
  } catch {
    return [];
  } finally {
    fs.closeSync(fd);
  }
}

/** Align a trailing read to a newline and a valid UTF-8 character start. */
export function alignTailBuffer(buffer: Buffer, hasPrefix: boolean): Buffer {
  let offset = hasPrefix ? 1 : 0;
  const startedMidLine = hasPrefix && buffer[0] !== 0x0a;
  while (offset < buffer.length && (buffer[offset] & 0xc0) === 0x80) {
    offset += 1;
  }
  if (startedMidLine) {
    const newline = buffer.indexOf(0x0a, offset);
    if (newline !== -1) {
      offset = newline + 1;
    }
  }
  return buffer.subarray(offset);
}
