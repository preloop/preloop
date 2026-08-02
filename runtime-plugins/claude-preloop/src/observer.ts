import fs from "node:fs";
import path from "node:path";

/**
 * Lightweight presence/telemetry for sessions the sidecar does NOT own:
 * Claude Code writes JSONL transcripts to
 * `~/.claude/projects/<project-slug>/<session-id>.jsonl`; polling file growth
 * and parsing the last record is enough for presence + a summary, without
 * uploading transcripts (summaries by default per the design memo).
 */
export type SessionActivity = {
  session_id: string;
  transcript_path: string;
  cwd?: string;
  last_role?: string;
  last_event_at: string;
};

export type ActivityListener = (activity: SessionActivity) => void;

const DEFAULT_POLL_MS = 5_000;
/** Ignore transcripts idle for longer than this on the initial scan. */
const INITIAL_IDLE_CUTOFF_MS = 15 * 60 * 1000;
/**
 * Read at most this many trailing bytes when summarizing a transcript.
 * The fixed-offset read can split a multi-byte UTF-8 character (or a JSON
 * line) at the window boundary; the backwards line walk below tolerates the
 * resulting parse failure and settles on the newest complete record, at
 * worst skipping a partial record at the very start of the window.
 */
const TAIL_BYTES = 64 * 1024;

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
    let projects: string[];
    try {
      projects = fs.readdirSync(this.root);
    } catch {
      return results;
    }
    for (const project of projects) {
      const projectDir = path.join(this.root, project);
      let entries: string[];
      try {
        entries = fs.readdirSync(projectDir);
      } catch {
        continue;
      }
      for (const entry of entries) {
        if (entry.endsWith(".jsonl")) {
          results.push(path.join(projectDir, entry));
        }
      }
    }
    return results;
  }

  private summarize(
    file: string,
    stat: fs.Stats,
  ): SessionActivity | undefined {
    const lastRecord = readLastJsonRecord(file, stat.size);
    const sessionId =
      (typeof lastRecord?.sessionId === "string" && lastRecord.sessionId) ||
      path.basename(file, ".jsonl");
    return {
      session_id: sessionId,
      transcript_path: file,
      cwd: typeof lastRecord?.cwd === "string" ? lastRecord.cwd : undefined,
      last_role: typeof lastRecord?.type === "string" ? lastRecord.type : undefined,
      last_event_at: new Date(stat.mtimeMs).toISOString(),
    };
  }
}

function readLastJsonRecord(
  file: string,
  size: number,
): Record<string, unknown> | undefined {
  let fd: number;
  try {
    fd = fs.openSync(file, "r");
  } catch {
    return undefined;
  }
  try {
    const length = Math.min(size, TAIL_BYTES);
    const buffer = Buffer.alloc(length);
    fs.readSync(fd, buffer, 0, length, size - length);
    const lines = buffer.toString("utf8").split("\n");
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const line = lines[i].trim();
      if (!line) continue;
      try {
        return JSON.parse(line) as Record<string, unknown>;
      } catch {
        // Partial or corrupt trailing line; keep walking backwards.
      }
    }
    return undefined;
  } catch {
    return undefined;
  } finally {
    fs.closeSync(fd);
  }
}
