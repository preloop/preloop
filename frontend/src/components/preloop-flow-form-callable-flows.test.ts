import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';
import {
  DELEGATION_TOOL_NAME,
  DELEGATION_TOOL_SERVER,
} from '../utils/callable-flows';

const PARENT_ID = '11111111-1111-1111-1111-111111111111';
const CHILD_ID = '22222222-2222-2222-2222-222222222222';
const SIBLING_ID = '33333333-3333-3333-3333-333333333333';

const TOOLS = [
  { name: 'create_pull_request', source: 'builtin' },
  { name: DELEGATION_TOOL_NAME, source: 'builtin' },
];

const ACCOUNT_FLOWS = [
  { id: PARENT_ID, name: 'Parent flow' },
  { id: CHILD_ID, name: 'Child flow' },
  { id: SIBLING_ID, name: 'Sibling flow' },
];

const delegationTool = () => ({
  server_name: DELEGATION_TOOL_SERVER,
  tool_name: DELEGATION_TOOL_NAME,
});

const isAccountFlowsList = (target: string): boolean => {
  const path = target.split('?')[0];
  return path.endsWith('/api/v1/flows');
};

const sliceAccountFlows = (
  target: string,
  rows: Array<Record<string, unknown>>
): Array<Record<string, unknown>> => {
  const url = new URL(target, 'http://local.test');
  const skip = Number(url.searchParams.get('skip') || '0');
  const limitParam = url.searchParams.get('limit');
  if (limitParam === null) return rows.slice(skip);
  return rows.slice(skip, skip + Number(limitParam));
};

describe('PreloopFlowForm callable flows picker', () => {
  let sandbox: SinonSandbox;
  let accountFlows: Array<Record<string, unknown>>;
  let flowsListStatus: number;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    accountFlows = [...ACCOUNT_FLOWS];
    flowsListStatus = 200;
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url) => {
      const target = String(url);
      if (target.includes('/api/v1/flows/presets')) {
        return new Response(JSON.stringify([]));
      }
      if (target.includes('/api/v1/tools')) {
        return new Response(JSON.stringify(TOOLS));
      }
      if (isAccountFlowsList(target)) {
        if (flowsListStatus !== 200) {
          return new Response('error', { status: flowsListStatus });
        }
        return new Response(
          JSON.stringify(sliceAccountFlows(target, accountFlows))
        );
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

  const mountParent = (
    overrides: Record<string, unknown> = {}
  ): Promise<PreloopFlowForm> =>
    mount({
      id: PARENT_ID,
      name: 'Parent flow',
      prompt_template: 'Orchestrate the work',
      agent_type: 'codex',
      allowed_mcp_tools: [delegationTool()],
      ...overrides,
    });

  const submit = async (
    element: PreloopFlowForm
  ): Promise<Record<string, unknown>> => {
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    expect(listener.callCount).to.equal(1);
    return listener.firstCall.args[0].detail.flow;
  };

  const section = (element: PreloopFlowForm) =>
    element.shadowRoot!.querySelector('[data-callable-flows]');

  const row = (element: PreloopFlowForm, name: string) =>
    element.shadowRoot!.querySelector(`[data-callable-flow="${name}"]`);

  const toggleFlow = async (
    element: PreloopFlowForm,
    name: string,
    checked: boolean
  ) => {
    const checkbox = element.shadowRoot!.querySelector(
      `[data-callable-flow-toggle="${name}"]`
    ) as HTMLInputElement;
    expect(checkbox, `no row for ${name}`).to.exist;
    checkbox.checked = checked;
    checkbox.dispatchEvent(new CustomEvent('sl-change'));
    await element.updateComplete;
  };

  const setCeiling = async (
    element: PreloopFlowForm,
    attribute: string,
    name: string,
    value: string
  ) => {
    const input = element.shadowRoot!.querySelector(
      `[${attribute}="${name}"]`
    ) as HTMLInputElement;
    expect(input, `no ${attribute} field for ${name}`).to.exist;
    input.value = value;
    input.dispatchEvent(new CustomEvent('sl-input'));
    await element.updateComplete;
  };

  it('renders nothing and sends no callable_flows while the tool is off', async () => {
    const element = await mountParent({ allowed_mcp_tools: [] });

    expect(section(element)).to.not.exist;

    const payload = await submit(element);
    expect('callable_flows' in payload).to.be.false;
    // Byte identical to the body this form sent before delegation existed:
    // the keys of the request, pinned, so the field cannot leak into the save
    // of a flow that does not delegate.
    expect(
      Object.keys(JSON.parse(JSON.stringify(payload))).sort()
    ).to.deep.equal([
      'agent_config',
      'agent_type',
      'allowed_mcp_servers',
      'allowed_mcp_tools',
      'approval_window_seconds',
      'description',
      'git_clone_config',
      'is_enabled',
      'name',
      'notifications',
      'prompt_template',
      'runner_pool',
      'schedule_config',
      'timeout_seconds',
      'trigger_event_source',
      'trigger_event_types',
    ]);
  });

  it('keeps a saved allowlist out of the payload while the tool is off', async () => {
    const element = await mountParent({
      allowed_mcp_tools: [],
      callable_flows: [{ flow: 'Child flow', max_children: 2 }],
    });

    expect(section(element)).to.not.exist;
    const payload = await submit(element);
    expect('callable_flows' in payload).to.be.false;
  });

  it('reveals the section when the delegation tool is turned on', async () => {
    const element = await mountParent({ allowed_mcp_tools: [] });
    expect(section(element)).to.not.exist;

    const toolCheckbox = element.shadowRoot!.querySelector(
      `[data-builtin-tool="${DELEGATION_TOOL_NAME}"]`
    ) as HTMLInputElement;
    expect(toolCheckbox).to.exist;
    toolCheckbox.checked = true;
    toolCheckbox.dispatchEvent(new CustomEvent('sl-change'));
    await element.updateComplete;

    expect(section(element)).to.exist;
    const help = element.shadowRoot!.querySelector(
      '[data-delegation-tool-help]'
    );
    expect(help!.textContent).to.include(
      'empty list means it may call nothing'
    );
  });

  it('sends the selected flow with both ceilings', async () => {
    const element = await mountParent();
    await toggleFlow(element, 'Child flow', true);
    await setCeiling(element, 'data-callable-max-children', 'Child flow', '3');
    await setCeiling(element, 'data-callable-max-usd', 'Child flow', '1.5');

    const payload = await submit(element);
    expect(payload.callable_flows).to.deep.equal([
      { flow: 'Child flow', max_children: 3, max_usd_per_child: 1.5 },
    ]);
  });

  it('shows a saved entry ticked with its ceilings in the fields', async () => {
    const element = await mountParent({
      allowed_mcp_tools: [delegationTool()],
      callable_flows: [
        { flow: 'Child flow', max_children: 5, max_usd_per_child: 0.75 },
      ],
    });

    const checkbox = element.shadowRoot!.querySelector(
      '[data-callable-flow-toggle="Child flow"]'
    ) as HTMLInputElement;
    expect(checkbox.checked).to.be.true;
    expect(
      (
        element.shadowRoot!.querySelector(
          '[data-callable-max-children="Child flow"]'
        ) as HTMLInputElement
      ).value
    ).to.equal('5');
    expect(
      (
        element.shadowRoot!.querySelector(
          '[data-callable-max-usd="Child flow"]'
        ) as HTMLInputElement
      ).value
    ).to.equal('0.75');

    const sibling = element.shadowRoot!.querySelector(
      '[data-callable-flow-toggle="Sibling flow"]'
    ) as HTMLInputElement;
    expect(sibling.checked).to.be.false;
    expect(
      element.shadowRoot!.querySelector(
        '[data-callable-max-children="Sibling flow"]'
      )
    ).to.not.exist;
  });

  it('marks the form dirty when a ceiling is edited', async () => {
    const element = await mountParent({
      allowed_mcp_tools: [delegationTool()],
      callable_flows: [{ flow: 'Child flow' }],
    });
    expect((element as any).hasPresetEdits()).to.be.false;

    await setCeiling(element, 'data-callable-max-children', 'Child flow', '4');

    expect((element as any).hasPresetEdits()).to.be.true;
    const payload = await submit(element);
    expect(payload.callable_flows).to.deep.equal([
      { flow: 'Child flow', max_children: 4 },
    ]);
  });

  it('sends no update when the allowlist was not touched', async () => {
    const element = await mountParent({
      allowed_mcp_tools: [delegationTool()],
      callable_flows: [{ flow: 'Child flow', max_children: 2 }],
    });

    expect((element as any).hasPresetEdits()).to.be.false;
    const payload = await submit(element);
    expect('callable_flows' in payload).to.be.false;
  });

  it('renders one picker row when paging returns the same flow twice', async () => {
    accountFlows = [
      ...ACCOUNT_FLOWS,
      { id: CHILD_ID, name: 'Child flow' },
      { id: CHILD_ID, name: 'Child flow (renamed mid-page)' },
    ];
    const element = await mountParent();

    expect(
      element.shadowRoot!.querySelectorAll('[data-callable-flow="Child flow"]')
    ).to.have.lengthOf(1);
    expect(
      element.shadowRoot!.querySelector(
        '[data-callable-flow="Child flow (renamed mid-page)"]'
      )
    ).to.not.exist;
  });

  it('cannot select the same flow twice', async () => {
    const element = await mountParent();
    await toggleFlow(element, 'Child flow', true);
    await toggleFlow(element, 'Child flow', true);
    (element as any).handleCallableFlowToggle('child flow', true);
    await element.updateComplete;

    const payload = await submit(element);
    expect(payload.callable_flows).to.deep.equal([{ flow: 'Child flow' }]);
    expect(
      element.shadowRoot!.querySelectorAll('[data-callable-flow="Child flow"]')
    ).to.have.lengthOf(1);
  });

  it('clears an entry when its row is unticked', async () => {
    const element = await mountParent({
      allowed_mcp_tools: [delegationTool()],
      callable_flows: [{ flow: 'Child flow', max_children: 2 }],
    });
    await toggleFlow(element, 'Child flow', false);

    const payload = await submit(element);
    expect(payload.callable_flows).to.deep.equal([]);
  });

  it('shows an empty state and saves an empty list with no other flows', async () => {
    accountFlows = [];
    const element = await mount({
      name: 'Lonely flow',
      prompt_template: 'Do the work',
      agent_type: 'codex',
      allowed_mcp_tools: [delegationTool()],
    });

    const empty = element.shadowRoot!.querySelector(
      '[data-callable-flows-empty]'
    );
    expect(empty).to.exist;
    expect(empty!.textContent).to.include('no other flows');

    const payload = await submit(element);
    expect(payload.callable_flows).to.deep.equal([]);
  });

  it('offers this flow only as an explicit self reference', async () => {
    const element = await mountParent();
    const selfRow = row(element, 'Parent flow');
    expect(selfRow).to.exist;
    expect(selfRow!.textContent).to.include('this flow');

    await toggleFlow(element, 'Parent flow', true);
    const payload = await submit(element);
    expect(payload.callable_flows).to.deep.equal([
      { flow: 'Parent flow', allow_self: true },
    ]);
  });

  it('refuses a self entry saved without the recursion flag', async () => {
    const element = await mountParent({
      allowed_mcp_tools: [delegationTool()],
      callable_flows: [{ flow: 'Parent flow' }],
    });
    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);

    await (element as any).handleFormSubmit(new Event('submit'));
    await element.updateComplete;

    expect(listener.callCount).to.equal(0);
    expect((element as any).formError).to.include("'Parent flow'");
  });

  it('refuses a ceiling of zero before it reaches the API', async () => {
    const element = await mountParent();
    await toggleFlow(element, 'Child flow', true);
    await setCeiling(element, 'data-callable-max-children', 'Child flow', '0');

    const listener = sandbox.spy();
    element.addEventListener('flow-submit', listener);
    await (element as any).handleFormSubmit(new Event('submit'));
    await element.updateComplete;

    expect(listener.callCount).to.equal(0);
    expect((element as any).formError).to.include("'Child flow'");
    expect((element as any).formError).to.include('above zero');
  });

  it('moves focus to the section when an entry is refused', async () => {
    const element = await mountParent({
      allowed_mcp_tools: [delegationTool()],
      callable_flows: [{ flow: 'Parent flow' }],
    });

    await (element as any).handleFormSubmit(new Event('submit'));
    await element.updateComplete;

    const section = element.shadowRoot!.querySelector('[data-callable-flows]');
    expect(section?.getAttribute('tabindex')).to.equal('-1');
    expect(element.shadowRoot!.activeElement).to.equal(section);
  });

  it('marks the row the server refused and names the entry', async () => {
    const element = await mountParent();
    await toggleFlow(element, 'Child flow', true);

    (element as any).formError =
      "callable_flows entry 'Child flow' does not name a flow in this account";
    await element.updateComplete;

    const rejected = row(element, 'Child flow');
    expect(rejected!.classList.contains('rejected')).to.be.true;
    expect(rejected!.textContent).to.include('Child flow');
    expect(
      element.shadowRoot!.querySelector('.callable-flow-note')!.textContent
    ).to.include('does not name a flow in this account');
  });

  it('gives a saved entry the account no longer has a row to clear', async () => {
    const element = await mountParent({
      allowed_mcp_tools: [delegationTool()],
      callable_flows: [{ flow: 'Retired flow' }],
    });

    const orphan = row(element, 'Retired flow');
    expect(orphan).to.exist;
    expect(orphan!.textContent).to.include('not in this account');

    await toggleFlow(element, 'Retired flow', false);
    const payload = await submit(element);
    expect(payload.callable_flows).to.deep.equal([]);
  });

  it('pages past the 100-flow default so a later flow is not marked missing', async () => {
    accountFlows = Array.from({ length: 101 }, (_, index) => ({
      id: `flow-${index}`,
      name: index === 100 ? 'Page two flow' : `Flow ${index}`,
    }));
    const element = await mountParent({
      allowed_mcp_tools: [delegationTool()],
      callable_flows: [{ flow: 'Page two flow' }],
    });

    const later = row(element, 'Page two flow');
    expect(later).to.exist;
    expect(later!.textContent).to.not.include('not in this account');
    const checkbox = element.shadowRoot!.querySelector(
      '[data-callable-flow-toggle="Page two flow"]'
    ) as HTMLInputElement;
    expect(checkbox.checked).to.be.true;
  });

  it('does not badge a saved entry missing when the flows list fails', async () => {
    flowsListStatus = 500;
    const element = await mountParent({
      allowed_mcp_tools: [delegationTool()],
      callable_flows: [{ flow: 'Child flow' }],
    });

    const saved = row(element, 'Child flow');
    expect(saved).to.exist;
    expect(saved!.textContent).to.not.include('not in this account');
    expect(
      element.shadowRoot!.querySelector('[data-callable-flows-load-error]')
    ).to.exist;
    expect(element.shadowRoot!.querySelector('[data-callable-flows-empty]')).to
      .not.exist;
  });

  it('is not dirty from callable edits once the delegation tool is off', async () => {
    const element = await mountParent({ allowed_mcp_tools: [] });
    expect((element as any).hasPresetEdits()).to.be.false;

    const toolCheckbox = element.shadowRoot!.querySelector(
      `[data-builtin-tool="${DELEGATION_TOOL_NAME}"]`
    ) as HTMLInputElement;
    toolCheckbox.checked = true;
    toolCheckbox.dispatchEvent(new CustomEvent('sl-change'));
    await element.updateComplete;

    await toggleFlow(element, 'Child flow', true);
    expect((element as any).hasPresetEdits()).to.be.true;

    toolCheckbox.checked = false;
    toolCheckbox.dispatchEvent(new CustomEvent('sl-change'));
    await element.updateComplete;

    expect(section(element)).to.not.exist;
    expect((element as any).hasPresetEdits()).to.be.false;
    const payload = await submit(element);
    expect('callable_flows' in payload).to.be.false;
  });
});
