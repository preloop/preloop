/**
 * Persistent visitor identity for first-party analytics.
 *
 * A random UUID (no user data) stored in localStorage and mirrored to a
 * first-party cookie so plain HTTP endpoints (e.g. installer downloads) can
 * correlate with web sessions. Either store recovers the other when cleared.
 */

const STORAGE_KEY = 'preloopVisitorId';
const COOKIE_NAME = 'pl_vid';
const COOKIE_MAX_AGE_SECONDS = 400 * 24 * 60 * 60; // 400 days (browser cap)

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function readCookie(): string | null {
  const match = document.cookie.match(
    new RegExp(`(?:^|;\\s*)${COOKIE_NAME}=([^;]*)`)
  );
  return match ? decodeURIComponent(match[1]) : null;
}

function writeCookie(id: string): void {
  const secure = location.protocol === 'https:' ? '; Secure' : '';
  document.cookie =
    `${COOKIE_NAME}=${encodeURIComponent(id)}; Max-Age=${COOKIE_MAX_AGE_SECONDS}` +
    `; Path=/; SameSite=Lax${secure}`;
}

function readStorage(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStorage(id: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // Private mode etc. — the cookie still carries the id.
  }
}

function generateUuid(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }
  // Fallback for older browsers.
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0'));
  return (
    hex.slice(0, 4).join('') +
    '-' +
    hex.slice(4, 6).join('') +
    '-' +
    hex.slice(6, 8).join('') +
    '-' +
    hex.slice(8, 10).join('') +
    '-' +
    hex.slice(10, 16).join('')
  );
}

/**
 * Return this browser's persistent visitor id, creating it when absent and
 * re-asserting both stores (localStorage + cookie) on every call.
 */
export function getVisitorId(): string {
  const fromStorage = readStorage();
  const fromCookie = readCookie();

  let id =
    (fromStorage && UUID_RE.test(fromStorage) && fromStorage) ||
    (fromCookie && UUID_RE.test(fromCookie) && fromCookie) ||
    null;

  if (!id) {
    id = generateUuid();
  }
  if (fromStorage !== id) writeStorage(id);
  if (fromCookie !== id) writeCookie(id);
  return id;
}
