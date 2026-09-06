import { expect, fixture, html } from '@open-wc/testing';

import './session-list-panel.ts';
import type { SessionListPanel } from './session-list-panel.ts';
import type { ObservedSession } from '../utils/session-observer';

function makeSession(overrides: Partial<ObservedSession>): ObservedSession {
  return {
    id: 'session-1',
    sourceId: null,
    sourceType: 'claude_code',
    title: 'Session one',
    subtitle: null,
    sessionReference: null,
    runtimePrincipalName: null,
    flowName: null,
    flowExecutionId: null,
    status: 'idle',
    startedAt: null,
    lastActivityAt: null,
    endedAt: null,
    totalRequests: 4,
    successfulRequests: 4,
    failedRequests: 0,
    tokenUsage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    estimatedCost: 0,
    latestModelAlias: null,
    latestProviderName: null,
    canLoadEvents: false,
    optimizationWasteScore: null,
    optimizationPotentialSavingsTokens: null,
    optimizationPotentialSavingsUsd: null,
    raw: null,
    ...overrides,
  };
}

async function renderPanel(
  sessions: ObservedSession[]
): Promise<SessionListPanel> {
  const el = await fixture<SessionListPanel>(
    html`<session-list-panel .sessions=${sessions}></session-list-panel>`
  );
  await el.updateComplete;
  return el;
}

describe('session-list-panel status chips', () => {
  it('renders idle sessions as a neutral chip, not a solid accent badge', async () => {
    const el = await renderPanel([makeSession({ status: 'idle' })]);

    const badge = el.shadowRoot?.querySelector('.title-row sl-badge');
    expect(badge).to.exist;
    expect(badge?.textContent?.trim()).to.equal('Idle');
    expect(badge?.getAttribute('variant')).to.equal('neutral');
    expect(badge?.classList.contains('chip')).to.be.true;
  });

  it('keeps live and failing sessions on their own tones', async () => {
    const el = await renderPanel([
      makeSession({ id: 'a', status: 'active_now' }),
      makeSession({ id: 'b', status: 'idle', failedRequests: 2 }),
    ]);

    const badges = Array.from(
      el.shadowRoot?.querySelectorAll('.title-row sl-badge') ?? []
    );
    expect(badges).to.have.lengthOf(2);
    expect(badges[0].getAttribute('variant')).to.equal('success');
    expect(badges[1].getAttribute('variant')).to.equal('warning');
    badges.forEach((badge) => {
      expect(badge.classList.contains('chip')).to.be.true;
    });
  });

  it('renders the waste badge through the same chip recipe', async () => {
    const el = await renderPanel([makeSession({ optimizationWasteScore: 20 })]);

    const badge = el.shadowRoot?.querySelector('.waste-row sl-badge');
    expect(badge).to.exist;
    expect(badge?.getAttribute('variant')).to.equal('warning');
    expect(badge?.classList.contains('chip')).to.be.true;
  });
});
