import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './approval-readiness';
import type { ApprovalReadiness } from './approval-readiness';

const route = {
  workflow_name: 'Tool review',
  recipients: ['Jane Doe'],
  deciders: ['Jane Doe'],
  destinations: [],
  issues: [
    "Only the agent's owner receives requests, and no notification destination is available.",
  ],
  timeout_seconds: 300,
  ai_fallback: null,
  external: false,
};

describe('Approval readiness', () => {
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
  });
  it('shows the owner notification gap and links to the fixes', async () => {
    localStorage.setItem('accessToken', 'test-token');
    const fetch = sinon
      .stub(window, 'fetch')
      .resolves(new Response(JSON.stringify(route)));
    const el = await fixture<ApprovalReadiness>(
      html`<approval-readiness
        compact
        .agentId=${'agent-1'}
      ></approval-readiness>`
    );
    await waitUntil(() => !!el.shadowRoot?.querySelector('section'));
    const disclosure = el.shadowRoot!.querySelector('details')!;
    expect(disclosure.open).to.equal(false);
    expect(disclosure.querySelector('summary')!.textContent).to.contain(
      'needs attention'
    );
    disclosure.querySelector('summary')!.click();
    expect(disclosure.open).to.equal(true);
    expect(el.shadowRoot!.textContent).to.include("Only the agent's owner");
    expect(el.shadowRoot!.textContent!.replace(/\s+/g, ' ')).to.include(
      'Who can decide in the console: Jane Doe'
    );
    expect(
      el.shadowRoot!.querySelector(
        'a[href="/console/settings/notification-preferences"]'
      )
    ).to.exist;
    expect(String(fetch.firstCall.args[0])).to.include('agent_id=agent-1');
  });
  it('hides the warning in Off mode without checking or changing enforcement', async () => {
    const fetch = sinon.stub(window, 'fetch');
    const el = await fixture<ApprovalReadiness>(
      html`<approval-readiness .enforced=${false}></approval-readiness>`
    );
    expect(el.shadowRoot!.textContent?.trim()).to.equal('');
    expect(fetch.called).to.equal(false);
  });
  it('uses the stored deadline and request workflow for a waiting request', async () => {
    localStorage.setItem('accessToken', 'test-token');
    const fetch = sinon
      .stub(window, 'fetch')
      .resolves(new Response(JSON.stringify({ ...route, issues: [] })));
    const el = await fixture<ApprovalReadiness>(
      html`<approval-readiness
        .requestId=${'request-1'}
        .expiresAt=${'2026-11-26T12:07:15Z'}
      ></approval-readiness>`
    );
    await waitUntil(() => !!el.shadowRoot?.querySelector('section'));
    expect(String(fetch.firstCall.args[0])).to.include(
      '/approval-requests/request-1/readiness'
    );
    expect(el.shadowRoot!.textContent).to.include('Waiting in');
    expect(el.shadowRoot!.textContent).to.include('Deadline:');
    expect(el.shadowRoot!.textContent).not.to.include('300 seconds');
  });
  it('reports a failed readiness read and offers retry', async () => {
    localStorage.setItem('accessToken', 'test-token');
    sinon.stub(window, 'fetch').resolves(new Response('{}', { status: 500 }));
    const el = await fixture<ApprovalReadiness>(
      html`<approval-readiness></approval-readiness>`
    );
    await waitUntil(() =>
      el.shadowRoot!.textContent!.includes('could not be checked')
    );
    expect(el.shadowRoot!.querySelector('button')!.textContent).to.include(
      'Retry'
    );
  });
});
