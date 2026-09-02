/**
 * One browser window per chat.
 *
 * A chat must own its viewport: bottom-anchored, its own scroll, an input
 * pinned under it. A tab inside a document-scrolled console page cannot do
 * that, and a modal takes the page hostage while you talk. So Talk opens the
 * talk route in its own small window, the way GCP opens SSH-in-browser, and
 * the OS handles moving, resizing and alt-tabbing it for free.
 *
 * Every Talk entry point goes through `openTalkWindow` so that a second click
 * anywhere focuses the window that is already open for that agent instead of
 * opening a second one.
 */
import { Router } from '@vaadin/router';

import { showToast } from '../components/confirm-dialog';
import type { ManagedAgentSummary, RuntimeSessionSummary } from '../types';

/** Default window size: a phone-shaped column, wide enough for a paragraph. */
export const TALK_WINDOW_DEFAULT_WIDTH = 520;
export const TALK_WINDOW_DEFAULT_HEIGHT = 720;

/** Below this the helper navigates instead: a phone has no second window. */
export const TALK_WINDOW_PHONE_MAX_WIDTH = 640;

const GEOMETRY_KEY_PREFIX = 'talk_window_';

export interface TalkWindowGeometry {
  width: number;
  height: number;
  left: number;
  top: number;
}

type AgentLike = Pick<ManagedAgentSummary, 'id'> &
  Partial<Pick<ManagedAgentSummary, 'display_name'>>;
type SessionLike = { id: string } | string | null | undefined;

function sessionIdOf(session: SessionLike): string | null {
  if (!session) return null;
  return typeof session === 'string' ? session : session.id || null;
}

export interface TalkWindowOptions {
  /**
   * Which entry point opened this conversation ('dashboard-active-agents',
   * 'agent-detail-view', ...). It rides the URL as `?source=` and the composer
   * reports it as `requested_from` on every turn, so the audit trail still
   * says where an operator was standing when they talked to an agent.
   */
  sourceContext?: string | null;
}

/** The in-console route: no window flag, so the shell keeps its chrome. */
export function talkRoutePath(
  agentId: string,
  session?: SessionLike | RuntimeSessionSummary,
  options: TalkWindowOptions = {}
): string {
  const sessionId = sessionIdOf(session as SessionLike);
  const parts: string[] = [];
  if (sessionId) parts.push(`session=${encodeURIComponent(sessionId)}`);
  if (options.sourceContext) {
    parts.push(`source=${encodeURIComponent(options.sourceContext)}`);
  }
  const query = parts.length ? `?${parts.join('&')}` : '';
  return `/console/agents/${encodeURIComponent(agentId)}/talk${query}`;
}

/** The same route with the window flag, which strips the console chrome. */
export function talkWindowUrl(
  agentId: string,
  session?: SessionLike | RuntimeSessionSummary,
  options: TalkWindowOptions = {}
): string {
  const path = talkRoutePath(agentId, session, options);
  return `${path}${path.includes('?') ? '&' : '?'}window=1`;
}

export function talkWindowName(agentId: string): string {
  return `preloop-talk-${agentId}`;
}

export function talkWindowGeometryKey(agentId: string): string {
  return `${GEOMETRY_KEY_PREFIX}${agentId}`;
}

/**
 * Where this agent's window was last left. Anything unparseable or
 * out-of-range falls back to the default box, offset from the opener so a
 * fresh window never lands exactly on top of the console.
 */
export function readTalkWindowGeometry(agentId: string): TalkWindowGeometry {
  const fallback: TalkWindowGeometry = {
    width: TALK_WINDOW_DEFAULT_WIDTH,
    height: TALK_WINDOW_DEFAULT_HEIGHT,
    left: (window.screenX || 0) + 60,
    top: (window.screenY || 0) + 60,
  };
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(talkWindowGeometryKey(agentId));
  } catch {
    return fallback;
  }
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw) as Partial<TalkWindowGeometry>;
    const numeric = (value: unknown, min: number): number | null =>
      typeof value === 'number' && Number.isFinite(value) && value >= min
        ? Math.round(value)
        : null;
    const width = numeric(parsed.width, 320);
    const height = numeric(parsed.height, 320);
    if (width === null || height === null) return fallback;
    return {
      width,
      height,
      left: numeric(parsed.left, -10000) ?? fallback.left,
      top: numeric(parsed.top, -10000) ?? fallback.top,
    };
  } catch {
    return fallback;
  }
}

/** Called by the talk window itself on resize and before it unloads. */
export function saveTalkWindowGeometry(
  agentId: string,
  geometry: TalkWindowGeometry
): void {
  try {
    localStorage.setItem(
      talkWindowGeometryKey(agentId),
      JSON.stringify({
        width: Math.round(geometry.width),
        height: Math.round(geometry.height),
        left: Math.round(geometry.left),
        top: Math.round(geometry.top),
      })
    );
  } catch {
    // Private mode: the window simply opens at the default size next time.
  }
}

export function talkWindowFeatures(geometry: TalkWindowGeometry): string {
  return [
    'popup=yes',
    'noopener=no',
    `width=${Math.round(geometry.width)}`,
    `height=${Math.round(geometry.height)}`,
    `left=${Math.round(geometry.left)}`,
    `top=${Math.round(geometry.top)}`,
  ].join(',');
}

export function isPhoneViewport(): boolean {
  return (
    window.innerWidth > 0 && window.innerWidth < TALK_WINDOW_PHONE_MAX_WIDTH
  );
}

/** Windows this tab opened, so a second click focuses instead of reopening. */
const openWindows = new Map<string, { win: Window; key: string }>();

/** Test seam: forget the windows this tab believes it opened. */
export function resetTalkWindowsForTests(): void {
  openWindows.clear();
}

export interface OpenTalkWindowResult {
  /** 'window' opened or focused a popup, 'navigated' is the phone form. */
  outcome: 'window' | 'focused' | 'navigated' | 'blocked';
  win: Window | null;
}

/**
 * Must be called synchronously from a click handler: a `window.open` after an
 * await has lost the user gesture and every browser blocks it.
 */
export function openTalkWindow(
  agent: AgentLike | string,
  session?: SessionLike | RuntimeSessionSummary,
  options: TalkWindowOptions = {}
): OpenTalkWindowResult {
  const agentId = typeof agent === 'string' ? agent : agent.id;
  const url = talkWindowUrl(agentId, session, options);
  // The entry point is attribution, not identity: a window opened from the
  // dashboard and clicked again from the header chip is the same conversation
  // and must be focused, not reloaded with a different `source`.
  const key = talkWindowUrl(agentId, session);

  if (isPhoneViewport()) {
    Router.go(talkRoutePath(agentId, session, options));
    return { outcome: 'navigated', win: null };
  }

  const existing = openWindows.get(agentId);
  if (existing && !existing.win.closed) {
    if (existing.key === key) {
      existing.win.focus();
      return { outcome: 'focused', win: existing.win };
    }
    openWindows.delete(agentId);
  }

  const geometry = readTalkWindowGeometry(agentId);
  const win = window.open(
    url,
    talkWindowName(agentId),
    talkWindowFeatures(geometry)
  );

  if (!win) {
    showToast('Your browser blocked the window', 'warning', {
      label: 'Open in a new tab',
      onClick: () =>
        window.open(talkRoutePath(agentId, session, options), '_blank'),
    });
    return { outcome: 'blocked', win: null };
  }

  openWindows.set(agentId, { win, key });
  win.focus();
  return { outcome: 'window', win };
}
