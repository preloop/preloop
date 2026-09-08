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
 * PR-dependent sections on the Create/Edit Flow form.
 *
 * Two sections only apply to a flow that opens a pull request itself:
 * "PR review and CI follow-up" and the issue comment when a PR is opened, the
 * second one also needing a trigger that is about an issue. The matrix below
 * mounts every combination of (create PR off/on) x (issue / PR / manual
 * trigger) and asserts what renders, the hint under the precondition, and the
 * submitted payload.
 */

let source: string;
/** Same source with runs of whitespace collapsed, for copy that wraps. */
let copy: string;

before(async () => {
  const res = await fetch(
    new URL('./preloop-flow-form.ts', import.meta.url).href
  );
  expect(res.ok).to.be.true;
  source = await res.text();
  copy = source.replace(/\s+/g, ' ');
});

describe('PreloopFlowForm notifications section', () => {
  it('keeps one notification toggle, tied to the success comment', () => {
    expect(source).to.include('data-notification="on_success_comment"');
    expect(source).to.not.include('data-notification="on_failure_comment"');
    expect(source).to.not.include('data-notification="on_failure_attention"');
    expect(copy).to.not.include(
      'Comment on the triggering issue when this flow fails'
    );
    expect(copy).to.include('Failed executions always appear on Overview.');
    expect(source).to.include('name="bell"');
  });

  it('never submits a failure notification block', () => {
    expect(source).to.include('composedNotifications()');
    expect(source).to.not.include('on_failure:');
  });
});

type TriggerKind = 'issue' | 'pull_request' | 'manual';

const TRIGGERS: Record<TriggerKind, Record<string, unknown>> = {
  // A tracker trigger. The form derives triggerType 'tracker' from a
  // trigger_event_source that is neither 'webhook' nor 'schedule'.
  issue: {
    trigger_event_source: 'tracker-1',
    trigger_event_types: ['issue_opened', 'issue_labeled'],
  },
  pull_request: {
    trigger_event_source: 'tracker-1',
    trigger_event_types: ['pull_request_opened'],
  },
  manual: {
    trigger_event_source: 'webhook',
    trigger_event_types: ['webhook'],
  },
};

function sampleFlow(options: {
  trigger: TriggerKind;
  createPullRequest: boolean;
  notifications?: Record<string, unknown>;
  agentConfig?: unknown;
}) {
  return {
    id: 'flow-1',
    name: 'Issue fixer',
    prompt_template: 'fix it',
    agent_type: 'codex',
    ...TRIGGERS[options.trigger],
    git_clone_config: {
      enabled: true,
      create_pull_request: options.createPullRequest,
    },
    ...(options.notifications ? { notifications: options.notifications } : {}),
    ...(options.agentConfig ? { agent_config: options.agentConfig } : {}),
  };
}

describe('PreloopFlowForm PR-dependent sections', () => {
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
    sessionStorage.clear();
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

  const query = (element: PreloopFlowForm, selector: string) =>
    element.shadowRoot!.querySelector(selector);

  const successCheckbox = (element: PreloopFlowForm) =>
    query(element, 'sl-checkbox[data-notification="on_success_comment"]') as
      (HTMLInputElement & { checked: boolean }) | null;

  const cases: Array<{
    trigger: TriggerKind;
    createPullRequest: boolean;
    feedback: boolean;
    issueComment: boolean;
  }> = [
    {
      trigger: 'issue',
      createPullRequest: false,
      feedback: false,
      issueComment: false,
    },
    {
      trigger: 'pull_request',
      createPullRequest: false,
      feedback: false,
      issueComment: false,
    },
    {
      trigger: 'manual',
      createPullRequest: false,
      feedback: false,
      issueComment: false,
    },
    {
      trigger: 'issue',
      createPullRequest: true,
      feedback: true,
      issueComment: true,
    },
    {
      trigger: 'pull_request',
      createPullRequest: true,
      feedback: true,
      issueComment: false,
    },
    {
      trigger: 'manual',
      createPullRequest: true,
      feedback: true,
      issueComment: false,
    },
  ];

  for (const testCase of cases) {
    const label = `${testCase.trigger} trigger, create PR ${
      testCase.createPullRequest ? 'on' : 'off'
    }`;
    it(`renders the right sections for a ${label}`, async () => {
      const element = await mount(sampleFlow(testCase));

      expect(
        Boolean(query(element, '[data-feedback-editor]')),
        'PR review and CI follow-up card'
      ).to.equal(testCase.feedback);
      expect(
        Boolean(query(element, '[data-notifications-card]')),
        'Notifications card'
      ).to.equal(testCase.issueComment);
      expect(Boolean(successCheckbox(element)), 'success comment').to.equal(
        testCase.issueComment
      );
      // The failure comment option is gone in every combination.
      expect(query(element, '[data-notification="on_failure_comment"]')).to.not
        .exist;
    });

    it(`hints at what the create PR checkbox unlocks for a ${label}`, async () => {
      const element = await mount(sampleFlow(testCase));
      const hint = query(element, '[data-pr-options-hint]');
      expect(hint, 'hint under the create PR checkbox').to.exist;
      expect(hint!.textContent!.replace(/\s+/g, ' ').trim()).to.equal(
        'Enables PR review and CI follow-up, and the issue comment when an issue event triggers this flow.'
      );
    });

    it(`submits notifications without a failure block for a ${label}`, async () => {
      const element = await mount(sampleFlow(testCase));
      const payload = await submit(element);
      expect(payload.notifications).to.deep.equal({
        on_success: { comment_on_trigger_issue: false },
      });
    });
  }

  it('hides the hint until git cloning is enabled', async () => {
    const element = await mount({
      id: 'flow-1',
      name: 'No clone',
      git_clone_config: { enabled: false },
      ...TRIGGERS.issue,
    });
    expect(query(element, '[data-pr-options-hint]')).to.not.exist;
    expect(query(element, '[data-feedback-editor]')).to.not.exist;
    expect(query(element, '[data-notifications-card]')).to.not.exist;
  });

  it('reveals both sections when create PR is ticked, without a reload', async () => {
    const element = await mount(
      sampleFlow({ trigger: 'issue', createPullRequest: false })
    );
    const createPr = query(
      element,
      'sl-checkbox[data-git="create_pull_request"]'
    ) as any;
    expect(createPr, 'create PR checkbox').to.exist;
    createPr.checked = true;
    createPr.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await element.updateComplete;

    expect(query(element, '[data-feedback-editor]')).to.exist;
    expect(query(element, '[data-notifications-card]')).to.exist;

    createPr.checked = false;
    createPr.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await element.updateComplete;
    expect(query(element, '[data-feedback-editor]')).to.not.exist;
    expect(query(element, '[data-notifications-card]')).to.not.exist;
  });

  it('reveals the issue comment when an issue event is added to the trigger', async () => {
    const element = await mount(
      sampleFlow({ trigger: 'pull_request', createPullRequest: true })
    );
    expect(query(element, '[data-notifications-card]')).to.not.exist;

    element.flow.trigger_event_types = ['pull_request_opened', 'issue_opened'];
    (element as any).requestUpdate();
    await element.updateComplete;
    expect(query(element, '[data-notifications-card]')).to.exist;
  });

  it('renders and submits a saved issue comment when it applies', async () => {
    const element = await mount(
      sampleFlow({
        trigger: 'issue',
        createPullRequest: true,
        notifications: { on_success: { comment_on_trigger_issue: true } },
      })
    );
    expect(successCheckbox(element)!.checked).to.equal(true);
    const payload = await submit(element);
    expect(payload.notifications).to.deep.equal({
      on_success: { comment_on_trigger_issue: true },
    });
  });

  it('toggles the issue comment into the submit payload', async () => {
    const element = await mount(
      sampleFlow({ trigger: 'issue', createPullRequest: true })
    );
    const checkbox = successCheckbox(element)!;
    checkbox.checked = true;
    checkbox.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
    await element.updateComplete;

    const payload = await submit(element);
    expect(payload.notifications.on_success.comment_on_trigger_issue).to.equal(
      true
    );
  });

  it('preserves a hidden issue comment instead of rewriting it', async () => {
    // A flow that opens its PR through the MCP tool rather than on commit:
    // the section is hidden, the saved value must survive an unrelated save.
    const element = await mount(
      sampleFlow({
        trigger: 'issue',
        createPullRequest: false,
        notifications: { on_success: { comment_on_trigger_issue: true } },
      })
    );
    expect(query(element, '[data-notifications-card]')).to.not.exist;
    const payload = await submit(element);
    expect(payload.notifications).to.deep.equal({
      on_success: { comment_on_trigger_issue: true },
    });
  });

  it('drops a stale failure comment from an old flow on save', async () => {
    const element = await mount(
      sampleFlow({
        trigger: 'issue',
        createPullRequest: true,
        notifications: {
          on_failure: { comment_on_trigger_issue: true, attention_item: true },
          on_success: { comment_on_trigger_issue: false },
        },
      })
    );
    const payload = await submit(element);
    expect(payload.notifications).to.deep.equal({
      on_success: { comment_on_trigger_issue: false },
    });
  });

  it('passes a hidden follow-up config through untouched', async () => {
    const feedback = {
      enabled: true,
      max_turns: 5,
      max_cost: 100,
      max_age_hours: 168,
      debounce_seconds: 30,
    };
    const element = await mount(
      sampleFlow({
        trigger: 'issue',
        createPullRequest: false,
        agentConfig: { feedback, image: 'project:test' },
      })
    );
    expect(element.shadowRoot!.querySelector('[data-feedback-editor]')).to.not
      .exist;
    const payload = await submit(element);
    expect(payload.agent_config).to.deep.equal({
      feedback,
      image: 'project:test',
    });
  });
});
