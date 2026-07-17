import { expect } from '@open-wc/testing';

import { captureAttribution, getAttribution } from './attribution';

const STORAGE_KEY = 'preloopAttribution';

describe('attribution', () => {
  beforeEach(() => {
    sessionStorage.removeItem(STORAGE_KEY);
  });

  afterEach(() => {
    sessionStorage.removeItem(STORAGE_KEY);
  });

  it('captures entry path and referrer on first call', () => {
    captureAttribution();
    const attribution = getAttribution();
    expect(attribution).to.not.be.null;
    expect(attribution!.entry_path).to.equal(location.pathname);
    expect(attribution!.landed_at).to.be.a('string');
  });

  it('does not overwrite an existing capture (first touch wins)', () => {
    sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        entry_path: '/pricing',
        entry_referrer: 'https://news.ycombinator.com/',
        landed_at: '2026-07-13T00:00:00.000Z',
        utm_source: 'hn',
      })
    );
    captureAttribution();
    const attribution = getAttribution();
    expect(attribution!.entry_path).to.equal('/pricing');
    expect(attribution!.utm_source).to.equal('hn');
  });

  it('returns null when nothing captured and storage empty', () => {
    expect(getAttribution()).to.be.null;
  });

  it('returns null for corrupt stored JSON', () => {
    sessionStorage.setItem(STORAGE_KEY, '{corrupt');
    expect(getAttribution()).to.be.null;
  });
});
