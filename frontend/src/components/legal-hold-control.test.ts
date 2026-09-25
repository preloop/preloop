import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './legal-hold-control';
import type { LegalHoldControl } from './legal-hold-control';
import { invalidateApiCaches } from '../api';
import { resetConfirmDialogForTests } from './confirm-dialog';

const RESOURCE = '11111111-1111-4111-8111-111111111111';

const HOLD = {
  id: 'hold-9',
  resource_type: 'approval',
  resource_id: RESOURCE,
  reason: 'incident review in progress',
  placed_by_user_id: 'user-1',
  placed_at: '2026-09-20T00:00:00Z',
  released_by_user_id: null,
  released_at: null,
  release_reason: null,
  active: true,
};

describe('LegalHoldControl', () => {
  let fetchStub: sinon.SinonStub;
  const calls: { url: string; method: string }[] = [];

  function install(holds: unknown[]) {
    calls.length = 0;
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'token');
    localStorage.setItem('refreshToken', 'refresh');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      const method = String(init?.method || 'GET').toUpperCase();
      calls.push({ url, method });
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), {
          status,
          headers: { 'Content-Type': 'application/json' },
        });
      if (url.includes('/auth/users/me')) {
        return json({
          username: 'operator',
          email: 'operator@example.com',
          email_verified: true,
          permissions: null,
        });
      }
      if (url.includes('/release')) {
        return json({ ...HOLD, active: false });
      }
      if (method === 'POST') {
        return json(HOLD, 201);
      }
      if (url.includes('/retention/holds')) {
        return json(holds);
      }
      return json({});
    });
  }

  afterEach(() => {
    fetchStub.restore();
    resetConfirmDialogForTests();
    localStorage.clear();
  });

  it('places a hold and shows the badge from the response', async () => {
    install([]);
    const el = await fixture<LegalHoldControl>(html`
      <legal-hold-control
        resource-type="approval"
        resource-id=${RESOURCE}
      ></legal-hold-control>
    `);
    await waitUntil(
      () => el.shadowRoot?.querySelector('[data-testid="place-hold"]'),
      'place was not offered'
    );
    (
      el.shadowRoot!.querySelector('[data-testid="place-hold"]') as HTMLElement
    ).click();
    await el.updateComplete;
    const reason = el.shadowRoot!.querySelector(
      '[data-testid="hold-reason"]'
    ) as HTMLInputElement;
    reason.value = 'incident review in progress';
    reason.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    (
      el.shadowRoot!.querySelector(
        '[data-testid="hold-confirm"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () => el.shadowRoot?.querySelector('[data-testid="hold-badge"]'),
      'badge did not appear'
    );
    expect(calls.some((call) => call.method === 'POST')).to.equal(true);
  });

  it('releases the hold that is already on the resource', async () => {
    install([HOLD]);
    const el = await fixture<LegalHoldControl>(html`
      <legal-hold-control
        resource-type="approval"
        resource-id=${RESOURCE}
      ></legal-hold-control>
    `);
    await waitUntil(
      () => el.shadowRoot?.querySelector('[data-testid="release-hold"]'),
      'release was not offered'
    );
    expect(el.shadowRoot!.textContent).to.contain('Legal hold');
    (
      el.shadowRoot!.querySelector(
        '[data-testid="release-hold"]'
      ) as HTMLElement
    ).click();
    await el.updateComplete;
    const reason = el.shadowRoot!.querySelector(
      '[data-testid="hold-reason"]'
    ) as HTMLInputElement;
    reason.value = 'review closed today';
    reason.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    (
      el.shadowRoot!.querySelector(
        '[data-testid="hold-confirm"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () => calls.some((call) => call.url.includes('/release')),
      'hold was not released'
    );
  });
});
