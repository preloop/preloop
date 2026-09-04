import { expect, fixture, html } from '@open-wc/testing';

import './relative-time-label';
import { formatRelativeTime } from './relative-time-label';

describe('relative-time-label', () => {
  it('reads as part of the sentence around it', async () => {
    const el = await fixture(
      html`<span
        >Updated
        <relative-time-label
          .timestamp=${new Date(Date.now() - 4 * 60000).toISOString()}
        ></relative-time-label
      ></span>`
    );
    // Light DOM on purpose: the host reads one string, not two shadow roots.
    expect(el.textContent?.replace(/\s+/g, ' ').trim()).to.equal(
      'Updated 4m ago'
    );
  });

  it('says what the host tells it to say before there is a time', async () => {
    const el = await fixture(
      html`<relative-time-label .fallback=${'Loading…'}></relative-time-label>`
    );
    expect(el.textContent).to.equal('Loading…');
  });

  it('ages on its own clock', async () => {
    const el = (await fixture(
      html`<relative-time-label
        .timestamp=${new Date().toISOString()}
      ></relative-time-label>`
    )) as HTMLElement & { updateComplete: Promise<unknown> };
    expect(el.textContent).to.equal('just now');

    // The page has been open a while and nothing has re-rendered it.
    (el as unknown as { tick: number }).tick += 1;
    (el as unknown as { timestamp: string }).timestamp = new Date(
      Date.now() - 3 * 3600 * 1000
    ).toISOString();
    await el.updateComplete;
    expect(el.textContent).to.equal('3h ago');
  });

  it('counts in minutes, then hours, then days', () => {
    const minutes = (n: number) =>
      new Date(Date.now() - n * 60000).toISOString();
    expect(formatRelativeTime(minutes(0.2))).to.equal('just now');
    expect(formatRelativeTime(minutes(45))).to.equal('45m ago');
    expect(formatRelativeTime(minutes(60 * 5))).to.equal('5h ago');
    expect(formatRelativeTime(minutes(60 * 24 * 3))).to.equal('3d ago');
    expect(formatRelativeTime(null)).to.equal('Never');
  });
});
