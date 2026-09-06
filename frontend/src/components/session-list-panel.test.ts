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

  it('leaves an ended session neutral even when a request failed', async () => {
    const el = await renderPanel([
      makeSession({ status: 'ended', failedRequests: 3 }),
    ]);

    const badge = el.shadowRoot?.querySelector('.title-row sl-badge');
    expect(badge?.textContent?.trim()).to.equal('Ended');
    // Warning means "needs a person"; a finished run does not.
    expect(badge?.getAttribute('variant')).to.equal('neutral');
  });

  it('renders the waste badge through the same chip recipe', async () => {
    const el = await renderPanel([makeSession({ optimizationWasteScore: 20 })]);

    const badge = el.shadowRoot?.querySelector('.waste-row sl-badge');
    expect(badge).to.exist;
    expect(badge?.getAttribute('variant')).to.equal('warning');
    expect(badge?.classList.contains('chip')).to.be.true;
  });
});

describe('session-list-panel figures', () => {
  it('states tokens before cost, split in and out', async () => {
    const el = await renderPanel([
      makeSession({
        estimatedCost: 0.42,
        tokenUsage: {
          prompt_tokens: 12400,
          completion_tokens: 3100,
          total_tokens: 15500,
          input_tokens: 12400,
          output_tokens: 3100,
          cache_read_tokens: 8200,
          cache_write_tokens: 0,
          uncached_input_tokens: 3900,
          cache_hit_ratio: 0.6777,
        },
      }),
    ]);

    const metric = el.shadowRoot?.querySelectorAll('.metric')[1];
    const figures = metric?.querySelector('token-figures')!;
    await (figures as unknown as { updateComplete: Promise<unknown> })
      .updateComplete;
    const tokenText = (figures.shadowRoot?.textContent || '').replace(
      /\s+/g,
      ' '
    );
    expect(tokenText).to.contain('12.4K in');
    expect(tokenText).to.contain('3.1K out');
    expect(tokenText).to.contain('cache 68% hit');

    // Cost follows the tokens in the same line, not the other way round.
    const rowText = (metric?.textContent || '').replace(/\s+/g, ' ');
    expect(rowText.trim().endsWith('$0.42')).to.be.true;
  });
});
