import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './unlocked-tools-review-dialog.ts';
import type { UnlockedToolsReviewDialog } from './unlocked-tools-review-dialog.ts';

const SAMPLE_TOOLS = [
  {
    name: 'get_issue',
    description: 'Get an issue',
    source: 'builtin',
    source_id: null,
    source_name: 'Built-in',
    schema: {},
    is_enabled: true,
    is_supported: true,
    config_id: null,
    schema_tokens_estimate: 120,
  },
  {
    name: 'create_issue',
    description: 'Create an issue',
    source: 'builtin',
    source_id: null,
    source_name: 'Built-in',
    schema: {},
    is_enabled: true,
    is_supported: true,
    config_id: 'cfg-create',
    schema_tokens_estimate: 200,
  },
  {
    name: 'add_comment',
    description: 'Add a comment',
    source: 'builtin',
    source_id: null,
    source_name: 'Built-in',
    schema: {},
    is_enabled: true,
    is_supported: true,
    config_id: null,
    schema_tokens_estimate: 80,
  },
];

describe('UnlockedToolsReviewDialog', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.clear();
  });

  async function mountDialog(toolNames: string[]) {
    const el = (await fixture(
      html`<unlocked-tools-review-dialog></unlocked-tools-review-dialog>`
    )) as UnlockedToolsReviewDialog;

    const getTools = sandbox.stub().resolves(SAMPLE_TOOLS);
    const createToolConfiguration = sandbox.stub().resolves({ id: 'new-cfg' });
    const updateToolConfiguration = sandbox
      .stub()
      .resolves({ id: 'cfg-create' });
    el._api = { getTools, createToolConfiguration, updateToolConfiguration };
    el.toolNames = toolNames;
    el.open = true;
    await el.updateComplete;
    await waitUntil(
      () => el.shadowRoot?.querySelector('.tool-row') !== null,
      'Tool rows did not render'
    );
    return { el, getTools, createToolConfiguration, updateToolConfiguration };
  }

  it('lists unlocked tools with token estimates and total delta', async () => {
    const { el } = await mountDialog(['get_issue', 'create_issue']);

    const rows = el.shadowRoot?.querySelectorAll('.tool-row');
    expect(rows?.length).to.equal(2);

    const text = el.shadowRoot?.textContent ?? '';
    expect(text).to.include('get_issue');
    expect(text).to.include('create_issue');
    expect(text).to.include('~120');
    expect(text).to.include('tokens/request');
    expect(text).to.include('~320');
    expect(text).to.include('to every agent request');
  });

  it('defaults toggles ON and persists only deselected tools on confirm', async () => {
    const { el, createToolConfiguration, updateToolConfiguration } =
      await mountDialog(['get_issue', 'create_issue', 'add_comment']);

    const switches = el.shadowRoot?.querySelectorAll('sl-switch');
    expect(switches?.length).to.equal(3);
    switches?.forEach((sw) => {
      expect((sw as HTMLInputElement & { checked: boolean }).checked).to.be
        .true;
    });

    // Opt out get_issue (no config) and create_issue (has config).
    const getIssueSwitch = switches![0] as HTMLElement & { checked: boolean };
    const createIssueSwitch = switches![1] as HTMLElement & {
      checked: boolean;
    };
    getIssueSwitch.checked = false;
    getIssueSwitch.dispatchEvent(
      new CustomEvent('sl-change', { bubbles: true })
    );
    createIssueSwitch.checked = false;
    createIssueSwitch.dispatchEvent(
      new CustomEvent('sl-change', { bubbles: true })
    );
    await el.updateComplete;

    const confirm = el.shadowRoot?.querySelector(
      'sl-button[variant="primary"]'
    ) as HTMLElement;
    confirm.click();

    await waitUntil(
      () =>
        createToolConfiguration.calledOnce &&
        updateToolConfiguration.calledOnce,
      'Expected two tool-configuration writes'
    );

    expect(createToolConfiguration.firstCall.args[0]).to.include({
      tool_name: 'get_issue',
      tool_source: 'builtin',
      is_enabled: false,
    });
    expect(updateToolConfiguration.firstCall.args[0]).to.equal('cfg-create');
    expect(updateToolConfiguration.firstCall.args[1]).to.deep.equal({
      is_enabled: false,
    });
  });

  it('dismiss does not write tool configurations', async () => {
    const { el, createToolConfiguration, updateToolConfiguration } =
      await mountDialog(['get_issue']);

    const dismiss = Array.from(
      el.shadowRoot?.querySelectorAll('sl-button') ?? []
    ).find((b) => b.textContent?.trim() === 'Dismiss') as HTMLElement;
    dismiss.click();
    await el.updateComplete;

    expect(createToolConfiguration.called).to.be.false;
    expect(updateToolConfiguration.called).to.be.false;
  });
});
