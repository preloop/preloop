import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './execution-records-card';
import type { ExecutionRecordsCard } from './execution-records-card';
import { invalidateApiCaches } from '../api';

const EXECUTION = '22222222-2222-4222-8222-222222222222';

const EVIDENCE = {
  status: 'available',
  size_bytes: 2048,
  sha256: 'abc123def456abc123def456abc123def456',
  integrity: 'not_checked',
  integrity_note: 'Availability only.',
  legal_hold: false,
  object_lock: false,
  expires_at: '2026-10-25T00:00:00Z',
};

describe('ExecutionRecordsCard', () => {
  let fetchStub: sinon.SinonStub;

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  function install() {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'token');
    localStorage.setItem('refreshToken', 'refresh');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      const method = String(init?.method || 'GET').toUpperCase();
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
      if (url.includes('/evidence-status')) return json(EVIDENCE);
      if (url.endsWith('/evidence') && method === 'GET') {
        return new Response(new Uint8Array([1, 2]), {
          status: 200,
          headers: {
            'Content-Type': 'application/gzip',
            'Content-Disposition': 'attachment; filename="pack.tar.gz"',
            'X-Preloop-Evidence-Integrity-State': 'verified',
            'X-Preloop-Evidence-SHA256': 'abc123',
            'X-Preloop-Signing-Key-Id': 'psk_active',
          },
        });
      }
      if (url.includes('/retention/holds')) return json([]);
      return json({});
    });
  }

  it('shows pack status, object lock as false, and the download integrity header', async () => {
    install();
    const click = sinon.stub(HTMLAnchorElement.prototype, 'click');
    const el = await fixture<ExecutionRecordsCard>(html`
      <execution-records-card
        execution-id=${EXECUTION}
      ></execution-records-card>
    `);
    await waitUntil(
      () => (el.shadowRoot?.textContent || '').includes('Present'),
      'status did not render'
    );
    const text = el.shadowRoot!.textContent || '';
    expect(text).to.contain('False');
    expect(text).to.contain('not a storage guarantee');
    expect(text).to.contain('not_checked');
    (
      el.shadowRoot!.querySelector(
        '[data-testid="download-evidence"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () =>
        (el.shadowRoot?.textContent || '').includes(
          'Integrity header: verified'
        ),
      'download note did not render'
    );
    expect(el.shadowRoot!.textContent).to.contain('preloop evidence verify');
    click.restore();
  });
});
