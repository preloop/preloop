import { expect } from '@open-wc/testing';
import {
  DEFAULT_FETCH_CONCURRENCY,
  mapWithConcurrency,
} from './concurrency.js';

describe('mapWithConcurrency', () => {
  it('keeps results in the order of the input', async () => {
    const results = await mapWithConcurrency([1, 2, 3, 4, 5], 2, async (n) => {
      await new Promise((resolve) => setTimeout(resolve, (5 - n) * 2));
      return n * 10;
    });

    expect(results).to.eql([10, 20, 30, 40, 50]);
  });

  it('never exceeds the cap', async () => {
    let inFlight = 0;
    let peak = 0;

    await mapWithConcurrency(
      Array.from({ length: 20 }, (_, i) => i),
      4,
      async () => {
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        await new Promise((resolve) => setTimeout(resolve, 1));
        inFlight -= 1;
      }
    );

    expect(peak).to.equal(4);
  });

  it('returns an empty list without calling the mapper', async () => {
    let calls = 0;
    const results = await mapWithConcurrency([], 4, async () => {
      calls += 1;
      return 1;
    });

    expect(results).to.eql([]);
    expect(calls).to.equal(0);
  });

  it('treats a cap below one as one', async () => {
    let inFlight = 0;
    let peak = 0;

    await mapWithConcurrency([1, 2, 3], 0, async () => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 1));
      inFlight -= 1;
    });

    expect(peak).to.equal(1);
  });

  it('rejects when a mapped call rejects', async () => {
    let error: unknown = null;
    try {
      await mapWithConcurrency([1, 2, 3], 2, async (n) => {
        if (n === 2) throw new Error('mapper failed');
        return n;
      });
    } catch (caught) {
      error = caught;
    }

    expect((error as Error)?.message).to.equal('mapper failed');
  });

  it('caps console fan-out at four', () => {
    expect(DEFAULT_FETCH_CONCURRENCY).to.equal(4);
  });
});
