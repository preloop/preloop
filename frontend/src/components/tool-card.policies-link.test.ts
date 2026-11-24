import { html, fixture, expect } from '@open-wc/testing';

import './tool-card';
import type { ToolCard } from './tool-card';
import sinon from 'sinon';

describe('tool-card approval dialog', () => {
  it('sends "Manage in Policies" to the Policies page', async () => {
    const element = await fixture<ToolCard>(
      html`<tool-card
        .tool=${{ name: 'search', description: 'Search', source: 'builtin' }}
      ></tool-card>`
    );
    (element as unknown as { showPreloopDialog: boolean }).showPreloopDialog =
      true;
    await element.updateComplete;
    const link = [
      ...element.shadowRoot!.querySelectorAll('sl-button[href]'),
    ].find((button) => button.textContent!.includes('Manage in'));
    expect(link?.getAttribute('href')).to.equal('/console/policies');
    expect(link?.textContent!.trim()).to.equal('Manage in Policies');
    expect(
      element.shadowRoot!.querySelector('[href="/console/governance"]')
    ).to.equal(null);
  });
});

describe('tool-card Teams workflow', () => {
  it('requires and submits the Teams webhook URL in OSS mode', async () => {
    const element = await fixture<ToolCard>(
      html`<tool-card
        .tool=${{ name: 'search', description: 'Search', source: 'builtin' }}
        .features=${{}}
      ></tool-card>`
    );
    const state = element as unknown as {
      showPreloopDialog: boolean;
      isCreatingPolicy: boolean;
      newPolicyName: string;
      handleConfirmPolicy: () => void;
    };
    state.showPreloopDialog = true;
    state.isCreatingPolicy = true;
    state.newPolicyName = 'Teams notices';
    await element.updateComplete;
    expect(element.shadowRoot!.querySelector('sl-radio[value="teams"]')).to
      .exist;
    const type = element.shadowRoot!.querySelector(
      'sl-radio-group[aria-label="Approval type"]'
    ) as HTMLElement & { value: string };
    type.value = 'teams';
    type.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await element.updateComplete;
    const webhook = element.shadowRoot!.querySelector(
      'sl-input[aria-label="Webhook URL"]'
    ) as HTMLElement & { value: string };
    expect(webhook).to.exist;
    expect(
      element.shadowRoot!.querySelector('[aria-label="Approval channel"]')
    ).to.equal(null);
    const created = sinon.spy();
    element.addEventListener('create-policy', created);
    const alertStub = sinon.stub(window, 'alert');
    try {
      state.handleConfirmPolicy();
      expect(alertStub.calledOnceWith('Webhook URL is required')).to.equal(
        true
      );
      expect(created.called).to.equal(false);
      webhook.value = 'https://example.com/workflows/test';
      webhook.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
      state.handleConfirmPolicy();
      expect(created.calledOnce).to.equal(true);
      expect(created.firstCall.args[0].detail.policy).to.include({
        approval_type: 'teams',
        name: 'Teams notices',
        channel: null,
      });
      expect(
        created.firstCall.args[0].detail.policy.approval_config
      ).to.deep.equal({
        webhook_url: 'https://example.com/workflows/test',
      });
    } finally {
      alertStub.restore();
    }
  });
});
