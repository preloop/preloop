import { expect } from '@open-wc/testing';

import { getVisitorId } from './visitor-id';

const STORAGE_KEY = 'preloopVisitorId';
const COOKIE_NAME = 'pl_vid';

function clearCookie() {
  document.cookie = `${COOKIE_NAME}=; Max-Age=0; Path=/`;
}

function readCookieValue(): string | null {
  const match = document.cookie.match(
    new RegExp(`(?:^|;\\s*)${COOKIE_NAME}=([^;]*)`)
  );
  return match ? decodeURIComponent(match[1]) : null;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

describe('visitor-id', () => {
  beforeEach(() => {
    localStorage.removeItem(STORAGE_KEY);
    clearCookie();
  });

  afterEach(() => {
    localStorage.removeItem(STORAGE_KEY);
    clearCookie();
  });

  it('generates a UUID and persists to both stores', () => {
    const id = getVisitorId();
    expect(id).to.match(UUID_RE);
    expect(localStorage.getItem(STORAGE_KEY)).to.equal(id);
    expect(readCookieValue()).to.equal(id);
  });

  it('is stable across calls', () => {
    expect(getVisitorId()).to.equal(getVisitorId());
  });

  it('recovers from the cookie when localStorage was cleared', () => {
    const id = getVisitorId();
    localStorage.removeItem(STORAGE_KEY);
    expect(getVisitorId()).to.equal(id);
    expect(localStorage.getItem(STORAGE_KEY)).to.equal(id);
  });

  it('recovers from localStorage when the cookie was cleared', () => {
    const id = getVisitorId();
    clearCookie();
    expect(getVisitorId()).to.equal(id);
    expect(readCookieValue()).to.equal(id);
  });

  it('replaces malformed stored values', () => {
    localStorage.setItem(STORAGE_KEY, 'not-a-uuid');
    const id = getVisitorId();
    expect(id).to.match(UUID_RE);
    expect(localStorage.getItem(STORAGE_KEY)).to.equal(id);
  });
});
