import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import './records-view';
import type { RecordsView } from './records-view';
import { invalidateApiCaches } from '../../../api';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';

const STATUS = {
  enabled: true,
  head_seq: 12000,
  head_hash: 'abc',
  last_sealed_at: '2026-09-25T00:00:00Z',
  pruned_below_seq: 10,
  sealed_rows: 11990,
  unsealed_rows: 4,
  seal_lag_seconds: 60,
  checkpoint_interval: 1000,
  latest_checkpoint: {
    seq: 12000,
    chain_hash: 'abc',
    row_count: 11990,
    checkpointed_at: '2026-09-25T00:00:00Z',
    signing_key_id: 'psk_active',
    signature: 'sig',
    signed_payload: {},
    digest: 'dig',
    signature_document: null,
  },
  active_key_id: 'psk_active',
};

const KEYS = {
  active_key_id: 'psk_active',
  signature_schema: 'preloop.signature/v1',
  signed_bytes_format: 'preloop.signature/v1',
  keys: [
    {
      key_id: 'psk_active',
      algorithm: 'ed25519',
      public_key: 'PUBKEY',
      active: true,
      created_at: '2026-09-01T00:00:00Z',
      retired_at: null,
    },
  ],
};

const RETENTION = {
  floor_days: 183,
  default_days: 365,
  max_days: 7300,
  purge_enabled: false,
  purge_dry_run: true,
  purge_window_utc: '1-5',
  evidence_payload_hours: 720,
  classes: [
    {
      record_class: 'audit',
      label: 'Audit log rows',
      days: 365,
      source: 'account',
      floored: false,
    },
    {
      record_class: 'usage',
      label: 'Usage',
      days: -1,
      source: 'subscription_history',
      floored: true,
    },
  ],
};

const HOLD = {
  id: 'hold-1',
  resource_type: 'execution',
  resource_id: '11111111-1111-4111-8111-111111111111',
  reason: 'incident review in progress',
  placed_by_user_id: 'user-1',
  placed_at: '2026-09-20T00:00:00Z',
  released_by_user_id: null,
  released_at: null,
  release_reason: null,
  active: true,
};

function profile(permissions: string[] | null) {
  return {
    username: 'operator',
    email: 'operator@example.com',
    email_verified: true,
    permissions,
  };
}

describe('RecordsView', () => {
  let fetchStub: sinon.SinonStub;
  const calls: { url: string; method: string; body: string }[] = [];

  function install(options: {
    permissions?: string[] | null;
    verify?: Record<string, unknown>;
    holds?: unknown[];
  }) {
    calls.length = 0;
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'token');
    localStorage.setItem('refreshToken', 'refresh');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      const method = String(init?.method || 'GET').toUpperCase();
      calls.push({ url, method, body: String(init?.body ?? '') });
      const json = (data: unknown, status = 200) =>
        new Response(JSON.stringify(data), {
          status,
          headers: { 'Content-Type': 'application/json' },
        });
      if (url.includes('/auth/users/me')) {
        return json(profile(options.permissions ?? null));
      }
      if (url.includes('/audit/chain/status')) return json(STATUS);
      if (url.includes('/audit/chain/verify')) {
        return json(
          options.verify ?? {
            account_id: 'acc',
            status: 'ok',
            checked_rows: 10000,
            start_seq: 2001,
            end_seq: 12000,
            head_seq: 12000,
            pruned_below_seq: 10,
            unsealed_rows: 4,
            truncated: false,
            out_of_order_rows: 0,
            first_break: null,
            checkpoints_verified: 2,
            checkpoint_failures: [],
          }
        );
      }
      if (url.includes('/audit/chain/checkpoints')) return json([]);
      if (url.includes('/signing/keys')) return json(KEYS);
      if (url.includes('/retention/settings') && method === 'PUT') {
        return json(RETENTION);
      }
      if (url.includes('/retention/settings')) return json(RETENTION);
      if (url.includes('/purge-preview')) {
        return json({
          account_id: 'acc',
          purge_enabled: false,
          total: 3,
          classes: [
            {
              record_class: 'audit',
              label: 'Audit log rows',
              retention_days: 365,
              cutoff: '2025-09-25T00:00:00Z',
              purgeable: 3,
            },
          ],
        });
      }
      if (
        url.includes('/retention/holds') &&
        method === 'POST' &&
        url.includes('/release')
      ) {
        return json({
          ...HOLD,
          active: false,
          release_reason: 'review closed today',
        });
      }
      if (url.includes('/retention/holds') && method === 'POST') {
        return json(HOLD, 201);
      }
      if (url.includes('/retention/holds'))
        return json(options.holds ?? [HOLD]);
      if (url.includes('/retention/exports')) {
        return new Response(new Uint8Array([1, 2, 3]), {
          status: 200,
          headers: {
            'Content-Type': 'application/gzip',
            'Content-Disposition': 'attachment; filename="period.tar.gz"',
            'X-Preloop-Signature': 'c2ln',
            'X-Preloop-Signing-Key-Id': 'psk_active',
          },
        });
      }
      if (url.includes('/flows/executions')) return json([]);
      if (url.includes('/approval-requests')) return json([]);
      return json({});
    });
  }

  afterEach(() => {
    fetchStub.restore();
    resetConfirmDialogForTests();
    localStorage.clear();
  });

  async function mount() {
    const el = await fixture<RecordsView>(html`<records-view></records-view>`);
    await waitUntil(
      () => el.shadowRoot?.querySelector('[data-testid="chain-status"]'),
      'status did not render'
    );
    return el;
  }

  it('renders chain status and an intact verification', async () => {
    install({});
    const el = await mount();
    const text = el.shadowRoot!.textContent || '';
    expect(text).to.contain('11990');
    expect(text).to.contain('Seal lag 60s');
    expect(text).to.contain('not that they were true when written');
    const button = el.shadowRoot!.querySelector(
      '[data-testid="verify-chain"]'
    ) as HTMLElement;
    button.click();
    await waitUntil(
      () => el.shadowRoot?.querySelector('[data-testid="verify-result"]'),
      'verdict did not render'
    );
    expect(el.shadowRoot!.textContent).to.contain('Intact');
    expect(
      calls.some((call) => call.url.includes('/audit/chain/verify'))
    ).to.equal(true);
  });

  it('renders a broken chain with the first break', async () => {
    install({
      verify: {
        account_id: 'acc',
        status: 'broken',
        checked_rows: 12,
        start_seq: 1,
        end_seq: 40,
        head_seq: 40,
        pruned_below_seq: 0,
        unsealed_rows: 0,
        truncated: false,
        out_of_order_rows: 0,
        first_break: {
          kind: 'hash_mismatch',
          seq: 13,
          row_id: 'row-13',
          detail: 'stored hash does not match',
        },
        checkpoints_verified: 0,
        checkpoint_failures: [],
      },
    });
    const el = await mount();
    (
      el.shadowRoot!.querySelector(
        '[data-testid="verify-chain"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () => el.shadowRoot?.querySelector('[data-testid="first-break"]'),
      'break did not render'
    );
    const text = el.shadowRoot!.textContent || '';
    expect(text).to.contain('Broken');
    expect(text).to.contain('row-13');
    expect(text).to.contain('hash_mismatch');
  });

  it('clamps retention days to the floor before save', async () => {
    install({});
    const el = await mount();
    const input = el.shadowRoot!.querySelector(
      '[data-testid="retention-audit"]'
    ) as HTMLInputElement;
    input.value = '10';
    input.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await el.updateComplete;
    expect(input.value).to.equal('183');
    (
      el.shadowRoot!.querySelector(
        '[data-testid="save-retention"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () => !!document.querySelector('confirm-dialog'),
      'no confirm'
    );
    const confirm = document
      .querySelector('confirm-dialog')!
      .shadowRoot!.querySelector(
        '[data-testid="confirm-dialog-confirm"]'
      ) as HTMLElement;
    confirm.click();
    await waitUntil(
      () => calls.some((call) => call.method === 'PUT'),
      'retention was not saved'
    );
    const put = calls.find((call) => call.method === 'PUT')!;
    expect(put.body).to.contain('"audit":183');
    expect(put.body).to.not.contain('usage');
  });

  it('places a legal hold from an empty list', async () => {
    install({ holds: [] });
    const el = await fixture<RecordsView>(html`<records-view></records-view>`);
    await waitUntil(
      () => el.shadowRoot?.querySelector('[data-testid="holds-empty"]'),
      'empty holds did not render'
    );
    expect(el.shadowRoot!.textContent).to.contain('not a storage guarantee');
    (
      el.shadowRoot!.querySelector(
        '[data-testid="open-place-hold"]'
      ) as HTMLElement
    ).click();
    await el.updateComplete;
    const id = el.shadowRoot!.querySelector(
      '[data-testid="place-resource-id"]'
    ) as HTMLInputElement;
    id.value = HOLD.resource_id;
    id.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    const reason = el.shadowRoot!.querySelector(
      '[data-testid="place-reason"]'
    ) as HTMLInputElement;
    reason.value = 'incident review in progress';
    reason.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    (
      el.shadowRoot!.querySelector(
        '[data-testid="confirm-place-hold"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () =>
        calls.some(
          (call) => call.method === 'POST' && call.url.endsWith('/holds')
        ),
      'hold was not placed'
    );
  });

  it('releases a legal hold after a reason is given', async () => {
    install({ holds: [HOLD] });
    const listed = await fixture<RecordsView>(
      html`<records-view></records-view>`
    );
    await waitUntil(
      () => listed.shadowRoot?.querySelector('[data-testid="release-hold-1"]'),
      'release was not offered'
    );
    (
      listed.shadowRoot!.querySelector(
        '[data-testid="release-hold-1"]'
      ) as HTMLElement
    ).click();
    await listed.updateComplete;
    const releaseReason = listed.shadowRoot!.querySelector(
      '[data-testid="release-reason"]'
    ) as HTMLInputElement;
    releaseReason.value = 'review closed today';
    releaseReason.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    (
      listed.shadowRoot!.querySelector(
        '[data-testid="confirm-release-hold"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () => calls.some((call) => call.url.includes('/release')),
      'hold was not released'
    );
  });

  it('downloads a period export once', async () => {
    install({});
    const click = sinon.stub(HTMLAnchorElement.prototype, 'click');
    const el = await mount();
    (
      el.shadowRoot!.querySelector(
        '[data-testid="export-period"]'
      ) as HTMLElement
    ).click();
    await waitUntil(
      () => el.shadowRoot?.querySelector('[data-testid="export-result"]'),
      'export result did not render'
    );
    expect(el.shadowRoot!.textContent).to.contain('psk_active');
    expect(el.shadowRoot!.textContent).to.contain('preloop evidence verify');
    const exports = calls.filter((call) =>
      call.url.includes('/retention/exports')
    );
    expect(exports).to.have.length(1);
    expect(exports[0].method).to.equal('POST');
    click.restore();
  });
});
