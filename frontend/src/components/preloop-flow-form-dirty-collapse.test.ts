import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon from 'sinon';
import './preloop-flow-form';
import type { PreloopFlowForm } from './preloop-flow-form';
import { resetConfirmDialogForTests } from './confirm-dialog';

describe('Flow form advanced sections and unsaved changes', () => {
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    sinon
      .stub(window, 'fetch')
      .callsFake(async () => new Response(JSON.stringify([])));
  });
  afterEach(() => {
    fixtureCleanup();
    sinon.restore();
    localStorage.clear();
    sessionStorage.clear();
    resetConfirmDialogForTests();
  });

  async function mount(flow: Record<string, unknown> = {}) {
    const el = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${flow}></preloop-flow-form>`
    );
    while ((el as any)._loadingReferenceData)
      await new Promise((resolve) => setTimeout(resolve, 10));
    await el.updateComplete;
    return el;
  }

  it('keeps core controls open and advanced defaults collapsed with sticky actions', async () => {
    const el = await mount({ name: 'Example flow' });
    for (const details of el.shadowRoot!.querySelectorAll(
      'sl-details[data-advanced]'
    ))
      expect((details as any).open).to.equal(false);
    expect(
      el.shadowRoot!.querySelector('sl-textarea.prompt')!.closest('sl-details')
    ).to.equal(null);
    expect(
      el.shadowRoot!.querySelector('[data-field="name"]')!.closest('sl-details')
    ).to.equal(null);
    expect(
      getComputedStyle(el.shadowRoot!.querySelector('.form-actions')!).position
    ).to.equal('sticky');
    expect(el.isDirty).to.equal(false);
  });

  it('opens non-default sections and opens/focuses a collapsed invalid limit', async () => {
    const el = await mount({
      name: 'Example flow',
      max_budget: 5,
      max_iterations: 40,
      git_clone_config: { enabled: true },
    });
    const limits = el.shadowRoot!.querySelector(
      'sl-details[data-advanced="limits"]'
    ) as any;
    expect(limits.open).to.equal(true);
    expect(limits.summary).to.include('$5 per run, 40 model calls');
    expect(
      (el.shadowRoot!.querySelector('sl-details[data-advanced="git"]') as any)
        .open
    ).to.equal(true);
    limits.open = false;
    (el as any).flow.timeout_seconds = 1;
    await (el as any).handleFormSubmit(new Event('submit'));
    expect(limits.open).to.equal(true);
    const input = el.shadowRoot!.querySelector(
      'sl-input[name="timeout_seconds"]'
    ) as any;
    expect(el.shadowRoot!.activeElement).to.equal(input);
  });

  it('guards unload only while dirty and clears the guard after save or OAuth start', async () => {
    const el = await mount({ name: 'Example flow' });
    expect(el.isDirty).to.equal(false);
    (el as any).flow.name = 'Changed flow';
    el.requestUpdate();
    await el.updateComplete;
    expect(el.isDirty).to.equal(true);
    let unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).to.equal(true);
    el.markSaved();
    unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).to.equal(false);
    (el as any).flow.name = 'More changes';
    el.requestUpdate();
    await el.updateComplete;
    el.dispatchEvent(new CustomEvent('github-oauth-starting'));
    unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).to.equal(false);
    expect(sessionStorage.getItem('preloop_flow_form_state')).to.include(
      'More changes'
    );
  });

  it('asks before dirty Cancel and keeps edits when the user stays', async () => {
    const el = await mount({ name: 'Example flow' });
    (el as any).flow.name = 'Changed flow';
    const cancel = sinon.spy();
    el.addEventListener('flow-cancel', cancel);
    const confirmation = (el as any).handleCancel();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const dialog = document.querySelector('confirm-dialog') as any;
    expect(dialog).to.exist;
    // The shared confirm component exposes standard Cancel/Discard actions.
    const buttons = Array.from(
      dialog.shadowRoot.querySelectorAll('sl-button')
    ) as any[];
    buttons.find((button) => button.textContent.trim() === 'Cancel').click();
    await confirmation;
    expect(cancel.called).to.equal(false);
    expect(el.isDirty).to.equal(true);
  });
  it('clears dirty state after a successful save and retains edits on rejection', async () => {
    const el = await mount({ name: 'Example flow' });
    (el as any).flow.name = 'Changed flow';
    const listener = (event: Event) =>
      (event as CustomEvent).detail.waitUntil(Promise.resolve());
    el.addEventListener('flow-submit', listener);
    await (el as any).handleFormSubmit(new Event('submit'));
    expect(el.isDirty).to.equal(false);
    el.removeEventListener('flow-submit', listener);
    (el as any).flow.name = 'Rejected changes';
    el.addEventListener('flow-submit', (event: Event) =>
      (event as CustomEvent).detail.waitUntil(
        Promise.reject(new Error('Save failed'))
      )
    );
    await (el as any).handleFormSubmit(new Event('submit'));
    expect(el.isDirty).to.equal(true);
  });

  it('does not mark edits made during an in-flight save as persisted', async () => {
    const el = await mount({ name: 'Example flow' });
    (el as any).flow.name = 'Submitted name';
    let finish!: () => void;
    el.addEventListener('flow-submit', (event: Event) =>
      (event as CustomEvent).detail.waitUntil(
        new Promise<void>((resolve) => {
          finish = resolve;
        })
      )
    );
    const save = (el as any).handleFormSubmit(new Event('submit'));
    (el as any).flow.name = 'New unsaved name';
    finish();
    await save;
    expect(el.isDirty).to.equal(true);
  });
  it('keeps unsaved routing and label edits across OAuth restoration', async () => {
    const el = await mount({ name: 'Example flow' });
    (el as any).routingRules = [
      {
        id: 'route-example',
        anyLabels: 'synthetic',
        allLabels: '',
        ai_model_id: 'model-example',
        agent_type: '',
      },
    ];
    (el as any).labelRules = [
      {
        label: 'synthetic',
        ai_model_id: 'model-example',
        reasoning_effort: '',
      },
    ];
    el.dispatchEvent(new CustomEvent('github-oauth-starting'));
    const stored = JSON.parse(
      sessionStorage.getItem('preloop_flow_form_state')!
    );
    expect(stored.routingRules).to.deep.equal((el as any).routingRules);
    expect(stored.labelRules).to.deep.equal((el as any).labelRules);
    el.remove();
    const restored = await mount();
    expect((restored as any).routingRules).to.deep.equal(stored.routingRules);
    expect((restored as any).labelRules).to.deep.equal(stored.labelRules);
    expect(restored.isDirty).to.equal(true);
  });

  it('keeps dirty guards when OAuth authorization URL lookup fails', async () => {
    const el = await mount({ name: 'Example flow' });
    (el as any).flow.name = 'Unsaved name';
    el.requestUpdate();
    await el.updateComplete;
    const modal = document.createElement('add-tracker-modal') as any;
    el.appendChild(modal);
    modal._api = {
      ...modal._api,
      getGitHubAuthUrl: sinon.stub().rejects(new Error('OAuth unavailable')),
    };
    await modal.startGitHubOAuth();
    expect(el.isDirty).to.equal(true);
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).to.equal(true);
    expect(modal.authMethod).to.equal('api_token');
  });
});
