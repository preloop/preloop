import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon from 'sinon';
import './preloop-flow-form';
import type { PreloopFlowForm } from './preloop-flow-form';

describe('Interactive flow settings', () => {
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    sinon.stub(window, 'fetch').resolves(new Response('[]'));
  });
  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    localStorage.clear();
  });
  async function mount(agent: string): Promise<PreloopFlowForm> {
    const el = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form
        .flow=${{ name: 'Repository questions', prompt_template: 'Answer questions', agent_type: agent, agent_config: {} }}
      ></preloop-flow-form>`
    );
    while ((el as any)._loadingReferenceData)
      await new Promise((resolve) => setTimeout(resolve, 10));
    await el.updateComplete;
    return el;
  }
  it('defaults off with documented turn and idle choices and preserves unrelated configuration', async () => {
    const el = await mount('opencode');
    const toggle = el.shadowRoot!.querySelector(
      '[data-field="interactive"]'
    ) as any;
    expect(toggle.checked).to.equal(false);
    expect(toggle.disabled).to.equal(false);
    expect(
      (
        el.shadowRoot!.querySelector(
          '[data-field="interactive-max-turns"]'
        ) as any
      ).value
    ).to.equal('30');
    expect(
      (
        el.shadowRoot!.querySelector(
          '[data-field="interactive-idle-timeout"]'
        ) as any
      ).value
    ).to.equal('600');
    toggle.checked = true;
    toggle.dispatchEvent(new Event('sl-change'));
    await el.updateComplete;
    expect((el as any).flow.agent_config.interactive.enabled).to.equal(true);
    (el as any).setInteractiveSetting('max_turns', 45);
    expect((el as any).flow.agent_config.interactive.max_turns).to.equal(45);
  });
  for (const harness of ['deepseek', 'pi']) {
    it(`disables and explains interactive mode for ${harness}`, async () => {
      const el = await mount(harness);
      expect(
        (el.shadowRoot!.querySelector('[data-field="interactive"]') as any)
          .disabled
      ).to.equal(true);
      expect(
        el.shadowRoot!.querySelector('[data-interactive-settings]')!.textContent
      ).to.contain('require hosted OpenCode');
    });
  }
});
