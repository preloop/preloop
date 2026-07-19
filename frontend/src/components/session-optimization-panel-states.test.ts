/**
 * Tests for the approved async-analysis panel states (launch console spec):
 * 1A analyzing, 2A failed + retry, 3A no-waste. These pin the exact copy,
 * the a11y contract, and the retry event — not the cosmetics.
 */
import { fixture, html, expect } from '@open-wc/testing';
import './session-optimization-panel';
import type { SessionOptimizationPanel } from './session-optimization-panel';

const SESSION = {
  id: 'sess-1',
  session_source_type: 'claude_code',
  session_source_id: 'workspace-1',
  session_reference: 'claude-session-1',
  runtime_principal_name: 'Claude Workspace',
  started_at: '2026-07-19T18:00:00Z',
  last_activity_at: '2026-07-19T20:00:00Z',
  ended_at: null,
  is_active_now: true,
  activity_status: 'active_now',
  total_requests: 1,
  successful_requests: 1,
  failed_requests: 0,
  token_usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
  estimated_cost: 0.01,
  last_request_at: '2026-07-19T20:00:00Z',
};

describe('SessionOptimizationPanel — async job states', () => {
  async function renderPanel(props: {
    jobState?: 'analyzing' | 'failed' | null;
    optimization?: Record<string, unknown> | null;
  }) {
    const el = (await fixture(
      html`<session-optimization-panel
        .session=${SESSION}
        .jobState=${props.jobState ?? null}
        .optimization=${props.optimization ?? null}
        .suggestions=${[]}
      ></session-optimization-panel>`
    )) as SessionOptimizationPanel;
    await el.updateComplete;
    return el;
  }

  describe('analyzing (1A)', () => {
    it('renders the approved copy and the indeterminate progress bar', async () => {
      const el = await renderPanel({ jobState: 'analyzing' });
      const text = (el.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
      expect(text).to.include('Analyzing this session');
      expect(text).to.include('Typically 1–2 minutes.');
      expect(text).to.include(
        'You can leave this tab open — results appear automatically.'
      );
      const bar = el.shadowRoot?.querySelector('[role="progressbar"]');
      expect(bar, 'progressbar role required').to.exist;
      // Indeterminate: no aria-valuenow.
      expect(bar?.hasAttribute('aria-valuenow')).to.be.false;
    });

    it('marks the panel busy and exposes a polite live region', async () => {
      const el = await renderPanel({ jobState: 'analyzing' });
      expect(el.shadowRoot?.querySelector('[aria-busy="true"]')).to.exist;
      const live = el.shadowRoot?.querySelector('[aria-live="polite"]');
      expect(live, 'polite live region required').to.exist;
    });

    it('does not render the suggestions tabs while analyzing', async () => {
      const el = await renderPanel({ jobState: 'analyzing' });
      expect(el.shadowRoot?.querySelector('sl-tab-group')).to.not.exist;
    });
  });

  describe('failed (2A)', () => {
    it('renders the approved failure copy in an alert', async () => {
      const el = await renderPanel({ jobState: 'failed' });
      const alert = el.shadowRoot?.querySelector('[role="alert"]');
      expect(alert, 'inline alert required').to.exist;
      const text = (alert?.textContent || '').replace(/\s+/g, ' ');
      expect(text).to.include('Analysis failed');
      expect(text).to.include(
        "The model request didn't complete. Nothing was changed."
      );
    });

    it('dispatches session-optimization-retry when Retry analysis is clicked', async () => {
      const el = await renderPanel({ jobState: 'failed' });
      let retried = false;
      el.addEventListener('session-optimization-retry', () => {
        retried = true;
      });
      const button = el.shadowRoot?.querySelector(
        '.job-retry-button'
      ) as HTMLButtonElement;
      expect(button, 'retry button required').to.exist;
      expect(button.textContent?.trim()).to.equal('Retry analysis');
      // A real <button>: keyboard focusable by default.
      expect(button.tabIndex).to.equal(0);
      button.click();
      expect(retried, 'retry event must bubble').to.be.true;
    });
  });

  describe('no-waste (3A)', () => {
    const zeroSavingsResult = {
      generated_by: 'local',
      fast_model_name: null,
      suggestions: [],
      potential_savings_tokens: 0,
    };

    it('renders the calm state for a zero-savings result', async () => {
      const el = await renderPanel({ optimization: zeroSavingsResult });
      const text = (el.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
      expect(text).to.include('No recoverable waste in this session');
      expect(text).to.include(
        'Short sessions rarely have any. Optimization pays off on longer, tool-heavy sessions.'
      );
      expect(el.shadowRoot?.querySelector('sl-tab-group')).to.not.exist;
    });

    it('links the demo video in a safe new tab', async () => {
      const el = await renderPanel({ optimization: zeroSavingsResult });
      const link = el.shadowRoot?.querySelector(
        '.job-no-waste a'
      ) as HTMLAnchorElement;
      expect(link, 'video link required').to.exist;
      expect(link.textContent).to.include('Watch it find real waste (2 min)');
      expect(link.href).to.include(
        'youtube.com/playlist?list=PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt'
      );
      expect(link.href).to.include('utm_source=console');
      expect(link.href).to.include('utm_campaign=series-ep1');
      expect(link.target).to.equal('_blank');
      expect(link.rel).to.include('noopener');
    });

    it('renders results normally when the result has real savings', async () => {
      const el = await renderPanel({
        optimization: {
          generated_by: 'local',
          fast_model_name: null,
          suggestions: [],
          potential_savings_tokens: 900,
        },
      });
      expect(el.shadowRoot?.querySelector('.job-no-waste')).to.not.exist;
      expect(el.shadowRoot?.querySelector('sl-tab-group')).to.exist;
    });
  });
});
