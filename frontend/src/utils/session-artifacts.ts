import { fetchWithAuth } from '../api';
import type {
  ArtifactRowMetadata,
  BrowserStepMetadata,
  RuntimeSessionActivityItem,
  RuntimeSessionArtifactDescriptor,
} from '../types';

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

/** One entry for the full-size viewer (see artifact-image-viewer). */
export interface BrowserStepViewerImage {
  key: string;
  artifactId: string | null;
  availability: string | null;
  title: string;
  caption: string | null;
}

/** Viewer entries for steps already in time order, one per step. */
export function browserStepViewerImages(
  steps: RuntimeSessionActivityItem[]
): BrowserStepViewerImage[] {
  return steps.map((item, position) => {
    const meta = browserStepMetadata(item);
    const action = String(meta.action || 'other');
    return {
      key: browserStepKey(item),
      artifactId: meta.screenshot?.artifact_id || null,
      availability: meta.screenshot?.availability || null,
      title: `Step #${browserStepNumber(item, position)}: ${action}${
        meta.url ? ` ${meta.url}` : ''
      }`,
      caption: meta.target ? `Target: ${meta.target}` : null,
    };
  });
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

// ---------------------------------------------------------------------------
// General session artifacts (`activity_type="artifact"`, deposit API #1080).
// ---------------------------------------------------------------------------

/**
 * Display groups for artifact kinds: one header icon each. Kinds outside the
 * named four (screencast, recording, generated_file, trace, future kinds)
 * fall into `other`.
 */
export type ArtifactKindGroup =
  'screenshot' | 'transcript' | 'document' | 'audio' | 'other';

export const ARTIFACT_KIND_GROUPS: ArtifactKindGroup[] = [
  'screenshot',
  'transcript',
  'document',
  'audio',
  'other',
];

export const ARTIFACT_KIND_ICONS: Record<ArtifactKindGroup, string> = {
  screenshot: 'image',
  transcript: 'chat-square-text',
  document: 'file-earmark-text',
  audio: 'music-note-beamed',
  other: 'paperclip',
};

export const ARTIFACT_KIND_LABELS: Record<ArtifactKindGroup, string> = {
  screenshot: 'Screenshots',
  transcript: 'Transcripts',
  document: 'Documents',
  audio: 'Audio',
  other: 'Other files',
};

export function artifactKindGroup(
  kind: string | null | undefined,
  contentType?: string | null
): ArtifactKindGroup {
  const value = String(kind || '');
  if (value === 'screenshot') return 'screenshot';
  if (value === 'transcript') return 'transcript';
  if (value === 'document') return 'document';
  if (value === 'audio') return 'audio';
  // An image deposited under another kind still previews as an image.
  if (!value && String(contentType || '').startsWith('image/')) {
    return 'screenshot';
  }
  return 'other';
}

/** Docs page for artifacts (preloop/preloop#1090). */
export const ARTIFACTS_DOCS_HREF = 'https://docs.preloop.ai/guide/artifacts';
/** Tools page, where the deposit_artifact builtin tool is enabled. */
export const TOOLS_PAGE_HREF = '/console/tools';

/** Bytes read for the inline excerpt of a text artifact. */
export const ARTIFACT_EXCERPT_BYTES = 4096;
/** Lines shown before "Show more". */
export const ARTIFACT_EXCERPT_LINES = 3;
/** Lines shown after "Show more"; open or download for the rest. */
export const ARTIFACT_EXPANDED_LINES = 200;

export function isArtifactRow(item: RuntimeSessionActivityItem): boolean {
  return item.activity_type === 'artifact';
}

export function artifactRowMetadata(
  item: RuntimeSessionActivityItem
): ArtifactRowMetadata | null {
  const raw = (item.metadata ?? {}) as Record<string, unknown>;
  const artifact = raw.artifact as ArtifactRowMetadata | undefined;
  return artifact && typeof artifact.id === 'string' ? artifact : null;
}

/** DOM-safe key of an artifact row, used for scrolling and `?artifact=`. */
export function artifactRowKey(artifactId: string): string {
  return `artifact-${artifactId.replace(/[^A-Za-z0-9_-]/g, '_')}`;
}

/**
 * What a row shows for one artifact: the timeline metadata, completed by the
 * list descriptor when it is loaded (sha256, lineage, availability).
 */
export interface ArtifactView {
  id: string;
  kind: string;
  group: ArtifactKindGroup;
  name: string;
  contentType: string;
  sizeBytes: number | null;
  labels: Record<string, unknown>;
  producer: string | null;
  toolName: string | null;
  sha256: string | null;
  parentArtifactId: string | null;
  availability: string;
}

export function artifactView(
  item: RuntimeSessionActivityItem,
  descriptor?: RuntimeSessionArtifactDescriptor | null
): ArtifactView | null {
  const meta = artifactRowMetadata(item);
  if (!meta) return null;
  const kind = descriptor?.kind || meta.kind || '';
  const contentType =
    descriptor?.content_type || meta.content_type || 'application/octet-stream';
  return {
    id: meta.id,
    kind,
    group: artifactKindGroup(kind, contentType),
    name: descriptor?.name || meta.name || kind || 'artifact',
    contentType,
    sizeBytes: descriptor?.size_bytes ?? meta.size_bytes ?? null,
    labels: descriptor?.labels || meta.labels || {},
    producer: descriptor?.producer || meta.producer || null,
    toolName: descriptor?.tool_name || item.tool_name || null,
    sha256: descriptor?.sha256 || null,
    parentArtifactId: descriptor?.parent_artifact_id || null,
    availability: descriptor?.availability || 'available',
  };
}

/** Label entries with `site` and `consent_basis` first, then by key. */
export function orderedArtifactLabels(
  labels: Record<string, unknown>
): Array<[string, string]> {
  const first = ['site', 'consent_basis'];
  return Object.entries(labels || {})
    .flatMap(([key, value]): Array<[string, string]> =>
      Array.isArray(value)
        ? value.map((entry) => [key, String(entry)] as [string, string])
        : [[key, String(value)]]
    )
    .sort(([left], [right]) => {
      const l = first.indexOf(left);
      const r = first.indexOf(right);
      if (l !== r) return (l < 0 ? 99 : l) - (r < 0 ? 99 : r);
      return left.localeCompare(right);
    });
}

export function formatArtifactBytes(size: number | null): string {
  if (size === null || !Number.isFinite(size)) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/** Human reason for any artifact whose bytes are no longer stored. */
export function artifactUnavailableReason(availability: string): string {
  if (availability === 'evicted') {
    return 'Evicted: the session or account storage bound dropped this file.';
  }
  if (availability === 'expired') {
    return 'Expired under the retention policy.';
  }
  return `Unavailable (${availability}).`;
}

export type ArtifactTextLoad =
  | { status: 'ok'; text: string; truncated: boolean }
  | { status: 'gone'; availability: string }
  | { status: 'error'; message: string };

async function goneAvailability(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (body && typeof body.availability === 'string') return body.availability;
  } catch {
    // The status alone says the bytes are gone.
  }
  return 'expired';
}

/**
 * Read at most `maxBytes` of a text artifact with the user's token. The
 * stream is cancelled once the limit is reached, so a large transcript costs
 * one small read until the user asks for more.
 */
export async function readSessionArtifactText(
  sessionId: string,
  artifactId: string,
  maxBytes: number
): Promise<ArtifactTextLoad> {
  try {
    const response = await fetchWithAuth(
      sessionArtifactPath(sessionId, artifactId),
      { cache: 'no-store' }
    );
    if (response.status === 410) {
      return { status: 'gone', availability: await goneAvailability(response) };
    }
    if (!response.ok) {
      return {
        status: 'error',
        message: `Could not load the text (HTTP ${response.status}).`,
      };
    }
    const chunks: Uint8Array[] = [];
    let total = 0;
    let truncated = false;
    const reader = response.body?.getReader();
    if (reader) {
      while (total < maxBytes) {
        const { done, value } = await reader.read();
        if (done || !value) break;
        chunks.push(value);
        total += value.length;
      }
      if (total >= maxBytes) {
        truncated = true;
        void reader.cancel().catch(() => undefined);
      }
    } else {
      const buffer = new Uint8Array(await response.arrayBuffer());
      chunks.push(buffer);
      total = buffer.length;
      truncated = total > maxBytes;
    }
    const joined = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      joined.set(chunk, offset);
      offset += chunk.length;
    }
    const slice = joined.subarray(0, Math.min(total, maxBytes));
    // `stream: true` drops a multibyte character cut at the limit.
    const text = new TextDecoder('utf-8', { fatal: false }).decode(slice, {
      stream: truncated,
    });
    return { status: 'ok', text, truncated: truncated || total > maxBytes };
  } catch (error) {
    return {
      status: 'error',
      message:
        error instanceof Error ? error.message : 'Could not load the text.',
    };
  }
}

/** Download an artifact with the user's token under its own name. */
export async function downloadSessionArtifact(
  sessionId: string,
  artifactId: string,
  filename: string
): Promise<SessionArtifactLoad> {
  const load = await acquireSessionArtifact(sessionId, artifactId);
  try {
    if (load.status === 'ok') {
      const link = document.createElement('a');
      link.href = load.url;
      link.download = filename;
      link.rel = 'noopener';
      document.body.appendChild(link);
      link.click();
      link.remove();
    }
    return load;
  } finally {
    // Let the click start the download before the URL can be revoked.
    setTimeout(() => releaseSessionArtifact(sessionId, artifactId), 30_000);
  }
}
