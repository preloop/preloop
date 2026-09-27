import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './shared-resource-view';
import type { SharedResourceView } from './shared-resource-view';
import {
  mockApi,
  signInForTest,
  type MockApi,
} from '../../../test-helpers/capability-api';

const PATH = '/api/v1/accounts/acc-root/shared-resources/ai_model';

async function open(id: string) {
  const el = await fixture<SharedResourceView>(
    html`<shared-resource-view></shared-resource-view>`
  );
  el.onBeforeEnter({ params: { kind: 'ai_model', resourceId: id } });
  await waitUntil(() => !el.shadowRoot!.querySelector('sl-spinner'));
  await el.updateComplete;
  return el;
}

const CREDENTIAL_SELECTOR = [
  'input[type="password"]',
  'sl-input[type="password"]',
  'sl-input',
  'input',
  'textarea',
  'sl-textarea',
  'sl-button',
].join(',');

describe('shared-resource-view', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
  });

  it('shows the shared model read-only and never a credential, even if the server sends one', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        {
          path: `${PATH}/model-1`,
          body: {
            kind: 'ai_model',
            id: 'model-1',
            name: 'Claude house model',
            provider: 'anthropic',
            identifier: 'claude-x',
            api_key: 'sk-leaked-secret',
            secret: 'also-secret',
            token: 'tok-secret',
            credentials: { api_key: 'nested-secret' },
            shared_from: {
              account_id: 'acc-parent',
              account_name: 'Parent Co',
            },
          },
        },
      ],
    });
    const el = await open('model-1');
    const text = el.shadowRoot!.textContent!;
    expect(text).to.contain('Claude house model');
    expect(text).to.contain('anthropic');
    expect(text).to.contain('Parent Co');
    for (const secret of [
      'sk-leaked-secret',
      'also-secret',
      'tok-secret',
      'nested-secret',
    ]) {
      expect(text).not.to.contain(secret);
    }
    expect(el.shadowRoot!.querySelectorAll(CREDENTIAL_SELECTOR)).to.have.length(
      0
    );
    expect(text.toLowerCase()).not.to.contain('api key');
  });

  it('shows not found for an id the server does not share with this account', async () => {
    // A sibling's model: the server answers 404 and nothing else.
    api = mockApi({ capabilities: ['account_hierarchy'] });
    const el = await open('model-of-sibling');
    expect(api.callsTo(`${PATH}/model-of-sibling`)).to.have.length(1);
    expect(el.shadowRoot!.querySelector('[data-testid="shared-not-found"]')).to
      .exist;
    expect(el.shadowRoot!.querySelector('[data-testid="shared-fields"]')).to.be
      .null;
  });

  it('shows not found when the answer is about a different resource', async () => {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        {
          path: `${PATH}/model-1`,
          body: {
            kind: 'ai_model',
            id: 'model-2',
            name: 'Someone else',
            shared_from: { account_id: 'acc-sib', account_name: 'Sibling' },
          },
        },
      ],
    });
    const el = await open('model-1');
    expect(el.shadowRoot!.textContent).not.to.contain('Someone else');
    expect(el.shadowRoot!.textContent).not.to.contain('Sibling');
    expect(el.shadowRoot!.querySelector('[data-testid="shared-not-found"]')).to
      .exist;
  });
});
