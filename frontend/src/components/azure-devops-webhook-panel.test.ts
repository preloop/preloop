import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './azure-devops-webhook-panel.ts';
import type { AzureDevOpsWebhookPanel } from './azure-devops-webhook-panel';
import type { AzureDevOpsWebhookStatus } from '../api';

const base: AzureDevOpsWebhookStatus = {
  callback_url:
    'https://preloop.example.com/api/v1/private/webhooks/azure_devops/t1',
  callback_configured: true,
  required_events: ['git.pullrequest.created', 'git.pullrequest.updated'],
  signature: 'missing_secret',
  registration: null,
  instructions: 'Create the subscriptions.',
  header_name: 'X-Preloop-Webhook-Secret',
};

describe('azure-devops-webhook-panel', () => {
  it('shows status, reveals a rotated secret once and registers', async () => {
    const client = {
      getAzureDevOpsWebhookStatus: sinon.stub().resolves(base),
      rotateAzureDevOpsWebhookSecret: sinon
        .stub()
        .resolves({ ...base, signature: 'configured', secret: 's3cret' }),
      registerAzureDevOpsWebhook: sinon.stub().resolves({
        ...base,
        signature: 'configured',
        registration: { status: 'registered', ids: ['a', 'b'] },
      }),
    };
    const el = await fixture<AzureDevOpsWebhookPanel>(
      html`<azure-devops-webhook-panel
        .apiClient=${client}
        trackerId="t1"
      ></azure-devops-webhook-panel>`
    );
    await waitUntil(() => !!el.shadowRoot!.querySelector('.callback-url'));
    const root = el.shadowRoot!;
    expect(client.getAzureDevOpsWebhookStatus).to.have.been.calledWith(
      't1',
      false
    );
    expect(root.querySelector('.callback-url')?.textContent).to.contain(
      '/webhooks/azure_devops/t1'
    );
    expect(root.querySelector('.header-name')?.textContent).to.contain(
      'X-Preloop-Webhook-Secret'
    );
    expect(root.querySelector('.events')?.textContent).to.contain(
      'git.pullrequest.updated'
    );
    expect(root.querySelector<any>('sl-button.register').disabled).to.be.true;

    root.querySelector<HTMLElement>('sl-button.rotate')!.click();
    await waitUntil(() => !!root.querySelector('.secret'));
    expect(root.querySelector('.secret')?.textContent).to.contain('s3cret');
    await el.updateComplete;

    root.querySelector<HTMLElement>('sl-button.register')!.click();
    await waitUntil(() =>
      root.querySelector('.registration')?.textContent?.includes('registered')
    );
    expect(root.querySelector('.registration')?.textContent).to.contain(
      '2 subscriptions'
    );
  });
});

it('explains an ignored repository delivery', async () => {
  const client = {
    getAzureDevOpsWebhookStatus: sinon
      .stub()
      .resolves({ ...base, last_ignored_reason: 'repository_not_imported' }),
    rotateAzureDevOpsWebhookSecret: sinon.stub(),
    registerAzureDevOpsWebhook: sinon.stub(),
  };
  const el = await fixture<AzureDevOpsWebhookPanel>(
    html`<azure-devops-webhook-panel
      .apiClient=${client}
      trackerId="t1"
    ></azure-devops-webhook-panel>`
  );
  await waitUntil(() => !!el.shadowRoot!.querySelector('.last-ignored'));
  expect(el.shadowRoot!.querySelector('.last-ignored')!.textContent).to.contain(
    'repository not imported'
  );
});
