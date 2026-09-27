import { expect } from '@open-wc/testing';

import type { SpendOutlierFinding } from '../spend-outliers-api';
import { ATTENTION_KIND_ORDER, deriveAttentionItems } from './attention';
import { importedSpendNote, spendOutlierItems } from './attention-spend';

const NOW = new Date('2026-09-02T12:00:00Z');

function finding(
  overrides: Partial<SpendOutlierFinding> = {}
): SpendOutlierFinding {
  return {
    id: 'finding-1',
    item_id: 'spend:daily_spend:user-1',
    fingerprint: 'daily_spend|user-1|2026-09-01',
    rule: 'daily_spend',
    rule_label: 'Daily spend spike',
    user_id: 'user-1',
    user_name: 'dev-one',
    runtime_session_id: null,
    session_title: null,
    day: '2026-09-01',
    detected_at: '2026-09-02T00:30:00Z',
    details: {
      spend_usd: 30,
      median_usd: 10,
      multiple: 3,
      threshold_multiple: 3,
      history_days: 8,
      gateway_usd: 30,
      imported_usd: 0,
      imported_sources: [],
    },
    summary: '',
    ...overrides,
  };
}

describe('spendOutlierItems', () => {
  it('names the developer, the rule, the numbers and the UTC day', () => {
    const [item] = spendOutlierItems([finding()]);

    expect(item.id).to.equal('spend:daily_spend:user-1');
    expect(item.kind).to.equal('spend');
    expect(item.severity).to.equal('warning');
    expect(item.title).to.equal('dev-one · Daily spend spike');
    expect(item.detail).to.equal(
      '$30.00 on 2026-09-01 (UTC), 3.0x the 28-day median of $10.00'
    );
    expect(item.href).to.equal('/console/cost');
    expect(item.fingerprint).to.equal('daily_spend|user-1|2026-09-01');
    expect(item.dismissable).to.be.true;
    expect(item.evidence?.spendOutlier?.medianUsd).to.equal(10);
  });

  it('describes a model mix finding with both days of share', () => {
    const [item] = spendOutlierItems([
      finding({
        rule: 'model_mix',
        rule_label: 'Top-tier model mix',
        item_id: 'spend:model_mix:user-1',
        details: {
          model: 'provider/top-model',
          share: 0.8,
          previous_share: 0.75,
          threshold_share: 0.5,
          spend_usd: 20,
        },
      }),
    ]);

    expect(item.detail).to.equal(
      'provider/top-model was 80% of spend on 2026-09-01 (UTC) and 75% the day before'
    );
    expect(item.evidence?.spendOutlier?.model).to.equal('provider/top-model');
  });

  it('links a session finding to the session', () => {
    const [item] = spendOutlierItems([
      finding({
        rule: 'session_cost',
        rule_label: 'Expensive session',
        item_id: 'spend:session_cost:session-9',
        runtime_session_id: 'session-9',
        session_title: 'Refactor',
        details: {
          spend_usd: 42.5,
          threshold_usd: 25,
          session_id: 'session-9',
        },
      }),
    ]);

    expect(item.href).to.equal('/console/runtime-sessions?sessionId=session-9');
    expect(item.detail).to.equal(
      'Session "Refactor" cost $42.50, over the $25.00 threshold (2026-09-01 (UTC))'
    );
  });

  it('labels imported spend as not metered by the gateway', () => {
    const imported = finding({
      details: {
        spend_usd: 45,
        median_usd: 10,
        multiple: 4.5,
        imported_usd: 15,
        imported_sources: ['copilot'],
      },
    });

    expect(importedSpendNote(imported)).to.equal(
      'includes $15.00 of imported copilot spend, not metered by the gateway'
    );
    expect(spendOutlierItems([imported])[0].detail).to.contain(
      'not metered by the gateway'
    );
    expect(importedSpendNote(finding())).to.equal('');
  });
});

describe('spend outliers in the attention inbox', () => {
  it('has its own section, after budgets', () => {
    expect(ATTENTION_KIND_ORDER.indexOf('spend')).to.equal(
      ATTENTION_KIND_ORDER.indexOf('budget') + 1
    );
  });

  it('hides a dismissed card while the day is the same', () => {
    const result = deriveAttentionItems({
      now: NOW,
      spendOutliers: [finding()],
      dismissals: [
        {
          item_id: 'spend:daily_spend:user-1',
          fingerprint: 'daily_spend|user-1|2026-09-01',
          reason: 'expected',
        },
      ],
    });

    expect(result.items).to.have.length(0);
    expect(result.dismissed.map((entry) => entry.item.id)).to.eql([
      'spend:daily_spend:user-1',
    ]);
  });

  it('brings the card back on a new day that still matches', () => {
    const result = deriveAttentionItems({
      now: NOW,
      spendOutliers: [
        finding({
          day: '2026-09-02',
          fingerprint: 'daily_spend|user-1|2026-09-02',
        }),
      ],
      dismissals: [
        {
          item_id: 'spend:daily_spend:user-1',
          fingerprint: 'daily_spend|user-1|2026-09-01',
          reason: 'expected',
        },
      ],
    });

    expect(result.items.map((item) => item.id)).to.eql([
      'spend:daily_spend:user-1',
    ]);
  });

  it('keeps a snoozed card hidden across new days until the snooze ends', () => {
    const inputs = {
      spendOutliers: [
        finding({
          day: '2026-09-02',
          fingerprint: 'daily_spend|user-1|2026-09-02',
        }),
      ],
      dismissals: [
        {
          item_id: 'spend:daily_spend:user-1',
          fingerprint: 'daily_spend|user-1|2026-09-01',
          reason: 'snoozed' as const,
          snooze_until: '2026-09-05T00:00:00Z',
        },
      ],
    };

    expect(deriveAttentionItems({ now: NOW, ...inputs }).items).to.have.length(
      0
    );
    expect(
      deriveAttentionItems({
        now: new Date('2026-09-06T00:00:00Z'),
        ...inputs,
      }).items
    ).to.have.length(1);
  });

  it('does not stretch a snooze across fingerprints for other kinds', () => {
    const result = deriveAttentionItems({
      now: NOW,
      executions: [
        {
          id: 'run-9',
          flow_id: 'flow-1',
          flow_name: 'Nightly Sync',
          status: 'FAILED',
          start_time: '2026-09-02T11:30:00Z',
          error_message: 'boom',
        },
      ],
      dismissals: [
        {
          item_id: 'flow:flow-1',
          fingerprint: 'run:run-8',
          reason: 'snoozed',
          snooze_until: '2026-09-05T00:00:00Z',
        },
      ],
    });

    expect(result.items.map((item) => item.id)).to.eql(['flow:flow-1']);
  });
});
