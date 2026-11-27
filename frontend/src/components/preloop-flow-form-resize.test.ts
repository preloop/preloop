import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './preloop-flow-form';
import type { PreloopFlowForm } from './preloop-flow-form';
import type SlTextarea from '@shoelace-style/shoelace/dist/components/textarea/textarea.js';

describe('flow prompt manual size', () => {
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
  });

  it('keeps the resized height through input, blur and a form refresh', async () => {
    localStorage.setItem('accessToken', 'test-token');
    sinon.stub(window, 'fetch').callsFake(async () => new Response('[]'));
    const form = await fixture<PreloopFlowForm>(html`
      <preloop-flow-form
        .flow=${{ name: 'Example flow', prompt_template: 'Original prompt' }}
      ></preloop-flow-form>
    `);
    await waitUntil(() => !(form as any)._loadingReferenceData);
    await form.updateComplete;
    const control = form.shadowRoot!.querySelector(
      'sl-textarea.prompt'
    ) as SlTextarea;
    await control.updateComplete;
    const textarea = control.shadowRoot!.querySelector('textarea')!;
    // The browser's resize handle sets the native textarea's inline height.
    textarea.style.height = '420px';
    await new Promise(requestAnimationFrame);
    textarea.value = 'Edited prompt';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    await control.updateComplete;
    await form.updateComplete;
    await new Promise(requestAnimationFrame);
    expect(textarea.getBoundingClientRect().height).to.equal(420);
    textarea.dispatchEvent(new Event('change', { bubbles: true }));
    await control.updateComplete;
    form.flow = { ...form.flow, prompt_template: 'Refreshed prompt' };
    await form.updateComplete;
    await control.updateComplete;
    await new Promise(requestAnimationFrame);
    expect(textarea.getBoundingClientRect().height).to.equal(420);
    expect(textarea.value).to.equal('Refreshed prompt');
    textarea.style.height = '210px';
    textarea.dispatchEvent(new Event('change', { bubbles: true }));
    form.requestUpdate();
    await form.updateComplete;
    await new Promise(requestAnimationFrame);
    expect(textarea.getBoundingClientRect().height).to.equal(210);
  });
});
