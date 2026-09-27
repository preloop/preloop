import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import { invalidateApiCaches } from '../api';
import {
  ATTENTION_KIND_META,
  ATTENTION_KIND_ORDER,
  deriveAttentionItems,
} from './attention';
import {
  loadPolicyNotices,
  policyNoticeItems,
  type AttentionPolicyNotice,
} from './attention-policy';

const notice = (
  overrides: Partial<AttentionPolicyNotice> = {}
): AttentionPolicyNotice => ({
  rule_id: 'notify-codename',
  rule_description: 'Mentions of the codename',
  target: 'model.request',
  count: 3,
  last_hit_id: 'hit-3',
  last_hit_at: '2026-09-27T10:00:00Z',
  last_excerpt: 'the project-x plan',
  last_username: 'alex',
  ...overrides,
});

describe('policy notice attention items', () => {
  it('is a kind with its own section, after the existing ones', () => {
    expect(ATTENTION_KIND_ORDER).to.include('policy');
    expect(ATTENTION_KIND_ORDER.indexOf('policy')).to.be.greaterThan(
      ATTENTION_KIND_ORDER.indexOf('pricing')
    );
    expect(ATTENTION_KIND_META.policy.plural).to.equal('Policy notices');
  });

  it('shows one dismissable card per rule with count and latest excerpt', () => {
    const items = policyNoticeItems([
      notice(),
      notice({ rule_id: 'notify-other', count: 1, last_hit_id: 'hit-9' }),
    ]);

    expect(items.map((item) => item.id)).to.eql([
      'policy:notify-codename',
      'policy:notify-other',
    ]);
    const [first, second] = items;
    expect(first.kind).to.equal('policy');
    expect(first.dismissable).to.be.true;
    expect(first.detail).to.contain('3 times');
    expect(second.detail).to.contain('once');
    expect(first.evidence?.policyNotice?.lastExcerpt).to.equal(
      'the project-x plan'
    );
    expect(first.evidence?.policyNotice?.lastUsername).to.equal('alex');
  });

  it('drops rows with no hits', () => {
    expect(policyNoticeItems([notice({ count: 0 })])).to.eql([]);
  });

  it('comes back after dismissal when a new hit arrives', () => {
    const now = new Date('2026-09-27T12:00:00Z');
    const dismissal = {
      item_id: 'policy:notify-codename',
      fingerprint: 'notify-codename|hit-3',
      reason: 'expected',
      created_at: '2026-09-27T11:00:00Z',
    };

    const hidden = deriveAttentionItems({
      policyNotices: [notice()],
      dismissals: [dismissal],
      now,
    });
    expect(hidden.items).to.eql([]);
    expect(hidden.dismissed.map((entry) => entry.item.id)).to.eql([
      'policy:notify-codename',
    ]);

    const back = deriveAttentionItems({
      policyNotices: [notice({ count: 4, last_hit_id: 'hit-4' })],
      dismissals: [dismissal],
      now,
    });
    expect(back.items.map((item) => item.id)).to.eql([
      'policy:notify-codename',
    ]);
  });
});

describe('loadPolicyNotices', () => {
  let fetchStub: sinon.SinonStub;
  let status = 200;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    invalidateApiCaches();
    status = 200;
    fetchStub = sinon.stub(window, 'fetch').callsFake(async () => {
      if (status !== 200) {
        return new Response('{"detail":"forbidden"}', { status });
      }
      return new Response(JSON.stringify({ days: 7, rules: [notice()] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  it('reads the seven day summary', async () => {
    const rows = await loadPolicyNotices();
    expect(rows.map((row) => row.rule_id)).to.eql(['notify-codename']);
    expect(String(fetchStub.firstCall.args[0])).to.contain(
      '/api/v1/policies/notices/summary?days=7'
    );
  });

  it('yields nothing instead of failing when the summary is forbidden', async () => {
    status = 403;
    expect(await loadPolicyNotices()).to.eql([]);
  });
});
