import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  oneEvent,
  waitUntil,
} from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';

import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

const GITHUB_TRACKER = {
  id: 'tracker-github',
  name: 'GitHub',
  tracker_type: 'github',
};

const ISSUE_TRIAGE = {
  id: 'preset-001',
  name: 'Issue Triage Assistant',
  description: 'Automatically analyze new issues.',
  icon: 'funnel',
  prompt_template: 'Triage this issue.',
  trigger_event_types: ['issue_opened'],
  allowed_mcp_tools: [{ name: 'get_issue' }],
  agent_type: 'codex',
  agent_config: {},
};

const PR_REVIEWER = {
  id: 'preset-002',
  name: 'Pull Request Reviewer',
  description: 'Review a pull request when it opens.',
  icon: 'code-square',
  prompt_template: 'Review this pull request.',
  trigger_event_types: ['pull_request_opened'],
  allowed_mcp_tools: [{ name: 'get_pull_request' }],
  agent_type: 'codex',
  agent_config: {},
  git_clone_config: { enabled: true, create_pull_request: false },
};

const OBSERVE_EVAL = {
  id: 'preset-003',
  name: 'Observe / Eval',
  description: 'Watch a repository on a schedule.',
  icon: 'eye',
  prompt_template: 'Observe the repository.',
  trigger_event_types: [],
  allowed_mcp_tools: [],
  agent_type: 'codex',
  agent_config: {},
};

const PRESETS = [ISSUE_TRIAGE, PR_REVIEWER, OBSERVE_EVAL];

describe('PreloopFlowForm presets', () => {
  let sandbox: SinonSandbox;
  let previousUrl: string;

  beforeEach(() => {
    previousUrl = `${window.location.pathname}${window.location.search}`;
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url: any) => {
      const target = String(url);
      if (target.includes('/api/v1/flows/presets')) {
        return new Response(JSON.stringify(PRESETS));
      }
      if (target.includes('/api/v1/trackers')) {
        return new Response(JSON.stringify([GITHUB_TRACKER]));
      }
      if (target.includes('/api/v1/agents')) {
        return new Response(JSON.stringify({ items: [] }));
      }
      if (target.includes('/api/v1/organizations')) {
        return new Response(JSON.stringify({ items: [] }));
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    window.history.replaceState({}, '', previousUrl);
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
  });

  const mount = async (flow: Record<string, unknown> = {}) => {
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

  const picker = (element: PreloopFlowForm) =>
    element.shadowRoot!.querySelector('preloop-flow-preset-picker') as any;

  it('does not set flow.id when selecting a preset and submits source_preset_id without id', async () => {
    const element = await mount();
    await (element as any).applyPresetSelection('preset-002');
    await element.updateComplete;

    expect(element.flow.id).to.equal(undefined);
    expect((element as any).sourcePresetId).to.equal('preset-002');

    const payload = await submit(element);
    expect(payload.source_preset_id).to.equal('preset-002');
    expect(payload).to.not.have.property('id');
  });

  it('keeps issue_opened when Issue Triage is selected on a GitHub tracker', async () => {
    const element = await mount();
    await (element as any).applyPresetSelection('preset-001');
    await element.updateComplete;

    expect(element.flow.trigger_event_types).to.deep.equal(['issue_opened']);
    expect(element.flow.trigger_event_source).to.equal(GITHUB_TRACKER.id);
  });

  it('does not turn Observe / Eval into a PR tracker flow', async () => {
    const element = await mount();
    await (element as any).applyPresetSelection('preset-003');
    await element.updateComplete;

    expect(element.flow.trigger_event_types).to.deep.equal([]);
    expect(element.flow.trigger_event_source).to.equal(undefined);
    expect((element as any).triggerType).to.equal('webhook');
  });

  it('clears the prompt when Blank flow is chosen after a preset', async () => {
    const element = await mount();
    await (element as any).applyPresetSelection('preset-002');
    await element.updateComplete;
    expect(element.flow.prompt_template).to.equal('Review this pull request.');

    await (element as any).applyPresetSelection('blank');
    await element.updateComplete;

    expect(element.flow.prompt_template).to.be.undefined;
    expect((element as any).sourcePresetId).to.equal(null);
    expect((element as any).pickerSelectedId).to.equal('blank');
  });

  it('shows Replace your edits? after an edited prompt is switched', async () => {
    const element = await mount();
    await (element as any).applyPresetSelection('preset-002');
    await element.updateComplete;

    element.flow = {
      ...element.flow,
      prompt_template: 'A rewritten review prompt.',
    };
    await element.updateComplete;

    (element as any).handlePickerSelect(
      new CustomEvent('preset-select', { detail: { presetId: 'preset-001' } })
    );
    await element.updateComplete;

    const dialog = element.shadowRoot!.querySelector(
      'sl-dialog[label="Replace your edits?"]'
    );
    expect(dialog).to.exist;
    const dialogText = (element.shadowRoot!.textContent || '').replace(
      /\s+/g,
      ' '
    );
    expect(dialogText).to.include(
      'Switching presets replaces the prompt, tools and trigger you changed.'
    );
    expect(element.shadowRoot!.textContent).to.include('Keep editing');
    expect(element.shadowRoot!.textContent).to.include('Switch preset');
    const keepEditing = element.shadowRoot!.querySelector(
      'sl-button[autofocus]'
    );
    expect(keepEditing).to.exist;
    expect(keepEditing!.textContent).to.contain('Keep editing');
    expect((element as any).pickerSelectedId).to.equal('preset-002');
    expect(element.flow.prompt_template).to.equal('A rewritten review prompt.');
  });

  it('replaces the prompt when Switch preset is confirmed', async () => {
    const element = await mount();
    await (element as any).applyPresetSelection('preset-002');
    await element.updateComplete;

    element.flow = {
      ...element.flow,
      prompt_template: 'A rewritten review prompt.',
    };
    await element.updateComplete;

    (element as any).handlePickerSelect(
      new CustomEvent('preset-select', { detail: { presetId: 'preset-001' } })
    );
    await element.updateComplete;
    expect(element.flow.prompt_template).to.equal('A rewritten review prompt.');

    (element as any).confirmSwitchPreset();
    await waitUntil(
      () => (element as any).pickerSelectedId === 'preset-001',
      'Switch preset did not apply the Issue Triage selection'
    );
    await element.updateComplete;

    expect(element.flow.prompt_template).to.equal('Triage this issue.');
    expect((element as any).pickerSelectedId).to.equal('preset-001');
    expect((element as any).replaceEditsOpen).to.be.false;
  });

  it('collapses the picker when the already-selected row is chosen again', async () => {
    const element = await mount();
    await (element as any).applyPresetSelection('preset-002');
    await element.updateComplete;
    expect((element as any).pickerCollapsed).to.be.true;

    (element as any).handlePickerChangeRequest();
    await element.updateComplete;
    expect((element as any).pickerCollapsed).to.be.false;

    (element as any).handlePickerSelect(
      new CustomEvent('preset-select', { detail: { presetId: 'preset-002' } })
    );
    await element.updateComplete;

    expect((element as any).pickerCollapsed).to.be.true;
    expect((element as any).pickerSelectedId).to.equal('preset-002');
    expect(element.flow.prompt_template).to.equal('Review this pull request.');
  });

  it('collapses the picker when opened with ?preset_id=', async () => {
    window.history.replaceState(
      {},
      '',
      '/console/flows/new?preset_id=preset-002'
    );
    const element = await mount();

    expect((element as any).pickerSelectedId).to.equal('preset-002');
    expect((element as any).pickerCollapsed).to.be.true;
    expect(picker(element).collapsed).to.be.true;
    expect(picker(element).selectedId).to.equal('preset-002');
  });

  it('warns on the next pick after typing into an unknown ?preset_id= draft', async () => {
    window.history.replaceState(
      {},
      '',
      '/console/flows/new?preset_id=missing-preset'
    );
    const element = await mount();
    expect((element as any).presetSnapshot).to.not.equal(null);

    element.flow = {
      ...element.flow,
      prompt_template: 'A typed prompt.',
    };
    await element.updateComplete;

    (element as any).handlePickerSelect(
      new CustomEvent('preset-select', { detail: { presetId: 'preset-002' } })
    );
    await element.updateComplete;

    expect(
      element.shadowRoot!.querySelector(
        'sl-dialog[label="Replace your edits?"]'
      )
    ).to.exist;
    expect(element.flow.prompt_template).to.equal('A typed prompt.');
  });
});
