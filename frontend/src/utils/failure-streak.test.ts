import { expect } from '@open-wc/testing';
import { failureStreak } from './failure-streak';

describe('failureStreak', () => {
  it('counts the failures at the head and dates the oldest one', () => {
    expect(
      failureStreak([
        {
          status: 'FAILED',
          start_time: '2026-11-15T10:00:00Z',
          failure_category: 'repository_access',
        },
        {
          status: 'FAILED',
          start_time: '2026-11-14T10:00:00Z',
          failure_category: 'repository_access',
        },
        { status: 'SUCCEEDED', start_time: '2026-11-13T10:00:00Z' },
        { status: 'FAILED', start_time: '2026-11-12T10:00:00Z' },
      ])
    ).to.deep.equal({
      count: 2,
      since: '2026-11-14T10:00:00Z',
      category: 'repository_access',
    });
  });

  it('names no category when the failures differ', () => {
    expect(
      failureStreak([
        { status: 'FAILED', failure_category: 'timeout' },
        { status: 'ERROR', failure_category: 'runner_error' },
      ])?.category
    ).to.equal(null);
  });

  it('is null when the newest run passed or only one failed', () => {
    expect(
      failureStreak([{ status: 'SUCCEEDED' }, { status: 'FAILED' }])
    ).to.equal(null);
    expect(
      failureStreak([{ status: 'FAILED' }, { status: 'SUCCEEDED' }])
    ).to.equal(null);
    expect(failureStreak([])).to.equal(null);
  });
});
