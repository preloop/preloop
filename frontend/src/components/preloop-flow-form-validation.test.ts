import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

/**
 * Submit-time validation of the flow form: a tracker trigger has to name a
 * tracker and at least one event (it is never quietly saved as a webhook
 * flow), the first invalid field is focused, the banner is announced, and
 * the submit button stays busy until the parent's save settles.
 */

const TRACKER = {
  id: 'tracker-1',
  name: 'Example tracker',
  tracker_type: 'github',
};

describe('PreloopFlowForm submit validation', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url: any) => {
      const target = String(url);
      if (target.includes('/api/v1/trackers')) {
        return new Response(JSON.stringify([TRACKER]));
      }
      if (target.includes('/api/v1/agents')) {
        return new Response(JSON.stringify({ items: [] }));
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  const mount = async (
    flow: Record<string, unknown>
  ): Promise<PreloopFlowForm> => {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${flow}></preloop-flow-form>`
    );
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  };

  const submit = async (element: PreloopFlowForm) => {
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    await element.updateComplete;
    element.removeEventListener('flow-submit', listener);
    return listener;
  };

  const banner = (element: PreloopFlowForm) =>
    element.shadowRoot!.querySelector('[data-form-error]');

  const chooseTrackerTrigger = async (element: PreloopFlowForm) => {
    (element as any).handleTriggerTypeChange('tracker');
    await element.updateComplete;
  };

  it('refuses a tracker trigger with no tracker and focuses the tracker field', async () => {
    const element = await mount({ name: 'Issue triage' });
    await chooseTrackerTrigger(element);

    const listener = await submit(element);

    expect(listener.called, 'flow-submit').to.equal(false);
    expect(banner(element)?.textContent).to.include('Choose a tracker');
    const tracker = element.shadowRoot!.querySelector('[data-field="tracker"]');
    expect(tracker?.hasAttribute('required')).to.equal(true);
    expect(element.shadowRoot!.activeElement).to.equal(tracker);
  });

  it('refuses a tracker trigger with no events and focuses the events field', async () => {
    const element = await mount({ name: 'Issue triage' });
    await chooseTrackerTrigger(element);
    element.flow.trigger_event_source = TRACKER.id;
    element.flow.trigger_event_types = [];
    element.requestUpdate();
    await element.updateComplete;

    const listener = await submit(element);

    expect(listener.called, 'flow-submit').to.equal(false);
    expect(banner(element)?.textContent).to.include(
      'Choose at least one event'
    );
    const events = element.shadowRoot!.querySelector('[data-field="events"]');
    expect(element.shadowRoot!.activeElement).to.equal(events);
  });

  const SAVED_REVIEWER = {
    id: 'flow-1',
    name: 'Pull Request Reviewer',
    trigger_event_source: TRACKER.id,
    trigger_project_ids: ['project-1'],
    trigger_config: { draft: false, state: 'open' },
    agent_type: 'deepseek',
    ai_model_id: 'model-old',
  };

  it('saves a model change on a flow saved with no events', async () => {
    const element = await mount({
      ...SAVED_REVIEWER,
      trigger_event_types: [],
    });
    expect((element as any).triggerType).to.equal('tracker');
    const events = element.shadowRoot!.querySelector<HTMLElement>(
      '[data-field="events"]'
    );
    expect(events?.hasAttribute('required'), 'native required').to.equal(false);
    expect(events?.getAttribute('help-text')).to.include(
      'saved with no events'
    );
    const form = element.shadowRoot!.querySelector('form')!;
    expect(form.checkValidity(), 'native form validity').to.equal(true);

    element.flow.ai_model_id = 'model-new';
    const listener = await submit(element);

    expect(banner(element)).to.equal(null);
    expect(listener.calledOnce, 'flow-submit').to.equal(true);
    const payload = listener.firstCall.args[0].detail.flow;
    expect(payload.ai_model_id).to.equal('model-new');
    expect(payload.trigger_event_source).to.equal(TRACKER.id);
    expect(payload.trigger_event_types).to.deep.equal([]);
  });

  it('saves a flow with events unchanged', async () => {
    const element = await mount({
      ...SAVED_REVIEWER,
      trigger_event_types: ['pull_request_opened', 'pull_request_updated'],
    });
    const events = element.shadowRoot!.querySelector('[data-field="events"]');
    expect(events?.getAttribute('help-text') || '').to.equal('');

    const listener = await submit(element);

    expect(listener.calledOnce).to.equal(true);
    const payload = listener.firstCall.args[0].detail.flow;
    expect(payload.trigger_event_types).to.deep.equal([
      'pull_request_opened',
      'pull_request_updated',
    ]);
  });

  it('refuses clearing every event on a saved flow', async () => {
    const element = await mount({
      ...SAVED_REVIEWER,
      trigger_event_types: ['pull_request_opened'],
    });
    element.flow.trigger_event_types = [];
    element.requestUpdate();
    await element.updateComplete;

    const listener = await submit(element);

    expect(listener.called).to.equal(false);
    expect(banner(element)?.textContent).to.include(
      'Choose at least one event that triggers this flow.'
    );
  });

  it('submits a complete tracker trigger with its own source, never webhook', async () => {
    const element = await mount({ name: 'Issue triage' });
    await chooseTrackerTrigger(element);
    element.flow.trigger_event_source = TRACKER.id;
    element.flow.trigger_event_types = ['issue_created'];

    const listener = await submit(element);

    expect(listener.calledOnce).to.equal(true);
    const payload = listener.firstCall.args[0].detail.flow;
    expect(payload.trigger_event_source).to.equal(TRACKER.id);
    expect(payload.trigger_event_types).to.deep.equal(['issue_created']);
  });

  it('still falls back to webhook values for a webhook trigger', async () => {
    const element = await mount({ name: 'Inbound hook' });

    const listener = await submit(element);

    expect(listener.calledOnce).to.equal(true);
    const payload = listener.firstCall.args[0].detail.flow;
    expect(payload.trigger_event_source).to.equal('webhook');
    expect(payload.trigger_event_types).to.deep.equal(['webhook']);
  });

  it('focuses the name field and announces the banner when the name is missing', async () => {
    const element = await mount({});

    const listener = await submit(element);

    expect(listener.called).to.equal(false);
    const alert = banner(element);
    expect(alert?.getAttribute('role')).to.equal('alert');
    expect(alert?.textContent).to.include('Flow name is required.');
    expect(element.shadowRoot!.activeElement).to.equal(
      element.shadowRoot!.querySelector('[data-field="name"]')
    );
  });

  it('stays busy until the parent save settles and ignores a second submit', async () => {
    const element = await mount({ name: 'Inbound hook' });
    let finishSave!: () => void;
    const save = new Promise<void>((resolve) => {
      finishSave = resolve;
    });
    const listener = sandbox.spy((event: CustomEvent) => {
      event.detail.waitUntil(save);
    });
    element.addEventListener('flow-submit', listener as EventListener);

    const first = (element as any).handleFormSubmit(new Event('submit'));
    await element.updateComplete;
    const submitButton = element.shadowRoot!.querySelector(
      'sl-button[type="submit"]'
    );
    expect((element as any).isSaving).to.equal(true);
    expect(submitButton?.hasAttribute('loading')).to.equal(true);

    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount, 'second submit while saving').to.equal(1);

    finishSave();
    await first;
    await element.updateComplete;
    expect((element as any).isSaving).to.equal(false);
    expect(submitButton?.hasAttribute('loading')).to.equal(false);
  });

  it('labels the trigger type radio group itself', async () => {
    const element = await mount({ name: 'Inbound hook' });
    const group = element.shadowRoot!.querySelector('sl-radio-group');
    expect(group?.getAttribute('label')).to.equal('Trigger type');
  });
});
