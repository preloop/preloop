import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import './webhooks-view';
import type { WebhooksView } from './webhooks-view';
import { invalidateApiCaches } from '../../../api';

const CATALOGUE = {
  version: '1',
  event_types: [
    {
      name: 'approval.created',
      description: 'An approval request was raised for a tool call.',
    },
    {
      name: 'policy.denied',
      description: 'A policy rule denied a tool call.',
    },
  ],
  signature_header: 'X-Preloop-Signature',
  tolerance_seconds: 300,
  max_attempts: 6,
  retry_delays_seconds: [10, 60, 300, 900, 2400],
};

const ACCOUNT_ENDPOINT = {
  id: '11111111-1111-4111-8111-111111111111',
  url: 'https://siem.example.com/hook',
  description: 'SIEM',
  event_types: ['policy.denied'],
  active: true,
  source: 'account',
  secret_hint: 'ab12',
  created_by_user_id: null,
  consecutive_failures: 0,
  circuit_open: false,
  last_delivery_status: 'delivered',
  last_delivery_at: '2026-09-08T10:00:00Z',
  last_response_code: 200,
  last_error: null,
  created_at: '2026-09-08T09:00:00Z',
};

const SHIM_ENDPOINT = {
  ...ACCOUNT_ENDPOINT,
  id: '22222222-2222-4222-8222-222222222222',
  url: 'https://chat.example.com/approvals',
  description: null,
  event_types: [],
  source: 'approval_workflow',
  last_delivery_status: null,
  last_delivery_at: null,
  last_response_code: null,
};

const DEAD_DELIVERY = {
  id: '33333333-3333-4333-8333-333333333333',
  endpoint_id: ACCOUNT_ENDPOINT.id,
  event_id: '44444444-4444-4444-8444-444444444444',
  event_type: 'policy.denied',
  status: 'dead',
  attempt_count: 6,
  generation: 0,
  occurred_at: '2026-09-08T10:00:00Z',
  next_attempt_at: null,
  delivered_at: null,
  response_status: 500,
  last_error: 'HTTP 500',
  created_at: '2026-09-08T10:00:00Z',
};

describe('WebhooksView', () => {
  let calls: { url: string; method: string; body: string }[];

  function stubFetch(
    options: {
      endpoints?: unknown[];
      deliveries?: unknown[];
      created?: Record<string, unknown>;
    } = {}
  ) {
    calls = [];
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = String(init?.method || 'GET').toUpperCase();
        calls.push({ url, method, body: String(init?.body ?? '') });
        const json = (data: unknown, status = 200) =>
          new Response(JSON.stringify(data), {
            status,
            headers: { 'Content-Type': 'application/json' },
          });
        if (url.includes('/event-webhooks/catalogue')) {
          return json(CATALOGUE);
        }
        if (url.includes('/event-webhooks/endpoints') && method === 'POST') {
          return json(
            options.created ?? {
              ...ACCOUNT_ENDPOINT,
              secret: 'whsec_shown_once',
              secret_note: 'Shown once.',
            },
            201
          );
        }
        if (url.includes('/event-webhooks/endpoints')) {
          return json(options.endpoints ?? [ACCOUNT_ENDPOINT]);
        }
        if (url.includes('/replay')) {
          return json({ event_id: DEAD_DELIVERY.event_id, queued: 1 });
        }
        if (url.includes('/event-webhooks/deliveries')) {
          return json(options.deliveries ?? []);
        }
        return json({ detail: `Unhandled: ${url}` });
      });
  }

  async function mount(): Promise<WebhooksView> {
    const element = (await fixture(
      html`<webhooks-view></webhooks-view>`
    )) as WebhooksView;
    await waitUntil(
      () => !(element as unknown as { loading: boolean }).loading,
      'Webhooks view did not finish loading'
    );
    await element.updateComplete;
    return element;
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    sinon.restore();
    localStorage.clear();
    invalidateApiCaches();
    document.querySelectorAll('sl-alert').forEach((node) => node.remove());
  });

  it('lists endpoints with their filter and state', async () => {
    stubFetch();
    const element = await mount();

    const text = element.shadowRoot?.textContent ?? '';
    expect(text).to.contain('https://siem.example.com/hook');
    expect(text).to.contain('policy.denied');
    expect(text).to.contain('Delivering');
  });

  it('never renders a secret hint as if it were the secret', async () => {
    stubFetch();
    const element = await mount();

    // The row shows state, not credentials: the only place a secret appears
    // is the create dialog, once.
    expect(element.shadowRoot?.textContent).to.not.contain('ab12');
  });

  it('shows the signing secret once, after create', async () => {
    stubFetch();
    const element = await mount();

    (element as unknown as { createUrl: string }).createUrl =
      'https://new.example.com/hook';
    await (
      element as unknown as { handleCreate: () => Promise<void> }
    ).handleCreate();
    await element.updateComplete;

    const dialogs = element.shadowRoot?.querySelectorAll('sl-dialog') ?? [];
    const secretDialog = Array.from(dialogs).find(
      (node) => node.getAttribute('label') === 'Signing secret'
    );
    expect(secretDialog).to.exist;
    expect(secretDialog?.textContent).to.contain('whsec_shown_once');
    expect(secretDialog?.textContent).to.contain('shown once');
  });

  it('renders the event catalogue from the server, not a hard-coded list', async () => {
    stubFetch();
    const element = await mount();
    (element as unknown as { openCreate: () => void }).openCreate();
    await element.updateComplete;

    const checkboxes =
      element.shadowRoot?.querySelectorAll('sl-checkbox') ?? [];
    expect(checkboxes.length).to.equal(CATALOGUE.event_types.length);
    expect(checkboxes[0].textContent).to.contain('approval.created');
    expect(checkboxes[0].textContent).to.contain(
      'An approval request was raised'
    );
  });

  it('offers no edit controls on an approval workflow endpoint', async () => {
    stubFetch({ endpoints: [SHIM_ENDPOINT] });
    const element = await mount();

    const rowText = element.shadowRoot?.textContent ?? '';
    expect(rowText).to.contain('https://chat.example.com/approvals');
    expect(rowText).to.contain('Managed by an approval workflow');
    // Listed so a failing legacy webhook is visible, but not editable here.
    expect(
      element.shadowRoot?.querySelectorAll('sl-button.danger-action')
    ).to.have.length(0);
  });

  it('replays only a dead delivery, by event id', async () => {
    stubFetch({ deliveries: [DEAD_DELIVERY] });
    const element = await mount();

    const buttons = Array.from(
      element.shadowRoot?.querySelectorAll('sl-button') ?? []
    ).filter((node) => node.textContent?.includes('Replay'));
    expect(buttons).to.have.length(1);
    (buttons[0] as HTMLElement).click();
    await waitUntil(() =>
      calls.some(
        (call) => call.url.includes('/replay') && call.method === 'POST'
      )
    );

    const replay = calls.find((call) => call.url.includes('/replay'));
    expect(replay?.url).to.contain(DEAD_DELIVERY.event_id);
  });

  it('says a test event was queued, not delivered', async () => {
    stubFetch();
    const element = await mount();

    await (
      element as unknown as {
        handleTest: (endpoint: unknown) => Promise<void>;
      }
    ).handleTest(ACCOUNT_ENDPOINT);

    const toast = document.querySelector('sl-alert');
    expect(toast?.textContent).to.contain('queued');
    expect(toast?.textContent).to.not.contain('delivered');
  });

  it('empty state names what an endpoint would receive', async () => {
    stubFetch({ endpoints: [] });
    const element = await mount();

    const empty = element.shadowRoot?.querySelector('.empty-state');
    expect(empty?.textContent).to.contain('No endpoints yet');
  });
});
