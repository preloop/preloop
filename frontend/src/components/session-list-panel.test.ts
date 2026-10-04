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
    noteCount: 0,
    latestNoteAuthorDisplay: null,
    latestNoteAuthorAuthMethod: null,
    latestNoteAt: null,
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

describe('session-list-panel note indicator', () => {
  it('shows the count and the most recent author on a noted session', async () => {
    const el = await renderPanel([
      makeSession({
        id: 'session-noted',
        noteCount: 3,
        latestNoteAuthorDisplay: 'Reviewer',
        latestNoteAuthorAuthMethod: 'agent',
        latestNoteAt: '2026-03-09T20:00:00Z',
      }),
    ]);

    const row = el.shadowRoot?.querySelector(
      '[data-testid="session-notes-session-noted"]'
    );
    expect(row).to.exist;
    expect(row?.textContent).to.contain('3 notes');
    expect(row?.textContent).to.contain('Reviewer');
    expect(row?.getAttribute('title')).to.equal(
      'Most recent note from Reviewer (agent)'
    );
  });

  it('shows nothing at all on a session nobody noted', async () => {
    const el = await renderPanel([makeSession({ id: 'session-quiet' })]);

    expect(
      el.shadowRoot?.querySelector(
        '[data-testid="session-notes-session-quiet"]'
      )
    ).to.equal(null);
    expect(el.shadowRoot?.textContent).to.not.contain('note');
  });

  it('marks an agent author apart from a human one', async () => {
    const el = await renderPanel([
      makeSession({
        id: 'by-agent',
        noteCount: 1,
        latestNoteAuthorDisplay: 'Reviewer',
        latestNoteAuthorAuthMethod: 'agent',
      }),
      makeSession({
        id: 'by-human',
        noteCount: 1,
        latestNoteAuthorDisplay: 'Jane Doe',
        latestNoteAuthorAuthMethod: 'jwt',
      }),
    ]);

    const byAgent = el.shadowRoot?.querySelector(
      '[data-testid="session-notes-by-agent"]'
    )!;
    const byHuman = el.shadowRoot?.querySelector(
      '[data-testid="session-notes-by-human"]'
    )!;

    expect(byAgent.getAttribute('data-note-author-kind')).to.equal('agent');
    expect(byHuman.getAttribute('data-note-author-kind')).to.equal('human');
    expect(byAgent.querySelector('sl-icon')?.getAttribute('name')).to.not.equal(
      byHuman.querySelector('sl-icon')?.getAttribute('name')
    );
    expect(byAgent.textContent).to.contain('1 note');
    expect(byAgent.textContent).to.not.contain('1 notes');
  });

  it('names an author the server did not record', async () => {
    const el = await renderPanel([
      makeSession({
        id: 'session-anon',
        noteCount: 2,
        latestNoteAuthorDisplay: null,
        latestNoteAuthorAuthMethod: null,
      }),
    ]);

    const row = el.shadowRoot?.querySelector(
      '[data-testid="session-notes-session-anon"]'
    );
    expect(row?.textContent).to.contain('Unknown author');
    expect(row?.getAttribute('data-note-author-kind')).to.equal('unknown');
    expect(row?.querySelector('sl-icon')?.getAttribute('name')).to.equal(
      'question-circle'
    );
  });
});

describe('session-list-panel artifact cell (#1084)', () => {
  it('renders nothing for a session without artifacts', async () => {
    const el = await renderPanel([makeSession({ artifactCounts: {} })]);
    expect(el.shadowRoot?.querySelector('.artifact-row')).to.equal(null);
  });

  it('folds kinds into header groups and keeps the icons outside the select button', async () => {
    const el = await renderPanel([
      makeSession({
        artifactCounts: { recording: 1, screencast: 2, screenshot: 1 },
      }),
    ]);
    const buttons = Array.from(
      el.shadowRoot!.querySelectorAll('.artifact-kind')
    ).map((node) => node.getAttribute('data-kind'));
    expect(buttons).to.deep.equal(['other', 'screenshot']);

    // Selection is a real button and the icons are its siblings, not its
    // children, so a screen reader reaches both.
    const select = el.shadowRoot!.querySelector(
      '.session-card > button.session-select'
    ) as HTMLButtonElement;
    expect(select).to.exist;
    expect(select.querySelector('.artifact-kind')).to.equal(null);
    expect(
      el.shadowRoot!.querySelectorAll('.session-card > .artifact-row button')
    ).to.have.length(2);
    expect(
      el.shadowRoot!.querySelector('.session-card')!.getAttribute('role')
    ).to.equal(null);
    const events: CustomEvent[] = [];
    el.addEventListener('session-selected', (event) =>
      events.push(event as CustomEvent)
    );
    select.click();
    (
      el.shadowRoot!.querySelector(
        '.artifact-kind[data-kind="screenshot"]'
      ) as HTMLButtonElement
    ).click();
    expect(events.map((event) => event.detail)).to.deep.equal([
      { sessionId: 'session-1', artifactKind: null },
      { sessionId: 'session-1', artifactKind: 'screenshot' },
    ]);
  });
});
