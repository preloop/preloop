import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../../components/view-header.ts';
import { invalidateApiCaches } from '../../../api';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';
import './emergency-view';
import type { EmergencyView } from './emergency-view';

describe('EmergencyView', () => {
  let fetchStub: sinon.SinonStub;

  function copy(el: EmergencyView): string {
    return (el.shadowRoot?.textContent ?? '').replace(/\s+/g, ' ').trim();
  }

  function json(data: unknown, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function createFetchStub(status: Record<string, unknown> | null = null) {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();
        if (url.includes('/account/kill-switch/status')) {
          return json(status ?? { active: false, scopes: [] });
        }
        if (url.includes('/account/kill-switch/activate')) {
          return json({
            active: true,
            scopes: [
              { scope: 'gateway', reason: 'Runaway agent' },
              { scope: 'tools', reason: 'Runaway agent' },
              { scope: 'flows', reason: 'Runaway agent' },
            ],
          });
        }
        if (url.includes('/account/kill-switch/deactivate')) {
          return json({ active: false, scopes: [] });
        }
        return json({ detail: `Unhandled: ${method} ${url}` }, 500);
      });
  }

  beforeEach(() => {
    invalidateApiCaches();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
    resetConfirmDialogForTests();
  });

  async function answerConfirm(confirm: boolean): Promise<string> {
    await waitUntil(() =>
      document
        .querySelector('confirm-dialog')
        ?.shadowRoot?.querySelector('sl-dialog[open]')
    );
    const root = document.querySelector('confirm-dialog')!.shadowRoot!;
    const text = (root.textContent ?? '').replace(/\s+/g, ' ');
    const buttons = root.querySelectorAll('sl-button[slot="footer"]');
    (
      (confirm
        ? root.querySelector('[data-testid="confirm-dialog-confirm"]')
        : buttons[0]) as HTMLElement
    ).click();
    return text;
  }

  async function mountLoaded(): Promise<EmergencyView> {
    const element = await fixture<EmergencyView>(
      html`<emergency-view></emergency-view>`
    );
    await waitUntil(
      () => (element as any)._statusState !== 'loading',
      'status read settled'
    );
    await element.updateComplete;
    return element;
  }

  it('shows a loading state, not the halt form, before the status is read', async () => {
    let release: (value: Response) => void = () => {};
    fetchStub = sinon.stub(window, 'fetch').callsFake(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        })
    );
    const element = await fixture<EmergencyView>(
      html`<emergency-view></emergency-view>`
    );
    await element.updateComplete;
    expect(copy(element)).to.contain('Checking whether agent activity');
    expect(copy(element)).to.not.contain('Block new agent requests');
    release(json({ active: false, scopes: [] }));
    await waitUntil(() => copy(element).includes('Block new agent requests'));
  });

  it('offers the halt on a page of its own', async () => {
    fetchStub = createFetchStub();
    const element = await mountLoaded();

    const text = copy(element);
    // One heading: the page title, not a second "Emergency controls" card.
    expect(text).to.not.contain('Emergency Controls');
    expect(text).to.contain('Block new agent requests');
    // Nothing on this page is about a subscription.
    expect(text).to.not.contain('Manage in Stripe');
    expect(
      fetchStub
        .getCalls()
        .some((call) => String(call.args[0]).includes('/api/v1/billing/'))
    ).to.equal(false);
  });

  it('asks before halting and lists every scope', async () => {
    fetchStub = createFetchStub();
    const element = await mountLoaded();
    const halt = [...element.shadowRoot!.querySelectorAll('sl-button')].find(
      (button) => button.textContent?.includes('Block new agent requests')
    ) as HTMLElement;

    halt.click();
    const prompt = await answerConfirm(false);
    expect(prompt).to.contain('Model requests');
    expect(prompt).to.contain('Tool calls');
    expect(prompt).to.contain('Flow executions');
    await element.updateComplete;
    const activations = () =>
      fetchStub
        .getCalls()
        .filter((call) => String(call.args[0]).includes('/activate'));
    expect(activations()).to.have.length(0);

    halt.click();
    await answerConfirm(true);
    await waitUntil(() => activations().length === 1);
    await waitUntil(() => copy(element).includes('Agent activity is halted'));
  });

  it('halts with the recorded reason', async () => {
    fetchStub = createFetchStub();
    const element = await mountLoaded();

    (element as any)._haltReason = 'Runaway agent';
    await (element as any)._handleHalt();
    await element.updateComplete;

    const request = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).includes('/kill-switch/activate'));
    expect(request, 'expected an activation request').to.exist;
    expect(JSON.parse(request!.args[1].body)).to.deep.equal({
      reason: 'Runaway agent',
    });
    expect(copy(element)).to.contain('Agent activity is halted');
  });

  it('includes the operator recovery reason in the deactivation request', async () => {
    fetchStub = createFetchStub({
      active: true,
      scopes: [{ scope: 'flows', reason: 'Inspect active runtimes' }],
    });
    const element = await fixture<EmergencyView>(
      html`<emergency-view></emergency-view>`
    );
    await waitUntil(() => (element as any)._haltStatus !== null, 'status');
    await element.updateComplete;

    expect(
      element.shadowRoot?.querySelectorAll('sl-input[label="Recovery reason"]')
    ).to.have.length(1);
    (element as any)._haltReason = 'Runtime termination verified';
    await (element as any)._handleResume(['flows']);

    const request = fetchStub
      .getCalls()
      .find((call) => String(call.args[0]).includes('/kill-switch/deactivate'));
    expect(request).to.exist;
    expect(JSON.parse(request!.args[1].body)).to.deep.equal({
      scopes: ['flows'],
      reason: 'Runtime termination verified',
    });
  });

  it('says the status is unknown, with a retry, when the first read fails', async () => {
    let fail = true;
    fetchStub = sinon.stub(window, 'fetch').callsFake(async () => {
      if (fail) return new Response('{}', { status: 500 });
      return json({ active: false, scopes: [] });
    });
    const element = await mountLoaded();

    // A failed read must not claim the account is halted, nor that it is
    // not: the "not halted" form stays hidden.
    expect(copy(element)).to.contain("Couldn't read the halt status");
    expect(copy(element)).to.not.contain('Block new agent requests');
    expect(copy(element)).to.not.contain('Agent activity is halted');

    fail = false;
    const retry = [...element.shadowRoot!.querySelectorAll('sl-button')].find(
      (button) => button.textContent?.includes('Retry')
    ) as HTMLElement;
    retry.click();
    await waitUntil(() => copy(element).includes('Block new agent requests'));
  });

  it('keeps the last known state when a later refresh fails', async () => {
    fetchStub = createFetchStub({
      active: true,
      scopes: [{ scope: 'gateway', reason: 'Runaway agent' }],
    });
    const element = await mountLoaded();
    fetchStub.restore();
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async () => new Response('{}', { status: 500 }));
    await (element as any)._refreshHaltStatus();
    await element.updateComplete;
    expect(copy(element)).to.contain('Agent activity is halted');
  });

  it('shows who halted each scope and when', async () => {
    const minutesAgo = new Date(Date.now() - 3 * 60 * 1000).toISOString();
    fetchStub = createFetchStub({
      active: true,
      scopes: [
        {
          scope: 'gateway',
          reason: 'Runaway agent',
          activated_by_username: 'Jane Doe',
          activated_at: minutesAgo,
        },
        {
          scope: 'tools',
          reason: 'Runaway agent',
          activated_by_username: null,
          activated_at: minutesAgo,
        },
      ],
    });
    const element = await mountLoaded();
    const metas = [
      ...element.shadowRoot!.querySelectorAll('.scope-list .scope-meta'),
    ].map((meta) => meta.textContent?.trim());
    expect(metas).to.deep.equal([
      'Halted by Jane Doe · 3m ago',
      'Halted 3m ago',
    ]);
  });
});
