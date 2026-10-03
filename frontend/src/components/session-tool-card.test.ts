import { expect, fixture, html } from '@open-wc/testing';
import './session-tool-card';
import type { SessionToolCard } from './session-tool-card';
import type { LiveToolCall } from '../utils/live-session';

/** Synthetic fixture: placeholder tool names, invented call ids. */
function call(overrides: Partial<LiveToolCall> = {}): LiveToolCall {
  return {
    key: 'gw:call_1',
    id: 'call_1',
    stableId: true,
    name: 'terminal',
    serverName: null,
    state: 'requested',
    summary: 'pytest -q',
    argumentsText: '{\n  "command": "pytest -q"\n}',
    resultText: null,
    durationMs: null,
    timestamp: '2026-10-02T09:00:05Z',
    redacted: false,
    truncated: false,
    repositoryArgs: null,
    ...overrides,
  };
}

async function renderCard(value: LiveToolCall): Promise<SessionToolCard> {
  const el = await fixture<SessionToolCard>(
    html`<session-tool-card .call=${value}></session-tool-card>`
  );
  await el.updateComplete;
  return el;
}

describe('session-tool-card', () => {
  it('names the tool, its lifecycle state and the deciding argument', async () => {
    const el = await renderCard(call({ state: 'completed', durationMs: 1200 }));

    const card = el.shadowRoot!.querySelector(
      '[data-testid="session-tool-card"]'
    )!;
    expect(card.getAttribute('data-state')).to.equal('completed');
    expect(card.querySelector('.name')?.textContent?.trim()).to.equal(
      'terminal'
    );
    expect(card.querySelector('.state')?.textContent?.trim()).to.equal(
      'completed'
    );
    expect(card.querySelector('.duration')?.textContent?.trim()).to.equal(
      '1.2s'
    );
    expect(
      card.querySelector('[data-testid="tool-summary"]')?.textContent?.trim()
    ).to.equal('pytest -q');
  });

  it('says requested, not running, when only the call was observed', async () => {
    const el = await renderCard(call({ state: 'requested' }));

    expect(
      el
        .shadowRoot!.querySelector('[data-testid="session-tool-card"]')!
        .getAttribute('data-state')
    ).to.equal('requested');
    expect(
      el.shadowRoot!.querySelector('.state')?.textContent?.trim()
    ).to.equal('requested');
  });

  it('keeps arguments and the result collapsed until asked', async () => {
    const el = await renderCard(
      call({ state: 'completed', resultText: '42 passed' })
    );

    expect(
      el.shadowRoot!.querySelector('[data-testid="tool-arguments"]')
    ).to.equal(null);

    const toggle = el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-testid="tool-toggle"]'
    )!;
    expect(toggle.getAttribute('aria-expanded')).to.equal('false');
    toggle.click();
    await el.updateComplete;

    expect(toggle.getAttribute('aria-expanded')).to.equal('true');
    expect(
      el.shadowRoot!.querySelector('[data-testid="tool-arguments"]')
        ?.textContent
    ).to.contain('pytest -q');
    expect(
      el.shadowRoot!.querySelector('[data-testid="tool-result"]')?.textContent
    ).to.contain('42 passed');
  });

  it('offers no expander when nothing was captured', async () => {
    const el = await renderCard(
      call({ argumentsText: null, resultText: null, summary: '' })
    );

    expect(
      el.shadowRoot!.querySelector('[data-testid="tool-toggle"]')
    ).to.equal(null);
  });

  it('says so honestly when the capture policy withheld the payload', async () => {
    const el = await renderCard(
      call({ argumentsText: null, redacted: true, resultText: null })
    );

    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-testid="tool-toggle"]'
    )!.click();
    await el.updateComplete;

    expect(
      el.shadowRoot!.querySelector('[data-testid="tool-arguments-withheld"]')
        ?.textContent
    ).to.contain('Withheld by capture policy');
  });

  it('renders a nameless tool honestly instead of blanking the row', async () => {
    const el = await renderCard(call({ name: null, summary: '' }));

    expect(el.shadowRoot!.querySelector('.name')?.textContent?.trim()).to.equal(
      'tool (name not captured)'
    );
  });

  it('flags a row whose identity could not be proven', async () => {
    const el = await renderCard(call({ id: null, stableId: false }));

    const badges = Array.from(el.shadowRoot!.querySelectorAll('sl-badge')).map(
      (badge) => badge.textContent?.trim()
    );
    expect(badges).to.include('unmatched');
  });

  it('flags redaction and truncation from the producer', async () => {
    const el = await renderCard(call({ redacted: true, truncated: true }));

    const badges = Array.from(el.shadowRoot!.querySelectorAll('sl-badge')).map(
      (badge) => badge.textContent?.trim()
    );
    expect(badges).to.include('Redacted');
    expect(badges).to.include('Truncated');
  });

  it('renders captured payloads as text, never as markup', async () => {
    const el = await renderCard(
      call({
        argumentsText: '<img src=x onerror="window.__pwned = true">',
        resultText: '<script>window.__pwned = true</script>',
      })
    );
    el.shadowRoot!.querySelector<HTMLButtonElement>(
      '[data-testid="tool-toggle"]'
    )!.click();
    await el.updateComplete;

    expect(el.shadowRoot!.querySelector('img')).to.equal(null);
    expect(el.shadowRoot!.querySelector('script')).to.equal(null);
    expect(
      el.shadowRoot!.querySelector('[data-testid="tool-arguments"]')
        ?.textContent
    ).to.contain('onerror');
    expect((window as unknown as Record<string, unknown>).__pwned).to.equal(
      undefined
    );
  });
});
