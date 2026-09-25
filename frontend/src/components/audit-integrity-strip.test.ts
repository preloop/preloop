import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './audit-integrity-strip';
import type { AuditIntegrityStrip } from './audit-integrity-strip';
import { invalidateApiCaches } from '../api';

const STATUS = {
  enabled: true,
  head_seq: 40,
  head_hash: 'abc',
  last_sealed_at: '2026-09-25T00:00:00Z',
  pruned_below_seq: 0,
  sealed_rows: 40,
  unsealed_rows: 2,
  seal_lag_seconds: 15,
  checkpoint_interval: 1000,
  latest_checkpoint: {
    seq: 40,
    chain_hash: 'abc',
    row_count: 40,
    checkpointed_at: '2026-09-25T00:00:00Z',
    signing_key_id: 'psk_active',
    signature: 'sig',
    signed_payload: {},
    digest: 'dig',
    signature_document: null,
  },
  active_key_id: 'psk_active',
};

describe('AuditIntegrityStrip', () => {
  let fetchStub: sinon.SinonStub;

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  it('names the sealed head and links to Records', async () => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'token');
    localStorage.setItem('refreshToken', 'refresh');
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        if (url.includes('/auth/users/me')) {
          return json({
            username: 'operator',
            email: 'operator@example.com',
            email_verified: true,
            permissions: ['view_audit_logs'],
          });
        }
        if (url.includes('/audit/chain/status')) return json(STATUS);
        return json({});
      });
    const el = await fixture<AuditIntegrityStrip>(
      html`<audit-integrity-strip></audit-integrity-strip>`
    );
    await waitUntil(
      () =>
        el.shadowRoot?.querySelector('[data-testid="audit-integrity-strip"]'),
      'strip did not render'
    );
    const text = el.shadowRoot!.textContent || '';
    expect(text).to.contain('Sealed through seq 40');
    expect(text).to.contain('seal lag 15s');
    const link = el.shadowRoot!.querySelector('a');
    expect(link?.getAttribute('href')).to.equal(
      '/console/settings/records#audit-integrity'
    );
  });
});
