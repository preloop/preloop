import { fetchWithAuth } from '../api';
import type { BrowserStepMetadata, RuntimeSessionActivityItem } from '../types';

/**
 * Result of loading one session artifact's bytes.
 *
 * `gone` is the byte route's 410: the step keeps its metadata and the console
 * shows why the image is missing (`evicted` by a storage bound, or `expired`
 * by retention).
 */
export type SessionArtifactLoad =
  | { status: 'ok'; url: string; contentType: string }
  | { status: 'gone'; availability: string }
  | { status: 'error'; message: string };

type CacheEntry = {
  refs: number;
  promise: Promise<SessionArtifactLoad>;
};

const cache = new Map<string, CacheEntry>();

function cacheKey(sessionId: string, artifactId: string): string {
  return `${sessionId}/${artifactId}`;
}

export function sessionArtifactPath(
  sessionId: string,
  artifactId: string
): string {
  return `/api/v1/runtime-sessions/${encodeURIComponent(
    sessionId
  )}/artifacts/${encodeURIComponent(artifactId)}`;
}

async function loadArtifact(
  sessionId: string,
  artifactId: string
): Promise<SessionArtifactLoad> {
  try {
    const response = await fetchWithAuth(
      sessionArtifactPath(sessionId, artifactId)
    );
    if (response.status === 410) {
      let availability = 'expired';
      try {
        const body = await response.json();
        if (body && typeof body.availability === 'string') {
          availability = body.availability;
        }
      } catch {
        // Keep the default reason; the status alone says the bytes are gone.
      }
      return { status: 'gone', availability };
    }
    if (!response.ok) {
      return {
        status: 'error',
        message: `Could not load the image (HTTP ${response.status}).`,
      };
    }
    const blob = await response.blob();
    return {
      status: 'ok',
      url: URL.createObjectURL(blob),
      contentType: blob.type,
    };
  } catch (error) {
    return {
      status: 'error',
      message:
        error instanceof Error ? error.message : 'Could not load the image.',
    };
  }
}

/**
 * Fetch an artifact with the user's token and hold an object URL for it.
 *
 * Calls are reference counted per artifact so a thumbnail, the header strip
 * and the full-size viewer share one download. Every acquire must be paired
 * with {@link releaseSessionArtifact}; the object URL is revoked when the
 * last holder releases it.
 */
export function acquireSessionArtifact(
  sessionId: string,
  artifactId: string
): Promise<SessionArtifactLoad> {
  const key = cacheKey(sessionId, artifactId);
  const existing = cache.get(key);
  if (existing) {
    existing.refs += 1;
    return existing.promise;
  }
  const entry: CacheEntry = {
    refs: 1,
    promise: loadArtifact(sessionId, artifactId),
  };
  cache.set(key, entry);
  return entry.promise;
}

export function releaseSessionArtifact(
  sessionId: string,
  artifactId: string
): void {
  const key = cacheKey(sessionId, artifactId);
  const entry = cache.get(key);
  if (!entry) return;
  entry.refs -= 1;
  if (entry.refs > 0) return;
  cache.delete(key);
  void entry.promise.then((result) => {
    if (result.status === 'ok') URL.revokeObjectURL(result.url);
  });
}

/** Number of artifacts currently held; for tests. */
export function heldSessionArtifactCount(): number {
  return cache.size;
}

export function isBrowserStep(item: RuntimeSessionActivityItem): boolean {
  return item.activity_type === 'browser_step';
}

export function browserStepMetadata(
  item: RuntimeSessionActivityItem
): BrowserStepMetadata {
  return (item.metadata ?? {}) as BrowserStepMetadata;
}

/** Stable DOM-safe key for one step, used for scrolling and viewer paging. */
export function browserStepKey(item: RuntimeSessionActivityItem): string {
  const meta = browserStepMetadata(item);
  const raw = `${meta.source ?? 'api'}-${
    meta.source_step_id ?? meta.step_index ?? item.timestamp
  }`;
  return `browser-step-${raw.replace(/[^A-Za-z0-9_-]/g, '_')}`;
}

/**
 * Display number of a step: the agent-reported `step_index` when present,
 * otherwise the step's position in the session. The row, the header strip
 * and the viewer all use this so the same step reads the same everywhere.
 */
export function browserStepNumber(
  item: RuntimeSessionActivityItem,
  position: number
): number {
  const index = browserStepMetadata(item).step_index;
  return typeof index === 'number' ? index : position;
}

/** Steps in time order, then by step index for steps sharing a timestamp. */
export function sortBrowserSteps(
  items: RuntimeSessionActivityItem[]
): RuntimeSessionActivityItem[] {
  return items.filter(isBrowserStep).sort((left, right) => {
    const delta =
      new Date(left.timestamp || 0).getTime() -
      new Date(right.timestamp || 0).getTime();
    if (delta) return delta;
    return (
      Number(browserStepMetadata(left).step_index ?? 0) -
      Number(browserStepMetadata(right).step_index ?? 0)
    );
  });
}

export const BROWSER_ACTION_ICONS: Record<string, string> = {
  navigate: 'globe2',
  click: 'cursor',
  type: 'keyboard',
  select: 'ui-checks',
  scroll: 'arrows-vertical',
  screenshot: 'camera',
  extract: 'file-earmark-text',
  wait: 'hourglass-split',
  done: 'check2-circle',
  other: 'window',
};

export function browserActionIcon(action: string | null | undefined): string {
  return BROWSER_ACTION_ICONS[String(action || 'other')] || 'window';
}

/** Human reason for a screenshot whose bytes are no longer stored. */
export function unavailableReason(availability: string): string {
  if (availability === 'evicted') {
    return 'Screenshot evicted: the session or account storage bound dropped older screenshots.';
  }
  if (availability === 'expired') {
    return 'Screenshot expired under the retention policy.';
  }
  return `Screenshot unavailable (${availability}).`;
}

/** Settings card that explains storage bounds and retention for artifacts. */
export const ARTIFACT_STORAGE_SETTINGS_HREF =
  '/console/settings/account#session-artifact-storage';
