import { expect } from '@open-wc/testing';
import {
  clampRetentionDays,
  defaultVerifyRange,
  retentionDiff,
  retentionRowEditable,
  retentionUpdatePayload,
} from './records-format';

describe('records format', () => {
  it('keeps the last 10000 sealed rows and never starts below the floor', () => {
    expect(
      defaultVerifyRange({ head_seq: 25000, pruned_below_seq: 100 })
    ).to.deep.equal({ start: 15001, end: 25000 });
    expect(
      defaultVerifyRange({ head_seq: 40, pruned_below_seq: 10 })
    ).to.deep.equal({ start: 11, end: 40 });
  });

  it('refuses a retention day below the floor or above the max', () => {
    expect(clampRetentionDays(10, 183, 7300)).to.equal(183);
    expect(clampRetentionDays(9000, 183, 7300)).to.equal(7300);
    expect(clampRetentionDays(400, 183, 7300)).to.equal(400);
    expect(clampRetentionDays(Number.NaN, 183, 7300)).to.equal(183);
  });

  it('leaves subscription history and unlimited rows read-only', () => {
    expect(
      retentionRowEditable({ days: -1, source: 'subscription_history' })
    ).to.equal(false);
    expect(retentionRowEditable({ days: 365, source: 'default' })).to.equal(
      true
    );
    const rows = [
      {
        record_class: 'audit',
        label: 'Audit',
        days: 365,
        source: 'account',
      },
      {
        record_class: 'usage',
        label: 'Usage',
        days: -1,
        source: 'subscription_history',
      },
    ];
    expect(
      retentionUpdatePayload(rows, { audit: 400, usage: 10 })
    ).to.deep.equal({ audit: 400 });
    expect(retentionDiff(rows, { audit: 400, usage: 10 })).to.deep.equal([
      { record_class: 'audit', label: 'Audit', from: 365, to: 400 },
    ]);
  });
});
