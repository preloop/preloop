import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon from 'sinon';
import './preloop-flow-form';
import type { PreloopFlowForm } from './preloop-flow-form';

describe('Flow chat opt-in', () => {
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    sinon.stub(window, 'fetch').resolves(new Response('[]'));
  });
  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    localStorage.clear();
  });
  async function mount(alias: string | null = null): Promise<PreloopFlowForm> {
    const el = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form
        .flow=${{
          name: 'Document answers',
          prompt_template: 'Answer the question',
          chat_alias: alias,
        }}
      ></preloop-flow-form>`
    );
    while ((el as any)._loadingReferenceData)
      await new Promise((resolve) => setTimeout(resolve, 10));
    await el.updateComplete;
    const details = el.shadowRoot!.querySelector('[data-chat-trigger]') as any;
    expect(details.open).to.equal(false);
    details.open = true;
    await details.updateComplete;
    return el;
  }
  async function submit(el: PreloopFlowForm): Promise<any> {
    const listener = sinon.spy();
    el.addEventListener('flow-submit', listener);
    await (el as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(1);
    return listener.firstCall.args[0].detail.flow;
  }
  it('defaults off and saves the explicitly chosen alias', async () => {
    const el = await mount();
    const input = el.shadowRoot!.querySelector(
      '[data-field="chat_alias"]'
    ) as any;
    expect(input.value).to.equal('');
    expect((await submit(el)).chat_alias).to.equal(null);
    input.value = 'docs';
    input.dispatchEvent(new Event('sl-input'));
    expect((await submit(el)).chat_alias).to.equal('docs');
  });
  it('clears an existing alias with an explicit null', async () => {
    const el = await mount('docs');
    const input = el.shadowRoot!.querySelector(
      '[data-field="chat_alias"]'
    ) as any;
    expect(input.value).to.equal('docs');
    input.value = '';
    input.dispatchEvent(new Event('sl-input'));
    expect((await submit(el)).chat_alias).to.equal(null);
  });
});
