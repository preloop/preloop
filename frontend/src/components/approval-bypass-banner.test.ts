/**
 * Theme contract for the approval-bypass banner.
 *
 * The banner is one of the two governance states the console promises are
 * impossible to miss, so its text has to stay readable whichever theme the
 * reader picked, independent of the operating system's colour scheme.
 */
import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './approval-bypass-banner.ts';
import type { ApprovalBypassBanner } from './approval-bypass-banner.ts';
import type { ApprovalBypassStatus } from '../types';

const ACTIVE: ApprovalBypassStatus = {
  active: true,
  auto_approve_active: true,
  soonest_expiry: '2099-01-01T00:00:00Z',
  bypasses: [
    {
      id: 'bypass-1',
      account_id: 'account-1',
      user_id: 'user-1',
      managed_agent_id: null,
      mode: 'auto_approve',
      reason: null,
      created_by_user_id: 'user-1',
      created_via: 'console',
      created_at: '2026-09-06T12:00:00Z',
      expires_at: '2099-01-01T00:00:00Z',
      revoked_at: null,
      revoked_by_user_id: null,
      auto_approved_count: 2,
    },
  ],
};

describe('approval-bypass-banner', () => {
  let restoreFetch: () => void;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    const original = window.fetch;
    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/approval-bypasses/status')) {
        return new Response(JSON.stringify(ACTIVE), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return original(input, init);
    }) as typeof window.fetch;
    restoreFetch = () => {
      window.fetch = original;
    };
  });

  afterEach(() => {
    restoreFetch();
    localStorage.removeItem('accessToken');
  });

  it("takes its colours from the console theme, not the OS's", async () => {
    const cssText = (
      customElements.get('approval-bypass-banner') as unknown as {
        styles: { cssText: string };
      }
    ).styles.cssText;
    expect(cssText).to.not.contain('prefers-color-scheme');
    expect(cssText).to.not.match(/#[0-9a-f]{3,6}\b/i);

    const el = await fixture<ApprovalBypassBanner>(
      html`<approval-bypass-banner
        style="--console-body-color: rgb(1, 2, 3)"
      ></approval-bypass-banner>`
    );
    await waitUntil(() => el.shadowRoot!.querySelector('.banner'));
    const banner = el.shadowRoot!.querySelector('.banner')!;
    expect(banner.textContent).to.contain('auto-approved');
    expect(getComputedStyle(banner).color).to.equal('rgb(1, 2, 3)');
  });
});
