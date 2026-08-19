import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

/**
 * Local/remote ownership for a Claude Code session.
 *
 * Local: `preloop claude` owns a PTY-spawned `claude` TUI.
 * Remote: the sidecar owns the same session through the Agent SDK.
 * Offline: no launcher and no live SDK query.
 *
 * Happy's switch: a phone/web/watch message or request_takeover asks the
 * launcher to exit local mode; the sidecar then resumes via the SDK.
 * Release (any key at the laptop, or an explicit remote Release) stops the
 * SDK query and tells the launcher to `claude --resume`.
 */
export type OwnershipMode = "local" | "remote" | "offline";

export type IpcMessage = {
  type:
    | "hello"
    | "switch"
    | "switched"
    | "release"
    | "released"
    | "local_ready"
    | "status"
    | "session";
  mode?: OwnershipMode;
  session_id?: string;
  cwd?: string;
};

export function defaultSocketPath(): string {
  return path.join(os.homedir(), ".preloop", "claude-control.sock");
}

export class LauncherBridge {
  private server?: net.Server;
  private client?: net.Socket;
  private buffer = "";
  mode: OwnershipMode = "offline";
  lastSessionId?: string;
  lastCwd?: string;
  private onSwitched?: () => void;
  private onReleased?: () => void;
  private logger: (message: string) => void = () => {};

  constructor(private readonly socketPath: string = defaultSocketPath()) {}

  setLogger(logger: (message: string) => void): void {
    this.logger = logger;
  }

  onLauncherSwitched(handler: () => void): void {
    this.onSwitched = handler;
  }

  onLauncherReleased(handler: () => void): void {
    this.onReleased = handler;
  }

  hasLauncher(): boolean {
    return this.client !== undefined && !this.client.destroyed;
  }

  async listen(): Promise<void> {
    await fs.promises.mkdir(path.dirname(this.socketPath), { recursive: true });
    try {
      fs.unlinkSync(this.socketPath);
    } catch {
      // no previous socket
    }
    this.server = net.createServer((socket) => {
      if (this.client && !this.client.destroyed) {
        this.client.destroy();
      }
      this.client = socket;
      this.buffer = "";
      socket.setEncoding("utf8");
      socket.on("data", (chunk: string) => this.ingest(chunk));
      socket.on("close", () => {
        if (this.client === socket) {
          this.client = undefined;
          if (this.mode === "local") {
            this.mode = "offline";
          }
        }
      });
    });
    await new Promise<void>((resolve, reject) => {
      this.server!.listen(this.socketPath, () => resolve());
      this.server!.on("error", reject);
    });
  }

  stop(): void {
    this.client?.destroy();
    this.client = undefined;
    this.server?.close();
    this.server = undefined;
    try {
      fs.unlinkSync(this.socketPath);
    } catch {
      // already gone
    }
  }

  requestSwitch(): boolean {
    if (!this.hasLauncher()) {
      return false;
    }
    this.send({ type: "switch" });
    return true;
  }

  requestRelease(): boolean {
    if (!this.hasLauncher()) {
      return false;
    }
    this.send({ type: "release", session_id: this.lastSessionId });
    return true;
  }

  notifyStatus(): void {
    this.send({
      type: "status",
      mode: this.mode,
      session_id: this.lastSessionId,
    });
  }

  private ingest(chunk: string): void {
    this.buffer += chunk;
    let newline = this.buffer.indexOf("\n");
    while (newline >= 0) {
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (line) {
        this.handleLine(line);
      }
      newline = this.buffer.indexOf("\n");
    }
  }

  private handleLine(line: string): void {
    let message: IpcMessage;
    try {
      message = JSON.parse(line) as IpcMessage;
    } catch (error) {
      this.logger(`launcher ipc invalid json: ${String(error)}`);
      return;
    }
    if (message.type === "hello" || message.type === "local_ready") {
      this.mode = "local";
      if (typeof message.session_id === "string") {
        this.lastSessionId = message.session_id;
      }
      if (typeof message.cwd === "string") {
        this.lastCwd = message.cwd;
      }
      this.notifyStatus();
      return;
    }
    if (message.type === "switched") {
      this.mode = "remote";
      this.onSwitched?.();
      this.notifyStatus();
      return;
    }
    if (message.type === "release") {
      this.mode = "local";
      this.onReleased?.();
      this.send({ type: "released", session_id: this.lastSessionId });
      return;
    }
    if (message.type === "session" && typeof message.session_id === "string") {
      this.lastSessionId = message.session_id;
    }
  }

  private send(message: IpcMessage): void {
    if (!this.hasLauncher()) {
      return;
    }
    this.client!.write(`${JSON.stringify(message)}\n`);
  }
}
