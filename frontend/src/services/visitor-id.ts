/**
 * Persistent visitor identity for first-party analytics.
 *
 * A random UUID (no user data) stored in localStorage and mirrored to a
 * first-party cookie so plain HTTP endpoints (e.g. installer downloads) can
 * correlate with web sessions. Either store recovers the other when cleared.
 */

import { randomUuid } from '../utils/random-uuid';

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
  return randomUuid();
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
