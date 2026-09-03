/**
 * Awareness between the console and its talk windows.
 *
 * A talk window is a separate browsing context, so the console cannot see it
 * with a DOM query. The two sides gossip over one `BroadcastChannel`: the
 * window says `open` when it mounts (and every 30s after, as a heartbeat),
 * `message` when an agent turn arrives while it is not focused, and `close`
 * when it goes away. The console keeps a registry of live windows and shows a
 * chip per window in its header.
 *
 * A window can vanish without saying `close` (crash, force quit), so the
 * registry expires anything it has not heard from for 90s: three missed
 * heartbeats, not one, so a busy machine does not drop a live window.
 *
 * `agentName` rides along with the message because the console may never have
 * loaded that agent (the chip has to be able to say who it is talking to
 * from the header of any page).
 */

export const TALK_CHANNEL_NAME = 'preloop-talk';

/** How often an open window re-announces itself. */
export const TALK_HEARTBEAT_MS = 30_000;

/** Silence after which a window is presumed gone. */
export const TALK_STALE_MS = 90_000;

export type TalkChannelMessageType = 'open' | 'message' | 'close';

export interface TalkChannelMessage {
  type: TalkChannelMessageType;
  agentId: string;
  agentName?: string;
  sessionId: string | null;
  at: number;
}

export interface TalkWindowEntry {
  agentId: string;
  agentName: string;
  sessionId: string | null;
  lastSeen: number;
  /** An agent turn arrived while the window was not focused. */
  unread: boolean;
}

/**
 * The console's view of which talk windows are open. Pure state so the add,
 * unread and prune rules can be tested without a real BroadcastChannel.
 */
export class TalkWindowRegistry {
  private windows = new Map<string, TalkWindowEntry>();

  get entries(): TalkWindowEntry[] {
    return Array.from(this.windows.values()).sort((left, right) =>
      left.agentName.localeCompare(right.agentName)
    );
  }

  /** Returns true when the registry changed and the header must re-render. */
  apply(message: TalkChannelMessage, now = Date.now()): boolean {
    if (!message?.agentId) return false;
    const at = typeof message.at === 'number' ? message.at : now;
    const existing = this.windows.get(message.agentId);

    if (message.type === 'close') {
      return this.windows.delete(message.agentId);
    }

    const entry: TalkWindowEntry = {
      agentId: message.agentId,
      agentName: message.agentName || existing?.agentName || 'Agent',
      sessionId: message.sessionId ?? existing?.sessionId ?? null,
      lastSeen: at,
      unread: message.type === 'message' ? true : (existing?.unread ?? false),
    };
    this.windows.set(message.agentId, entry);
    return (
      !existing ||
      existing.unread !== entry.unread ||
      existing.agentName !== entry.agentName ||
      existing.sessionId !== entry.sessionId
    );
  }

  /** Drop windows that have gone silent. Returns true when one was dropped. */
  prune(now = Date.now()): boolean {
    let changed = false;
    for (const [agentId, entry] of this.windows) {
      if (now - entry.lastSeen > TALK_STALE_MS) {
        this.windows.delete(agentId);
        changed = true;
      }
    }
    return changed;
  }

  /** Used when focusing a window fails: the window is not there any more. */
  drop(agentId: string): boolean {
    return this.windows.delete(agentId);
  }

  clearUnread(agentId: string): boolean {
    const entry = this.windows.get(agentId);
    if (!entry || !entry.unread) return false;
    this.windows.set(agentId, { ...entry, unread: false });
    return true;
  }
}

/** null in browsers (or test environments) without BroadcastChannel. */
export function openTalkChannel(): BroadcastChannel | null {
  if (typeof BroadcastChannel === 'undefined') return null;
  try {
    return new BroadcastChannel(TALK_CHANNEL_NAME);
  } catch {
    return null;
  }
}

export function postTalkChannelMessage(
  channel: BroadcastChannel | null,
  message: Omit<TalkChannelMessage, 'at'> & { at?: number }
): void {
  if (!channel) return;
  try {
    channel.postMessage({ at: Date.now(), ...message });
  } catch {
    // A closed channel (window tearing down) is not worth an error.
  }
}
