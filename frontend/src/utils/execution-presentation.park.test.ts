import { expect } from '@open-wc/testing';

import {
  executionStatusVariant,
  formatApprovalWindow,
  parkWaitingSummary,
  parkedRowTitle,
} from './execution-presentation';

describe('executionStatusVariant, waiting on a human', () => {
  it('is amber: nothing is computing, a person is deciding', () => {
    expect(executionStatusVariant('WAITING_FOR_HUMAN')).to.equal('warning');
  });

  it('keeps a resuming run blue with the other live statuses', () => {
    expect(executionStatusVariant('RESUMING')).to.equal('primary');
  });

  it('leaves the existing taxonomy alone', () => {
    expect(executionStatusVariant('SUCCEEDED')).to.equal('success');
    expect(executionStatusVariant('FAILED')).to.equal('danger');
    expect(executionStatusVariant('RUNNING')).to.equal('primary');
    expect(executionStatusVariant('CANCELLED')).to.equal('neutral');
  });
});

describe('formatApprovalWindow', () => {
  it('renders a compliance window in days, not seconds', () => {
    expect(formatApprovalWindow(259200)).to.equal('3 days');
  });

  it('renders the interactive default in minutes', () => {
    expect(formatApprovalWindow(300)).to.equal('5 minutes');
  });

  it('adds the leftover unit when there is one', () => {
    expect(formatApprovalWindow(90000)).to.equal('1 day 1 hour');
    expect(formatApprovalWindow(5400)).to.equal('1 hour 30 minutes');
  });

  it('says nothing rather than "0" when there is no window', () => {
    expect(formatApprovalWindow(null)).to.equal('—');
    expect(formatApprovalWindow(undefined)).to.equal('—');
  });
});

describe('parkWaitingSummary', () => {
  const now = new Date('2026-09-09T00:47:00Z');

  it('says who, since when, and how long is left', () => {
    const summary = parkWaitingSummary(
      {
        request_id: 'req-1',
        since: '2026-09-08T00:47:00Z',
        expires_at: '2026-09-11T00:47:00Z',
        waiting_for: 'Security approvers',
      },
      now
    );

    expect(summary).to.contain('Waiting for Security approvers');
    expect(summary).to.contain('since');
    expect(summary).to.contain('expires in 2 days');
  });

  it('says the window closed rather than a negative countdown', () => {
    const summary = parkWaitingSummary(
      {
        request_id: 'req-1',
        since: '2026-09-08T00:47:00Z',
        expires_at: '2026-09-08T00:52:00Z',
        waiting_for: 'Security approvers',
      },
      now
    );

    expect(summary).to.contain('window closed');
  });

  it('falls back to a human decision when the approver is unnamed', () => {
    expect(parkWaitingSummary({ request_id: 'req-1' }, now)).to.equal(
      'Waiting for a human decision.'
    );
  });

  it('is empty for a run that is not parked', () => {
    expect(parkWaitingSummary(null)).to.equal('');
  });
});

describe('parkedRowTitle', () => {
  it('states both timestamps a list row knows', () => {
    const title = parkedRowTitle({
      status: 'WAITING_FOR_HUMAN',
      parked_at: '2026-09-08T00:47:00Z',
      park_expires_at: '2026-09-11T00:47:00Z',
    });

    expect(title).to.contain('Parked');
    expect(title).to.contain('approval window closes');
  });

  it('says something even when the timestamps are missing', () => {
    expect(parkedRowTitle({ status: 'WAITING_FOR_HUMAN' })).to.equal(
      'Waiting for a human decision.'
    );
  });

  it('is empty for every other status', () => {
    expect(
      parkedRowTitle({ status: 'RUNNING', parked_at: '2026-09-08T00:47:00Z' })
    ).to.equal('');
  });
});
