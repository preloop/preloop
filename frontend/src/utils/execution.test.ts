import { expect } from '@open-wc/testing';

import { RUNNING_STATUSES, executionDurationText } from './execution';

describe('RUNNING_STATUSES', () => {
  it('contains exactly the non-terminal statuses', () => {
    expect([...RUNNING_STATUSES].sort()).to.deep.equal([
      'INITIALIZING',
      'PENDING',
      'RUNNING',
      'STARTING',
    ]);
  });
});

describe('executionDurationText', () => {
  it('shows the finished span when end_time is present, regardless of status', () => {
    expect(
      executionDurationText({
        status: 'SUCCEEDED',
        start_time: '2026-08-09 12:00:00',
        end_time: '2026-08-09 12:04:32',
      })
    ).to.equal('4m 32s');
  });

  it('shows a live Running prefix for a running execution without end_time', () => {
    expect(
      executionDurationText(
        {
          status: 'RUNNING',
          start_time: '2026-08-09 12:00:00',
          end_time: null,
        },
        new Date('2026-08-09T12:00:25Z')
      )
    ).to.equal('Running · 25s');
  });

  it('measures against the provided now for all running-like statuses', () => {
    const now = new Date('2026-08-09T12:01:05Z');
    for (const status of ['PENDING', 'STARTING', 'INITIALIZING', 'RUNNING']) {
      expect(
        executionDurationText(
          { status, start_time: '2026-08-09 12:00:00' },
          now
        )
      ).to.equal('Running · 1m 5s');
    }
  });

  it('returns an empty string for a legacy terminal row without end_time', () => {
    expect(
      executionDurationText({
        status: 'FAILED',
        start_time: '2026-08-09 12:00:00',
      })
    ).to.equal('');
  });

  it('returns an empty string when the timestamps are unusable', () => {
    expect(
      executionDurationText({
        status: 'SUCCEEDED',
        start_time: 'not-a-date',
        end_time: '2026-08-09 12:04:32',
      })
    ).to.equal('');
    expect(
      executionDurationText(
        { status: 'RUNNING', start_time: '2026-08-09 12:00:10' },
        new Date('2026-08-09T12:00:00Z')
      )
    ).to.equal('');
  });
});
