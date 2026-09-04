import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  oneEvent,
} from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

/**
 * Notifications section on the Create/Edit Flow form.
 *
 * Source guards keep the two comment toggles wired to flow.notifications. The
 * behaviour block mounts the form and checks the submitted payload.
 */

let source: string;

before(async () => {
  const res = await fetch(
    new URL('./preloop-flow-form.ts', import.meta.url).href
  );
  expect(res.ok).to.be.true;
  source = await res.text();
});

describe('PreloopFlowForm notifications section', () => {
  it('renders a Notifications card', () => {
    expect(source).to.include('Notifications');
    expect(source).to.include('name="bell"');
  });

  it('wires the two notification toggles', () => {
    expect(source).to.include('data-notification="on_failure_comment"');
    expect(source).to.include('data-notification="on_success_comment"');
    expect(source).to.not.include('data-notification="on_failure_attention"');
    expect(source).to.include(
      'Comment on the triggering issue when this flow fails'
    );
    expect(source).to.include('Failed executions always');
    expect(source).to.include(
      'Comment on the triggering issue when a pull request is opened'
    );
  });

  it('includes notifications in the submit payload', () => {
    expect(source).to.include('notifications:');
    expect(source).to.include('defaultFlowNotifications()');
  });
});

function sampleFlow(notifications?: Record<string, unknown>) {
  return {
    id: 'flow-1',
    name: 'Issue fixer',
    prompt_template: 'fix it',
    agent_type: 'codex',
    trigger_event_source: 'webhook',
    trigger_event_types: ['webhook'],
    ...(notifications ? { notifications } : {}),
  };
}

describe('PreloopFlowForm notifications behaviour', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async () => {
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
  });

  const mount = async (flow: Record<string, unknown>) => {
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
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    return event.detail.flow;
  };

  const checkbox = (element: PreloopFlowForm, name: string) =>
    element.shadowRoot!.querySelector(
      `sl-checkbox[data-notification="${name}"]`
    ) as HTMLInputElement & { checked: boolean };

  it('defaults both toggles off and submits that shape', async () => {
    const element = await mount(sampleFlow());
    expect(checkbox(element, 'on_failure_comment').checked).to.equal(false);
    expect(checkbox(element, 'on_success_comment').checked).to.equal(false);

    const payload = await submit(element);
    expect(payload.notifications).to.deep.equal({
      on_failure: {
        comment_on_trigger_issue: false,
        attention_item: false,
      },
      on_success: {
        comment_on_trigger_issue: false,
      },
    });
  });

  it('renders saved notification flags and submits them unchanged', async () => {
    const saved = {
      on_failure: {
        comment_on_trigger_issue: true,
        attention_item: false,
      },
      on_success: {
        comment_on_trigger_issue: true,
      },
    };
    const element = await mount(sampleFlow(saved));
    expect(checkbox(element, 'on_failure_comment').checked).to.equal(true);
    expect(checkbox(element, 'on_success_comment').checked).to.equal(true);

    const payload = await submit(element);
    expect(payload.notifications).to.deep.equal(saved);
  });

  it('toggles a checkbox into the submit payload', async () => {
    const element = await mount(sampleFlow());
    const failureComment = checkbox(element, 'on_failure_comment');
    failureComment.checked = true;
    failureComment.dispatchEvent(
      new CustomEvent('sl-change', { bubbles: true })
    );
    await element.updateComplete;

    const payload = await submit(element);
    expect(payload.notifications.on_failure.comment_on_trigger_issue).to.equal(
      true
    );
  });
});
