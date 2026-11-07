import { expect } from '@open-wc/testing';
import {
  cachedCostRead,
  clearCostReadCache,
  COST_CLOSED_WINDOW_GRACE_MS,
  COST_CLOSED_WINDOW_TTL_MS,
  costReadFetchedAt,
  COST_OPEN_WINDOW_TTL_MS,
} from './api';

describe('cachedCostRead', () => {
  const now = Date.parse('2026-11-07T12:00:00Z');
  let reads = 0;
  const read = async () => ({ n: ++reads });

  beforeEach(() => {
    reads = 0;
    clearCostReadCache();
  });
  afterEach(() => clearCostReadCache());

  it('keeps a closed window as long as the server does', async () => {
    const end = '2026-11-01T00:00:00Z';
    await cachedCostRead('/closed', end, read, now);
    const later = now + COST_CLOSED_WINDOW_TTL_MS - 1;
    expect(await cachedCostRead('/closed', end, read, later)).to.deep.equal({
      n: 1,
    });
    expect(reads).to.equal(1);
    await cachedCostRead('/closed', end, read, now + COST_CLOSED_WINDOW_TTL_MS);
    expect(reads).to.equal(2);
  });

  it('clears when the tab becomes visible again', async () => {
    const end = '2026-11-01T00:00:00Z';
    await cachedCostRead('/visible', end, read, now);
    document.dispatchEvent(new Event('visibilitychange'));
    await cachedCostRead('/visible', end, read, now);
    expect(reads).to.equal(2);
  });

  it('remembers when the answer was fetched', async () => {
    const before = Date.now();
    await cachedCostRead('/fetched', '2026-11-01T00:00:00Z', read, now);
    expect(costReadFetchedAt('/fetched')).to.be.at.least(before);
  });

  it('expires the current period after a minute', async () => {
    const end = new Date(now).toISOString();
    await cachedCostRead('/open', end, read, now);
    await cachedCostRead('/open', end, read, now + COST_OPEN_WINDOW_TTL_MS - 1);
    expect(reads).to.equal(1);
    await cachedCostRead('/open', end, read, now + COST_OPEN_WINDOW_TTL_MS);
    expect(reads).to.equal(2);
  });

  it('treats a window inside the grace period as open', async () => {
    const end = new Date(now - COST_CLOSED_WINDOW_GRACE_MS + 1).toISOString();
    await cachedCostRead('/recent', end, read, now);
    await cachedCostRead('/recent', end, read, now + COST_OPEN_WINDOW_TTL_MS);
    expect(reads).to.equal(2);
  });

  it('never keeps a failure and hands out copies', async () => {
    const end = '2026-11-01T00:00:00Z';
    let fail = true;
    const flaky = async () => {
      if (fail) throw new Error('boom');
      return { n: ++reads };
    };
    let error: unknown;
    try {
      await cachedCostRead('/flaky', end, flaky, now);
    } catch (caught) {
      error = caught;
    }
    expect(error).to.be.instanceOf(Error);
    fail = false;
    const first = await cachedCostRead('/flaky', end, flaky, now);
    first.n = 99;
    expect(await cachedCostRead('/flaky', end, flaky, now)).to.deep.equal({
      n: 1,
    });
  });

  it('is cleared by clearCostReadCache', async () => {
    const end = '2026-11-01T00:00:00Z';
    await cachedCostRead('/cleared', end, read, now);
    clearCostReadCache();
    await cachedCostRead('/cleared', end, read, now);
    expect(reads).to.equal(2);
  });
});
