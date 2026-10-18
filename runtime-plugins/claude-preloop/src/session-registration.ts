// Spawn-time labels and lineage for SDK-owned workers (#1552).
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";

type RegistrationConfig = { control_ws_url?: string; bearer_token?: string };

export class SessionRegistrar {
  private pending = new Map<string, Promise<void>>();

  constructor(private config: RegistrationConfig, private source: string) {}

  register(sessionId: string | undefined, cwd?: string): Promise<void> {
    if (!sessionId || (!process.env.PRELOOP_SESSION_ALIAS && !process.env.PRELOOP_PARENT_SESSION_ID)) return Promise.resolve();
    const previous = this.pending.get(sessionId);
    if (previous) return previous;
    const task = this.post(sessionId, cwd).catch(error => {
      this.pending.delete(sessionId);
      process.stderr.write(`preloop session hook: could not register the session: ${String(error)}\n`);
    });
    this.pending.set(sessionId, task);
    return task;
  }

  /**
   * Record a turn end or session end for the live activity board (#1889).
   * Only the hook name and the session id are sent, never turn text. Fire and
   * forget: a failure is logged and never fails the turn.
   */
  recordEvent(sessionId: string | undefined, hookEventName: "Stop" | "SessionEnd"): Promise<void> {
    if (!sessionId || !this.config.control_ws_url || !this.config.bearer_token) return Promise.resolve();
    if (!/^[A-Za-z0-9_.:\-]{1,200}$/.test(sessionId) || sessionId.includes("..")) return Promise.resolve();
    const url = new URL(this.config.control_ws_url);
    url.protocol = url.protocol === "wss:" ? "https:" : "http:";
    url.pathname = "/api/v1/agents/session-event";
    url.search = "";
    return fetch(url, {
      method: "POST",
      headers: { Authorization: `Bearer ${this.config.bearer_token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, hook_event_name: hookEventName, source: this.source }),
      signal: AbortSignal.timeout(5000),
    }).then(response => {
      if (!response.ok) throw new Error(`API error (status ${response.status})`);
    }).catch(error => {
      process.stderr.write(`preloop session event: could not record ${hookEventName}: ${String(error)}\n`);
    });
  }

  private async post(sessionId: string, cwd?: string): Promise<void> {
    if (!this.config.control_ws_url || !this.config.bearer_token) return;
    if (!/^[A-Za-z0-9_.:\-]{1,200}$/.test(sessionId) || sessionId.includes("..")) return;
    const url = new URL(this.config.control_ws_url);
    url.protocol = url.protocol === "wss:" ? "https:" : "http:";
    url.pathname = "/api/v1/agents/session-start";
    url.search = "";
    const response = await fetch(url, {
      method: "POST",
      headers: { Authorization: `Bearer ${this.config.bearer_token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId, source: this.source, cwd,
        alias: process.env.PRELOOP_SESSION_ALIAS,
        parent_session_id: process.env.PRELOOP_PARENT_SESSION_ID,
      }),
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) throw new Error(`API error (status ${response.status})`);
    const result = await response.json() as { runtime_session_id?: string; alias?: string; parent_session_id?: string; started_at?: string };
    if (!result.runtime_session_id) throw new Error("response carried no runtime_session_id");
    const directory = path.join(os.homedir(), ".preloop", "sessions");
    await fs.mkdir(directory, { recursive: true, mode: 0o700 });
    const destination = path.join(directory, `${sessionId}.json`);
    let existing: {runtime_session_id?: string; alias?: string} = {};
    try { existing = JSON.parse(await fs.readFile(destination, "utf8")); } catch { /* first registration */ }
    const temporary = `${destination}.${randomUUID()}.tmp`;
    try {
      await fs.writeFile(temporary, JSON.stringify({ ...existing, ...result, external_session_id: sessionId, agent_kind: this.source, cwd }, null, 2) + "\n", { mode: 0o600 });
      await fs.rename(temporary, destination);
    } finally { await fs.rm(temporary, { force: true }); }
    if (existing.runtime_session_id !== result.runtime_session_id) process.stderr.write(`preloop runtime_session_id=${result.runtime_session_id}\n`);
    if (result.alias && existing.alias !== result.alias) process.stderr.write(`preloop session_alias=${result.alias}\n`);
  }
}
