import { expect, fixture, html, oneEvent } from '@open-wc/testing';

import './similar-sessions-panel.ts';
import type { SimilarSessionsPanel } from './similar-sessions-panel.ts';
import type {
  SimilarSessionMatch,
  SimilarSessionResult,
  SimilarSessionsResponse,
} from '../types';

function makeMatch(
  overrides: Partial<SimilarSessionMatch> = {}
): SimilarSessionMatch {
  return {
    document_id: 'doc-1',
    source_kind: 'gateway_interaction',
    source_id: 'usage-1',
    chunk_index: 0,
    occurred_at: '2026-09-01T10:00:00Z',
    role: 'user',
    similarity: 0.82,
    band: 'close',
    redaction_state: 'clear',
    text: 'the deploy kept timing out on the migration step',
    probe: {
      document_id: 'doc-probe',
      source_kind: 'gateway_interaction',
      source_id: 'usage-probe',
      chunk_index: 0,
      occurred_at: '2026-09-10T10:00:00Z',
      role: 'user',
    },
    ...overrides,
  };
}

function makeResult(
  overrides: Partial<SimilarSessionResult> = {}
): SimilarSessionResult {
  return {
    runtime_session_id: '11111111-1111-4111-8111-111111111111',
    session_source_type: 'claude_code',
    session_source_id: 'src-1',
    session_reference: 'ref-1',
    title: 'Migration retry loop',
    started_at: '2026-09-01T09:00:00Z',
    last_activity_at: '2026-09-01T11:00:00Z',
    score: 0.84,
    similarity: 0.82,
    band: 'close',
    matched_chunk_count: 2,
    matches: [makeMatch()],
    ...overrides,
  };
}

function makeResponse(
  overrides: Partial<SimilarSessionsResponse> = {}
): SimilarSessionsResponse {
  return {
    runtime_session_id: '22222222-2222-4222-8222-222222222222',
    model_identity: 'openai:text-embedding-3-small@1536',
    embedded_chunks: 40,
    probe_chunks: 8,
    pending_chunks: 0,
    window_days: null,
    limit: 5,
    max_matches_per_session: 2,
    degraded: { semantic: false, reasons: [], detail: null },
    elapsed_ms: 12,
    results: [makeResult()],
    ...overrides,
  };
}

/**
 * Stop a click this panel let through from actually navigating the test page.
 *
 * The listener is added after the component's own, so it reads whether the
 * panel prevented the default before taking it out of the browser's hands.
 */
function guardNavigation(link: HTMLAnchorElement): () => boolean {
  let preventedByPanel = false;
  link.addEventListener('click', (event) => {
    preventedByPanel = event.defaultPrevented;
    event.preventDefault();
  });
  return () => preventedByPanel;
}

async function renderPanel(
  response: SimilarSessionsResponse | null,
  extra: { loading?: boolean; error?: string } = {}
): Promise<SimilarSessionsPanel> {
  const el = await fixture<SimilarSessionsPanel>(
    html`<similar-sessions-panel
      runtime-session-id="22222222-2222-4222-8222-222222222222"
      .response=${response}
      .loading=${extra.loading ?? false}
      .error=${extra.error ?? ''}
    ></similar-sessions-panel>`
  );
  await el.updateComplete;
  return el;
}

describe('similar-sessions-panel', () => {
  it('asks for the list only once, and only when opened', async () => {
    const el = await renderPanel(null);
    let asks = 0;
    el.addEventListener('similar-sessions-requested', () => {
      asks += 1;
    });

    const details = el.shadowRoot?.querySelector('sl-details');
    expect(details).to.exist;
    expect(asks).to.equal(0);

    details?.dispatchEvent(new CustomEvent('sl-show'));
    details?.dispatchEvent(new CustomEvent('sl-show'));
    await el.updateComplete;

    expect(asks).to.equal(1);
  });

  it('asks again when the session under it changes', async () => {
    const el = await renderPanel(null);
    const asked: string[] = [];
    el.addEventListener('similar-sessions-requested', (event) => {
      asked.push((event as CustomEvent).detail.runtimeSessionId);
    });

    el.shadowRoot
      ?.querySelector('sl-details')
      ?.dispatchEvent(new CustomEvent('sl-show'));
    await el.updateComplete;

    el.runtimeSessionId = '33333333-3333-4333-8333-333333333333';
    await el.updateComplete;
    el.shadowRoot
      ?.querySelector('sl-details')
      ?.dispatchEvent(new CustomEvent('sl-show'));
    await el.updateComplete;

    expect(asked).to.deep.equal([
      '22222222-2222-4222-8222-222222222222',
      '33333333-3333-4333-8333-333333333333',
    ]);
  });

  it('shows the band as a word, keeping the number in the title only', async () => {
    const el = await renderPanel(makeResponse());

    const badge = el.shadowRoot?.querySelector('.entry sl-badge');
    expect(badge?.textContent?.trim()).to.equal('Close');
    expect(badge?.getAttribute('title')).to.contain('0.82');
    expect(el.shadowRoot?.textContent).to.not.contain('0.82 ');
  });

  it('renders the matching passage under the session it came from', async () => {
    const el = await renderPanel(makeResponse());

    const text = el.shadowRoot?.querySelector('.match')?.textContent || '';
    expect(text).to.contain('the deploy kept timing out');
  });

  it('says content was withheld instead of rendering an empty passage', async () => {
    const el = await renderPanel(
      makeResponse({
        results: [
          makeResult({
            matches: [makeMatch({ text: null, redaction_state: 'withheld' })],
          }),
        ],
      })
    );

    const text = el.shadowRoot?.querySelector('.match')?.textContent || '';
    expect(text).to.contain('withheld');
  });

  it('names what the comparison could not do rather than showing nothing', async () => {
    const el = await renderPanel(
      makeResponse({
        results: [],
        degraded: {
          semantic: true,
          reasons: ['similar_session_not_embedded'],
          detail: 'This session has nothing indexed to compare.',
        },
      })
    );

    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('No similar session found.');
    expect(text).to.contain('nothing indexed to compare');
  });

  it('falls back to its own sentence when the API sent codes with no detail', async () => {
    const el = await renderPanel(
      makeResponse({
        results: [],
        degraded: {
          semantic: true,
          reasons: ['semantic_not_enabled'],
          detail: null,
        },
      })
    );

    expect(el.shadowRoot?.textContent).to.contain(
      'Session embedding is off for this account.'
    );
  });

  it('links each entry at the other session so a new tab still works', async () => {
    const el = await renderPanel(makeResponse());

    const link = el.shadowRoot?.querySelector('a.entry');
    expect(link?.getAttribute('href')).to.equal(
      '/console/sessions?sessionId=11111111-1111-4111-8111-111111111111&replay=conversation'
    );
  });

  it('offers the click to the host, naming the session and its match', async () => {
    const el = await renderPanel(makeResponse());
    const link = el.shadowRoot?.querySelector('a.entry') as HTMLAnchorElement;
    guardNavigation(link);

    setTimeout(() =>
      link.dispatchEvent(
        new MouseEvent('click', {
          bubbles: true,
          composed: true,
          cancelable: true,
        })
      )
    );
    const event = (await oneEvent(
      el,
      'similar-session-selected'
    )) as CustomEvent;

    expect(event.detail.runtimeSessionId).to.equal(
      '11111111-1111-4111-8111-111111111111'
    );
    expect(event.detail.matchSourceId).to.equal('usage-1');
    expect(event.cancelable).to.be.true;
  });

  it('lets the link navigate when no host takes the click', async () => {
    const el = await renderPanel(makeResponse());
    const link = el.shadowRoot?.querySelector('a.entry') as HTMLAnchorElement;
    const preventedByPanel = guardNavigation(link);

    link.dispatchEvent(
      new MouseEvent('click', {
        bubbles: true,
        composed: true,
        cancelable: true,
      })
    );

    expect(preventedByPanel()).to.be.false;
  });

  it('stops the navigation when the host handled it in place', async () => {
    const el = await renderPanel(makeResponse());
    el.addEventListener('similar-session-selected', (event) =>
      event.preventDefault()
    );
    const link = el.shadowRoot?.querySelector('a.entry') as HTMLAnchorElement;
    const preventedByPanel = guardNavigation(link);

    link.dispatchEvent(
      new MouseEvent('click', {
        bubbles: true,
        composed: true,
        cancelable: true,
      })
    );

    expect(preventedByPanel()).to.be.true;
  });

  it('leaves a modified click to the browser', async () => {
    const el = await renderPanel(makeResponse());
    el.addEventListener('similar-session-selected', (event) =>
      event.preventDefault()
    );
    const link = el.shadowRoot?.querySelector('a.entry') as HTMLAnchorElement;
    const preventedByPanel = guardNavigation(link);

    link.dispatchEvent(
      new MouseEvent('click', {
        bubbles: true,
        composed: true,
        cancelable: true,
        metaKey: true,
      })
    );

    expect(preventedByPanel()).to.be.false;
  });

  it('says a failed load in a sentence instead of dropping the panel', async () => {
    const el = await renderPanel(null, {
      error: 'Failed to fetch similar sessions',
    });

    expect(el.shadowRoot?.textContent).to.contain(
      'Failed to fetch similar sessions'
    );
  });
});
