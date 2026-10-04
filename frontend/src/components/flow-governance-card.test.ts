import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './flow-governance-card';
import type { FlowGovernanceCard } from './flow-governance-card';

const FLOW_ID = '11111111-1111-4111-8111-111111111111';
const URL_BASE = `/api/v1/account/governance/flows/${FLOW_ID}`;

function governance(overrides: Record<string, unknown> = {}) {
  return {
    subject_type: 'flows',
    subject_id: FLOW_ID,
    has_override: false,
    account_defaults: {
      native_tool_approvals: 'off',
      approval_workflow_id: null,
    },
    config: {
      allowed_models: [],
      model_budgets: {},
      tool_rules: {},
      tool_enabled_overrides: {},
      approval_workflow_id: null,
      native_tool_approvals: null,
    },
    ...overrides,
  };
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('flow-governance-card', () => {
  let fetchStub: sinon.SinonStub;
  let current: Record<string, unknown>;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    current = governance();
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        if (url.startsWith(URL_BASE)) {
          const method = init?.method || 'GET';
          if (method === 'PUT') {
            current = governance({
              has_override: true,
              config: JSON.parse(String(init?.body)),
            });
          } else if (method === 'DELETE') {
            current = governance();
          }
          return json(current);
        }
        if (url.includes('/api/v1/features')) return json({ features: {} });
        if (url.includes('approval-workflows'))
          return json([
            { id: 'wf-1', name: 'Security review', is_default: true },
          ]);
        if (url.includes('ai-models') || url.includes('/models'))
          return json([{ id: 'model-1', name: 'GPT Mini' }]);
        return json([]);
      }
    );
  });

  afterEach(() => {
    fetchStub.restore();
  });

  async function mount(): Promise<FlowGovernanceCard> {
    const el = await fixture<FlowGovernanceCard>(
      html`<flow-governance-card .flowId=${FLOW_ID}></flow-governance-card>`
    );
    await waitUntil(() => !(el as any).loading, 'card did not load');
    await el.updateComplete;
    return el;
  }

  function badgeText(el: FlowGovernanceCard): string {
    return (
      el.shadowRoot?.querySelector('#flow-governance-state')?.textContent || ''
    ).trim();
  }

  it('shows inherited account defaults when the flow has no override', async () => {
    const el = await mount();
    expect(badgeText(el)).to.equal('Inherits account defaults');
    const reset = el.shadowRoot?.querySelector(
      '#flow-governance-reset'
    ) as HTMLButtonElement;
    expect(reset.disabled).to.equal(true);
    const inherit = el.shadowRoot?.querySelector(
      '#flow-native-tool-approvals sl-option[value=""]'
    );
    expect(inherit?.textContent).to.contain('account default: Off');
  });

  it('names the agent fallback for flows that run as an agent', async () => {
    const el = await mount();
    el.inheritsFromAgent = true;
    await el.updateComplete;
    const inherit = el.shadowRoot?.querySelector(
      '#flow-native-tool-approvals sl-option[value=""]'
    );
    expect(inherit?.textContent).to.contain('agent setting');
  });

  it('serializes saves so the last edit is the last write', async () => {
    const el = await mount();
    const order: string[] = [];
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    let first = true;
    const handler = async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        const body = JSON.parse(String(init.body));
        order.push(`start:${body.native_tool_approvals}`);
        if (first) {
          first = false;
          await gate;
        }
        order.push(`end:${body.native_tool_approvals}`);
        return json(governance({ has_override: true, config: body }));
      }
      return json(current);
    };
    fetchStub.callsFake(handler);
    const a = el.save({ native_tool_approvals: 'off' });
    const b = el.save({ native_tool_approvals: 'enforce' });
    await new Promise((r) => setTimeout(r, 20));
    // Only one write is in flight; the second waits for the first.
    expect(order).to.have.length(1);
    release();
    await Promise.all([a, b]);
    expect(order).to.have.length(4);
    expect(order[1].startsWith('end:')).to.equal(true);
    // Every write carries the latest local edit, so the last one wins.
    expect(order[3]).to.equal('end:enforce');
    expect((el as any).config.native_tool_approvals).to.equal('enforce');
  });

  it('saves an override through PUT and reflects it', async () => {
    const el = await mount();
    await el.save({ native_tool_approvals: 'enforce' });
    await el.updateComplete;

    const put = fetchStub
      .getCalls()
      .find((call) => call.args[1]?.method === 'PUT');
    expect(put, 'PUT sent').to.exist;
    expect(String(put!.args[0])).to.equal(URL_BASE);
    const body = JSON.parse(String(put!.args[1].body));
    expect(body.native_tool_approvals).to.equal('enforce');
    expect(badgeText(el)).to.equal('Flow override');
  });

  it('resets to account defaults through DELETE', async () => {
    current = governance({ has_override: true });
    const el = await mount();
    expect(badgeText(el)).to.equal('Flow override');

    (
      el.shadowRoot?.querySelector('#flow-governance-reset') as HTMLElement
    ).click();
    await waitUntil(
      () => badgeText(el) === 'Inherits account defaults',
      'reset did not apply'
    );
    const del = fetchStub
      .getCalls()
      .find((call) => call.args[1]?.method === 'DELETE');
    expect(del, 'DELETE sent').to.exist;
  });

  it('limits the tools editor to the flow allowed tools', async () => {
    const el = await mount();
    (el as any).toolCatalog = [
      { name: 'search_issues' },
      { name: 'delete_issue' },
    ];
    el.allowedToolNames = ['search_issues'];
    await el.updateComplete;
    const editor = el.shadowRoot?.querySelector(
      'tools-editor-component'
    ) as any;
    expect(editor.tools.map((t: any) => t.name)).to.deep.equal([
      'search_issues',
    ]);
  });
});
