import { expect, fixture, html } from '@open-wc/testing';
import { formatAbsoluteTime } from './date';
import { relativeTime } from './relative-time';

describe('relativeTime', () => {
  const now = new Date('2026-10-07T20:00:00Z');

  it('renders an age with the absolute time in title', async () => {
    const el = await fixture(
      html`<div>${relativeTime('2026-08-26T20:00:00Z', '', now)}</div>`
    );
    const span = el.querySelector('span');
    expect(span?.textContent?.trim()).to.equal('6w ago');
    expect(span?.getAttribute('title')).to.equal(
      formatAbsoluteTime('2026-08-26T20:00:00Z')
    );
  });

  it('renders future times as time remaining', async () => {
    const el = await fixture(
      html`<div>${relativeTime('2026-10-10T20:00:00Z', '', now)}</div>`
    );
    expect(el.querySelector('span')?.textContent?.trim()).to.equal('in 3d');
  });

  it('renders the fallback for a missing value', async () => {
    const el = await fixture(
      html`<div>${relativeTime(null, 'Never', now)}</div>`
    );
    expect(el.querySelector('span')).to.equal(null);
    expect(el.textContent?.trim()).to.equal('Never');
  });
});
