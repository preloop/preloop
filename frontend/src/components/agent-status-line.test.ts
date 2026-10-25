import { expect, fixture, html } from '@open-wc/testing';

import './agent-status-line';
import type { AgentStatusLine } from './agent-status-line';
import { deriveAgentStatus } from '../utils/agent-status';

const T0 = Date.UTC(2026, 9, 21, 20, 0, 0);

function stubReducedMotion(reduce: boolean): () => void {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) =>
    ({
      matches: reduce && query.includes('reduce'),
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }) as unknown as MediaQueryList) as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

describe('agent-status-line', () => {
  const working = deriveAgentStatus({
    runtime: 'claude',
    turnId: 'turn-1',
    turnStartedAt: T0,
    modelBusy: true,
    now: T0 + 12_000,
  });

  it('reduced motion renders a static glyph', async () => {
    const restore = stubReducedMotion(true);
    try {
      const el = await fixture<AgentStatusLine>(
        html`<agent-status-line .status=${working}></agent-status-line>`
      );
      const glyph = el.querySelector('.agent-status-glyph')!;
      expect(glyph.textContent).to.equal('✻');
      expect(glyph.classList.contains('is-animated')).to.equal(false);
      expect(el.querySelectorAll('[data-animated]').length).to.equal(0);
      // Light DOM: the words are in the page.
      expect(el.textContent).to.contain('(12s · thinking)');
    } finally {
      restore();
    }
  });

  it('only one animated element', async () => {
    const restore = stubReducedMotion(false);
    try {
      const el = await fixture<AgentStatusLine>(
        html`<agent-status-line .status=${working}></agent-status-line>`
      );
      expect(el.querySelectorAll('[data-animated]').length).to.equal(1);
      expect(el.querySelectorAll('.is-animated').length).to.equal(1);
      const animated = el.querySelector('.is-animated')!;
      expect(animated.getAttribute('aria-hidden')).to.equal('true');
      expect(el.querySelectorAll('sl-spinner').length).to.equal(0);
    } finally {
      restore();
    }
  });

  it('renders nothing without a live line and marks a waiting line amber', async () => {
    const el = await fixture<AgentStatusLine>(
      html`<agent-status-line
        .status=${deriveAgentStatus({ runtime: 'codex', now: T0 })}
      ></agent-status-line>`
    );
    expect(el.textContent!.trim()).to.equal('');
    el.status = deriveAgentStatus({
      runtime: 'codex',
      pendingApprovals: 1,
      pendingSince: T0,
      now: T0 + 5000,
    });
    await el.updateComplete;
    expect(el.getAttribute('tone')).to.equal('warning');
    expect(el.textContent).to.contain('Waiting for approval (5s)');
  });
});
