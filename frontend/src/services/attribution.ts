/**
 * First-touch attribution capture.
 *
 * On the first page load of a browser session, records where the visitor
 * landed and came from (entry path, referrer, UTM parameters, gclid) into
 * sessionStorage. Sent with the analytics session hello so the backend can
 * attribute signups to their acquisition source. Never overwritten within a
 * session — the first touch wins.
 */

const STORAGE_KEY = 'preloopAttribution';

const UTM_KEYS = [
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'utm_term',
  'utm_content',
] as const;

export interface Attribution {
  entry_path: string;
  entry_referrer: string;
  landed_at: string;
  utm_source?: string;
  utm_medium?: string;
  utm_campaign?: string;
  utm_term?: string;
  utm_content?: string;
  gclid?: string;
}

/** Capture attribution once per browser session (no-op when present). */
export function captureAttribution(): void {
  try {
    if (sessionStorage.getItem(STORAGE_KEY)) return;

    const params = new URLSearchParams(location.search);
    const attribution: Attribution = {
      entry_path: location.pathname,
      entry_referrer: document.referrer || '',
      landed_at: new Date().toISOString(),
    };
    for (const key of UTM_KEYS) {
      const value = params.get(key);
      if (value) attribution[key] = value.slice(0, 256);
    }
    const gclid = params.get('gclid');
    if (gclid) attribution.gclid = gclid.slice(0, 256);

    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(attribution));
  } catch {
    // sessionStorage unavailable — attribution is best-effort.
  }
}

/** The captured first-touch attribution for this session, if any. */
export function getAttribution(): Attribution | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Attribution) : null;
  } catch {
    return null;
  }
}
