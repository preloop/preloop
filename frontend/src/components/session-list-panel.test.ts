import { expect, fixture, html } from '@open-wc/testing';

import './session-list-panel.ts';
import type { SessionListPanel } from './session-list-panel.ts';
import { loadShoelaceTokens } from '../utils/test-shoelace-theme';
import { setViewport } from '@web/test-runner-commands';
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
      makeSession({
        id: 'a',
        status: 'active_now',
        raw: { turn_state: 'working' },
      }),
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
    // The row is one line in the list column: the cache rate moved to the
    // figures' tooltip (#1585).
    expect(tokenText).to.not.contain('cache 68% hit');
    expect(
      figures.shadowRoot?.querySelector('[title]')?.getAttribute('title')
    ).to.contain('68% hit');

    // Cost follows the tokens in the same line, not the other way round.
    const row = el.shadowRoot?.querySelector('.metric-row') as HTMLElement;
    const rowText = (row.textContent || '').replace(/\s+/g, ' ');
    expect(rowText.trim().endsWith('$0.42 est.')).to.be.true;
    expect(getComputedStyle(row).flexWrap).to.equal('nowrap');
  });
});

describe('session-list-panel empty figures', () => {
  it('drops the token segment when no tokens were recorded', async () => {
    const el = await renderPanel([
      makeSession({ totalRequests: 0, estimatedCost: 0 }),
    ]);
    const row = el.shadowRoot?.querySelector('.metric-row') as HTMLElement;
    const rowText = (row.textContent || '').replace(/\s+/g, ' ').trim();
    expect(rowText).to.equal('0 requests · $0.00 est.');
    expect(row.querySelector('token-figures')).to.not.exist;
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

  it('folds kinds into header groups and keeps the icons outside the select link', async () => {
    const el = await renderPanel([
      makeSession({
        artifactCounts: { recording: 1, screencast: 2, screenshot: 1 },
      }),
    ]);
    const buttons = Array.from(
      el.shadowRoot!.querySelectorAll('.artifact-kind')
    ).map((node) => node.getAttribute('data-kind'));
    expect(buttons).to.deep.equal(['other', 'screenshot']);

    // Selection is a real link and the icons are its siblings, not its
    // children, so a screen reader reaches both.
    const select = el.shadowRoot!.querySelector(
      '.session-card > a.session-select'
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

describe('session-list-panel compact rows (#1585)', () => {
  it('puts the subtitle and last activity on one truncating line', async () => {
    const el = await renderPanel([
      makeSession({
        subtitle: 'Gateway · m-2 · anthropic',
        lastActivityAt: '2026-10-12T19:45:28Z',
      }),
    ]);
    const metas = el.shadowRoot!.querySelectorAll('.session-select .meta');
    expect(metas.length).to.equal(1);
    const line = metas[0].textContent!.trim();
    expect(line.startsWith('Gateway · m-2 · anthropic · ')).to.be.true;
    expect(line).to.not.contain('Last activity');
    expect(metas[0].getAttribute('title')).to.match(
      /^Gateway · m-2 · anthropic · Last activity /
    );
    expect(getComputedStyle(metas[0]).whiteSpace).to.equal('nowrap');
  });

  it('keeps a plain row under 80 px tall', async () => {
    const el = await renderPanel([
      makeSession({ subtitle: 'Gateway · m-2 · anthropic' }),
    ]);
    const card = el.shadowRoot!.querySelector('.session-card') as HTMLElement;
    expect(card.getBoundingClientRect().height).to.be.lessThan(80);
  });
});

describe('session-list-panel navigation', () => {
  it('uses one tab stop and arrow keys without selecting until Enter', async () => {
    const el = await renderPanel([
      makeSession({ id: 'first' }),
      makeSession({ id: 'second', artifactCounts: { screenshot: 1 } }),
      makeSession({ id: 'third' }),
    ]);
    const links = Array.from(
      el.shadowRoot!.querySelectorAll<HTMLAnchorElement>('a.session-select')
    );
    expect(links).to.have.length(3);
    expect(links.map((link) => link.tabIndex)).to.deep.equal([0, -1, -1]);
    let selected = '';
    el.addEventListener('session-selected', (event) => {
      selected = (event as CustomEvent).detail.sessionId;
    });
    links[0].focus();
    links[0].dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'ArrowDown',
        bubbles: true,
        cancelable: true,
      })
    );
    await el.updateComplete;
    expect(el.shadowRoot!.activeElement).to.equal(links[1]);
    expect(selected).to.equal('');
    expect(links.map((link) => link.tabIndex)).to.deep.equal([-1, 0, -1]);
    expect(
      el
        .shadowRoot!.querySelector('button.artifact-kind')!
        .getAttribute('tabindex')
    ).to.equal('0');
    links[1].click();
    expect(selected).to.equal('second');
    links[1].dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'End',
        bubbles: true,
        cancelable: true,
      })
    );
    await el.updateComplete;
    expect(el.shadowRoot!.activeElement).to.equal(links[2]);
    el.sessions = [makeSession({ id: 'first' })];
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('a.session-select')!.getAttribute('tabindex')
    ).to.equal('0');
  });

  it('keeps modified clicks native and links to the selected identity', async () => {
    const el = await renderPanel([makeSession({ id: 'session-example' })]);
    const link =
      el.shadowRoot!.querySelector<HTMLAnchorElement>('a.session-select')!;
    expect(link).to.exist;
    expect(new URL(link.href).searchParams.get('sessionId')).to.equal(
      'session-example'
    );
    let selections = 0;
    el.addEventListener('session-selected', () => selections++);
    for (const init of [
      { metaKey: true },
      { ctrlKey: true },
      { shiftKey: true },
      { button: 1 },
    ]) {
      const click = new MouseEvent('click', {
        ...init,
        bubbles: true,
        cancelable: true,
      });
      // Observe the component's decision before preventing browser navigation.
      let prevented = true;
      link.addEventListener(
        'click',
        (event) => {
          prevented = event.defaultPrevented;
          event.preventDefault();
        },
        { once: true }
      );
      link.dispatchEvent(click);
      expect(prevented).to.be.false;
    }
    expect(selections).to.equal(0);
  });
});

describe('session-list-panel design (#2431)', () => {
  for (const width of [1440, 390]) {
    it(`uses hairlines and a selection rule at ${width}px`, async () => {
      await loadShoelaceTokens();
      await setViewport({ width, height: 844 });
      const el = await renderPanel([
        makeSession({ id: 'first', lastActivityAt: '2026-01-01T12:00:00Z' }),
        makeSession({ id: 'second' }),
      ]);
      el.style.setProperty('--console-hairline', 'var(--sl-color-neutral-200)');
      el.activeSessionId = 'first';
      await el.updateComplete;
      const row = el.shadowRoot!.querySelector<HTMLElement>(
        '.session-card.active'
      )!;
      const style = getComputedStyle(row);
      expect(style.backgroundColor).to.equal('rgba(0, 0, 0, 0)');
      expect(style.borderLeftWidth).to.equal('3px');
      expect(style.borderBottomWidth).to.equal('1px');
      expect(style.borderTopWidth).to.equal('0px');
      expect(style.boxShadow).to.equal('none');
      const title = row.querySelector<HTMLElement>('.title')!;
      const cost = row.querySelector<HTMLElement>('.metric.cost')!;
      expect(getComputedStyle(cost).color).to.equal(
        getComputedStyle(title).color
      );
      expect(cost.textContent).to.contain('est.');
      expect(row.querySelector('.meta')!.textContent).to.contain('ago');
      expect(row.querySelector('.meta')!.getAttribute('title')).to.contain(
        'Last activity'
      );
      expect(row.getBoundingClientRect().right).to.be.at.most(width);
    });
  }
});
