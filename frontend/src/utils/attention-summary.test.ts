/**
 * The counts the header bell repeats. They have to be the strip's counts,
 * including the low-severity rule, or the bell and the strip disagree again.
 */
import { expect } from '@open-wc/testing';

import type {
  AttentionItem,
  AttentionKind,
  AttentionSeverity,
} from './attention';
import {
  formatAttentionSummary,
  publishAttentionSummary,
  readAttentionSummary,
  summariseAttentionItems,
} from './attention-summary';

function item(
  kind: AttentionKind,
  severity: AttentionSeverity = 'warning'
): AttentionItem {
  return {
    id: `${kind}:${Math.random()}`,
    kind,
    severity,
    title: 'Something',
    detail: 'happened',
    href: '/console/attention',
    at: null,
    fingerprint: 'fingerprint',
    dismissable: true,
  };
}

describe('attention summary', () => {
  afterEach(() => {
    sessionStorage.removeItem('preloop:attention-summary');
  });

  it('counts per kind in the console order', () => {
    const summary = summariseAttentionItems([
      item('pricing'),
      item('flow'),
      item('flow'),
    ]);
    expect(summary.total).to.equal(3);
    expect(summary.counts).to.eql([
      { kind: 'flow', count: 2 },
      { kind: 'pricing', count: 1 },
    ]);
    expect(summary.lowOnly).to.equal(false);
  });

  it('drops low items while anything louder is open', () => {
    const summary = summariseAttentionItems([
      item('flow', 'critical'),
      item('model', 'low'),
    ]);
    expect(summary.total).to.equal(1);
    expect(summary.counts).to.eql([{ kind: 'flow', count: 1 }]);
  });

  it('words a list of low items as worth a look', () => {
    const summary = summariseAttentionItems([item('model', 'low')]);
    expect(summary.lowOnly).to.equal(true);
    expect(formatAttentionSummary(summary)).to.equal(
      '1 item worth a look: 1 model'
    );
  });

  it('reads back what it published, and says nothing at zero', () => {
    publishAttentionSummary([item('flow'), item('pricing')]);
    const stored = readAttentionSummary();
    expect(formatAttentionSummary(stored)).to.equal(
      '2 items need attention: 1 flow, 1 pricing'
    );

    publishAttentionSummary([]);
    expect(formatAttentionSummary(readAttentionSummary())).to.equal('');
    expect(formatAttentionSummary(null)).to.equal('');
  });
});
