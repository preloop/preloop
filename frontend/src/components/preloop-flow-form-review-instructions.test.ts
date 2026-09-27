import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import type SlTextarea from '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';
import {
  REVIEW_INSTRUCTIONS_PROMPT_CAP,
  REVIEW_POLICY_DOCS_URL,
} from './preloop-flow-form';

const reviewerPreset = {
  id: 'preset-reviewer',
  name: 'Pull Request Reviewer',
  slug: 'pull-request-reviewer',
  prompt_template: 'Review the change.\n{{flow.review_instructions}}\n',
  agent_type: 'codex',
  trigger_event_types: ['pull_request_opened'],
};

const namedReviewerPreset = {
  id: 'preset-named-reviewer',
  name: 'Pull Request Reviewer',
  prompt_template: 'Review the change without the placeholder.',
  agent_type: 'codex',
  trigger_event_types: ['pull_request_opened'],
};

const otherPreset = {
  id: 'preset-other',
  name: 'Implementation',
  slug: 'automated-issue-implementation',
  prompt_template: 'Implement the issue.',
  agent_type: 'codex',
  trigger_event_types: ['webhook'],
};

describe('PreloopFlowForm review instructions', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url) => {
      const path = String(url);
      const presets = path.includes('/api/v1/flows/presets')
        ? [reviewerPreset, namedReviewerPreset, otherPreset]
        : [];
      return new Response(JSON.stringify(presets));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  async function mount(
    flow: Record<string, unknown> = {}
  ): Promise<PreloopFlowForm> {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${flow}></preloop-flow-form>`
    );
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  }

  function field(element: PreloopFlowForm): HTMLElement | null {
    return element.shadowRoot!.querySelector('[data-review-instructions]');
  }

  function textarea(element: PreloopFlowForm): SlTextarea {
    const control = element.shadowRoot!.querySelector<SlTextarea>(
      '[data-review-instructions-input]'
    );
    expect(control, 'Review instructions control').to.exist;
    return control!;
  }

  async function submit(
    element: PreloopFlowForm
  ): Promise<Record<string, unknown>> {
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(1);
    return listener.firstCall.args[0].detail.flow;
  }

  it('hides the field when the flow is not a reviewer and the prompt does not reference it', async () => {
    const element = await mount({
      name: 'Implementation',
      prompt_template: 'Implement the issue.',
      slug: 'automated-issue-implementation',
    });
    expect(field(element)).to.equal(null);
    expect('review_instructions' in (await submit(element))).to.equal(false);
  });

  it('shows the field when the preset slug is pull-request-reviewer', async () => {
    const element = await mount({
      name: 'Reviewer',
      slug: 'pull-request-reviewer',
      prompt_template: 'Review the change.',
      review_instructions: 'Keep the declared runtime.',
    });
    expect(field(element)).to.exist;
    expect(textarea(element).value).to.equal('Keep the declared runtime.');
    const control = textarea(element);
    const help =
      (control as unknown as { helpText?: string }).helpText ||
      control.getAttribute('help-text') ||
      '';
    const capLabel = REVIEW_INSTRUCTIONS_PROMPT_CAP.toLocaleString('en-US');
    expect(help).to.include('Blocking policy');
    expect(help).to.include(
      `${REVIEW_INSTRUCTIONS_PROMPT_CAP / 1024} KiB (${capLabel} characters)`
    );
    const docs = element.shadowRoot!.querySelector(
      '[data-review-instructions-docs]'
    );
    expect(docs?.getAttribute('href')).to.equal(REVIEW_POLICY_DOCS_URL);
    expect(docs?.textContent).to.include('.preloop/review-policy.md');
  });

  it('shows the field when the prompt references flow.review_instructions', async () => {
    const element = await mount({
      name: 'Custom review',
      slug: 'custom-review',
      prompt_template:
        'Follow {{flow.review_instructions|truncate(16384)}} strictly.',
    });
    expect(field(element)).to.exist;
    expect(textarea(element).value).to.equal('');
  });

  it('shows the field for an account flow whose source preset is the reviewer', async () => {
    const element = await mount({
      name: 'Reviewer copy',
      source_preset_id: 'preset-named-reviewer',
      prompt_template: 'The placeholder was removed from this copy.',
    });
    expect(field(element)).to.exist;
  });

  it('shows the field after the reviewer preset is selected', async () => {
    const element = await mount({ name: 'New flow' });
    expect(field(element)).to.equal(null);
    await (element as any).applyPresetSelection(reviewerPreset.id);
    await element.updateComplete;
    expect(field(element)).to.exist;
  });

  it('saves the current value through the flow update payload', async () => {
    const element = await mount({
      id: 'flow-1',
      name: 'Reviewer',
      slug: 'pull-request-reviewer',
      prompt_template: 'Review.',
      review_instructions: 'Keep the declared runtime.',
    });
    const control = textarea(element);
    control.value = 'Prefer the repository policy file.';
    control.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await element.updateComplete;
    expect((await submit(element)).review_instructions).to.equal(
      'Prefer the repository policy file.'
    );
  });

  it('clears a saved value with null', async () => {
    const element = await mount({
      id: 'flow-1',
      name: 'Reviewer',
      slug: 'pull-request-reviewer',
      prompt_template: 'Review.',
      review_instructions: 'Keep the declared runtime.',
    });
    const clear = element.shadowRoot!.querySelector<HTMLElement>(
      '[data-review-instructions-clear]'
    );
    expect(clear).to.exist;
    clear!.click();
    await element.updateComplete;
    expect(textarea(element).value).to.equal('');
    expect((await submit(element)).review_instructions).to.equal(null);
  });

  it('warns when the text is longer than the 16 KiB prompt cap', async () => {
    const element = await mount({
      name: 'Reviewer',
      slug: 'pull-request-reviewer',
      prompt_template: 'Review.',
      review_instructions: 'x'.repeat(REVIEW_INSTRUCTIONS_PROMPT_CAP),
    });
    expect(
      element.shadowRoot!.querySelector('[data-review-instructions-cap]')
    ).to.equal(null);

    element.flow = {
      ...element.flow,
      review_instructions: 'x'.repeat(REVIEW_INSTRUCTIONS_PROMPT_CAP + 1),
    };
    await element.updateComplete;
    const warning = element.shadowRoot!.querySelector(
      '[data-review-instructions-cap]'
    );
    expect(warning).to.exist;
    const capLabel = REVIEW_INSTRUCTIONS_PROMPT_CAP.toLocaleString('en-US');
    expect(warning!.textContent).to.include(
      `${REVIEW_INSTRUCTIONS_PROMPT_CAP / 1024} KiB`
    );
    expect(warning!.textContent).to.include(capLabel);
  });
});
