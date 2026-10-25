import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './tools-editor-component';
import type { ToolsEditorComponent } from './tools-editor-component';
import type { ToolWithRules } from './tools-editor-component';
import { resetConfirmDialogForTests } from './confirm-dialog';

describe('ToolsEditorComponent – MCP server actions', () => {
  const server = {
    id: 'srv-1',
    name: 'Example MCP Server',
    url: 'https://example.com/mcp',
  };

  const tool = {
    name: 'list_issues',
    description: 'List issues',
    source: 'mcp',
    source_id: 'srv-1',
    source_name: 'Example MCP Server',
    schema: {},
    is_enabled: true,
    is_supported: true,
    approval_workflow_id: null,
    has_approval_condition: false,
    config_id: null,
    access_rules: [],
  };

  it('shows shared server tools with read-only configuration controls', async () => {
    const el = await fixture<ToolsEditorComponent>(html`
      <tools-editor-component
        mode="global"
        .mcpServers=${[{ ...server, is_shared: true, read_only: true }]}
        .tools=${[tool]}
      ></tools-editor-component>
    `);
    const row = el.shadowRoot!.querySelector('tool-list-item') as any;
    row.expanded = true;
    await row.updateComplete;
    expect(row.shadowRoot.querySelector('sl-switch').disabled).to.equal(true);
    expect(row.shadowRoot.querySelector('[label="Tool settings"]')).not.to
      .exist;
    expect(el.shadowRoot!.querySelector('.section-actions')).not.to.exist;
    expect(el.shadowRoot!.textContent).to.include('Shared server');
    const editor = row.shadowRoot.querySelector('governance-rule-set-editor');
    await editor.updateComplete;
    expect(editor.shadowRoot.querySelector('sl-button').disabled).to.equal(
      true
    );
    const toggle = sinon.spy();
    el.addEventListener('toggle-enabled', toggle);
    row.shadowRoot
      .querySelector('sl-switch')
      .dispatchEvent(new Event('sl-change'));
    expect(toggle.called).to.equal(false);
  });

  it('allows flow-scoped overrides for shared tools without global settings', async () => {
    const el = await fixture<ToolsEditorComponent>(html`
      <tools-editor-component
        mode="scoped"
        .mcpServers=${[{ ...server, is_shared: true, read_only: true }]}
        .tools=${[tool]}
      ></tools-editor-component>
    `);
    const row = el.shadowRoot!.querySelector('tool-list-item') as any;
    row.expanded = true;
    await row.updateComplete;
    const toggle = sinon.spy();
    const save = sinon.spy();
    el.addEventListener('toggle-enabled', toggle);
    el.addEventListener('save-rule', save);
    const control = row.shadowRoot.querySelector('sl-switch');
    expect(control.disabled).to.equal(false);
    control.dispatchEvent(new Event('sl-change'));
    expect(toggle.calledOnce).to.equal(true);
    const editor = row.shadowRoot.querySelector('governance-rule-set-editor');
    await editor.updateComplete;
    expect(editor.shadowRoot.querySelector('sl-button').disabled).to.equal(
      false
    );
    editor.dispatchEvent(
      new CustomEvent('save-rule', { detail: { formData: { action: 'deny' } } })
    );
    expect(save.calledOnce).to.equal(true);
    expect(row.shadowRoot.querySelector('[label="Tool settings"]')).not.to
      .exist;
  });

  it('refresh dispatches scan-server only', async () => {
    const el = (await fixture(html`
      <tools-editor-component
        mode="global"
        .hasDefaultAIModel=${true}
        .mcpServers=${[server]}
        .tools=${[tool]}
      ></tools-editor-component>
    `)) as ToolsEditorComponent;
    await el.updateComplete;

    const events: string[] = [];
    let scannedId: string | undefined;
    el.addEventListener('scan-server', (event: Event) => {
      events.push('scan-server');
      scannedId = (event as CustomEvent).detail;
    });
    el.addEventListener('suggest-starter-policy', () => {
      events.push('suggest-starter-policy');
    });

    const refresh = el.shadowRoot?.querySelector(
      'sl-icon-button[name="arrow-clockwise"]'
    ) as HTMLElement | null;
    expect(refresh).to.exist;
    expect(refresh!.getAttribute('label')).to.equal('Scan for new tools');
    const magic = el.shadowRoot?.querySelector(
      'sl-icon-button[name="magic"]'
    ) as HTMLElement | null;
    expect(magic).to.exist;
    expect(magic!.getAttribute('label')).to.equal('Suggest starter policy');
    refresh!.click();

    expect(events).to.deep.equal(['scan-server']);
    expect(scannedId).to.equal('srv-1');
  });

  it('names the edit and delete server buttons', async () => {
    const el = (await fixture(html`
      <tools-editor-component
        mode="global"
        .mcpServers=${[server]}
        .tools=${[tool]}
      ></tools-editor-component>
    `)) as ToolsEditorComponent;
    await el.updateComplete;

    expect(
      el.shadowRoot
        ?.querySelector('sl-icon-button[name="pencil"]')
        ?.getAttribute('label')
    ).to.equal('Edit server Example MCP Server');
    expect(
      el.shadowRoot
        ?.querySelector('sl-icon-button[name="trash"]')
        ?.getAttribute('label')
    ).to.equal('Delete server Example MCP Server');
  });

  it('confirms in the console dialog, with what goes, before deleting a server', async () => {
    const el = (await fixture(html`
      <tools-editor-component
        mode="global"
        .mcpServers=${[server]}
        .tools=${[
          {
            ...tool,
            access_rules: [
              {
                id: 'r1',
                action: 'deny',
                condition_expression: null,
                condition_type: 'cel',
                priority: 1,
                description: null,
                is_enabled: true,
                approval_workflow_id: null,
              },
            ],
          },
        ]}
      ></tools-editor-component>
    `)) as ToolsEditorComponent;
    await el.updateComplete;
    const nativeConfirm = sinon.stub(window, 'confirm').returns(true);
    const deleted: string[] = [];
    el.addEventListener('delete-server', (event: Event) =>
      deleted.push((event as CustomEvent).detail)
    );
    try {
      (
        el.shadowRoot?.querySelector(
          'sl-icon-button[name="trash"]'
        ) as HTMLElement
      ).click();
      await waitUntil(
        () => !!document.querySelector('confirm-dialog'),
        'no confirm dialog'
      );
      const dialog = document.querySelector('confirm-dialog')!;
      await (dialog as any).updateComplete;
      const text = dialog.shadowRoot?.textContent?.replace(/\s+/g, ' ') ?? '';
      expect(text).to.contain('Example MCP Server');
      expect(text).to.contain('1 tool and 1 access rule');
      expect(nativeConfirm.called, 'used the native confirm').to.equal(false);
      expect(deleted).to.deep.equal([]);

      (
        dialog.shadowRoot?.querySelector(
          '[data-testid="confirm-dialog-confirm"]'
        ) as HTMLElement
      ).click();
      await waitUntil(() => deleted.length === 1, 'server was not deleted');
      expect(deleted).to.deep.equal(['srv-1']);
    } finally {
      nativeConfirm.restore();
      resetConfirmDialogForTests();
    }
  });

  it('opens and closes a server group from a real button with aria-expanded', async () => {
    sessionStorage.removeItem('preloopCollapsedGroups');
    const el = (await fixture(html`
      <tools-editor-component
        mode="global"
        .mcpServers=${[server]}
        .tools=${[tool]}
      ></tools-editor-component>
    `)) as ToolsEditorComponent;
    await el.updateComplete;

    const toggle = () =>
      el.shadowRoot?.querySelector(
        '.section-header button.section-toggle'
      ) as HTMLButtonElement;
    expect(toggle()).to.exist;
    expect(toggle().textContent).to.contain('Example MCP Server');
    const before = toggle().getAttribute('aria-expanded');
    expect(before === 'true' || before === 'false').to.equal(true);

    toggle().click();
    await el.updateComplete;
    // One click toggles once: the header's own click handler must not undo it.
    const after = toggle().getAttribute('aria-expanded');
    expect(after).to.equal(before === 'true' ? 'false' : 'true');
    expect(!!el.shadowRoot?.querySelector('.tool-list')).to.equal(
      after === 'true'
    );
    sessionStorage.removeItem('preloopCollapsedGroups');
  });
});

function makeTool(
  overrides: Partial<ToolWithRules> & { name: string }
): ToolWithRules {
  return {
    description: `${overrides.name} tool`,
    source: 'builtin',
    source_id: null,
    source_name: 'Built-in',
    schema: {},
    is_enabled: true,
    is_supported: true,
    approval_workflow_id: null,
    has_approval_condition: false,
    config_id: null,
    access_rules: [],
    ...overrides,
  };
}

function groupTitles(editor: ToolsEditorComponent): string[] {
  return [...(editor.shadowRoot?.querySelectorAll('.section-title') || [])].map(
    (node) => (node.textContent || '').trim()
  );
}

describe('ToolsEditorComponent – tool name collisions (#1135)', () => {
  it('shows the server warnings and a Shadowed badge on the tool row', async () => {
    const warning =
      "Tool 'read_scope' on MCP server 'newer' is shadowed by MCP server 'older'.";
    const server = {
      id: 'srv-2',
      name: 'newer',
      url: 'https://newer.example.com/mcp',
      tool_prefix: null,
      warnings: [warning],
    };
    const tool = {
      name: 'read_scope',
      description: 'Read',
      source: 'mcp',
      source_id: 'srv-2',
      source_name: 'newer',
      schema: {},
      is_enabled: true,
      is_supported: true,
      approval_workflow_id: null,
      has_approval_condition: false,
      config_id: null,
      access_rules: [],
      shadowed: true,
      warnings: [warning],
    };
    const el = (await fixture(html`
      <tools-editor-component
        mode="global"
        .mcpServers=${[server]}
        .tools=${[tool]}
      ></tools-editor-component>
    `)) as ToolsEditorComponent;
    await el.updateComplete;
    const root = el.shadowRoot!;
    expect(
      root.querySelector('[data-testid="server-warning-badge"]')?.textContent
    ).to.contain('1 warning');
    const toggle = root.querySelector('.section-toggle') as HTMLElement;
    if (!root.querySelector('[data-testid="server-warnings"]')) toggle.click();
    await el.updateComplete;
    expect(
      root.querySelector('[data-testid="server-warnings"]')?.textContent
    ).to.contain(warning);
    const row = root.querySelector('tool-list-item') as any;
    await row.updateComplete;
    expect(row.shadowRoot.querySelector('[data-testid="tool-shadowed-badge"]'))
      .to.exist;
  });
});

describe('ToolsEditorComponent – native family', () => {
  const mixedTools: ToolWithRules[] = [
    makeTool({
      name: 'Bash',
      source: 'agent',
      source_name: 'Agent',
      adapters: ['Claude Code'],
      parameters: {
        command: { type: 'string', description: 'Shell command' },
      },
      schema: {
        type: 'object',
        properties: {
          command: { type: 'string', description: 'Shell command' },
        },
      },
    }),
    makeTool({
      name: 'Edit',
      source: 'agent',
      source_name: 'Agent',
      adapters: ['Claude Code', 'Cursor'],
      parameters: {
        file_path: { type: 'string', description: 'Path' },
      },
      schema: {
        type: 'object',
        properties: {
          file_path: { type: 'string', description: 'Path' },
        },
      },
    }),
    makeTool({
      name: 'shell',
      source: 'agent',
      source_name: 'Agent',
      adapters: ['Codex CLI'],
    }),
    makeTool({
      name: 'mystery_hook',
      source: 'agent',
      source_name: 'Agent',
      adapters: [],
    }),
    makeTool({
      name: 'github_search',
      source: 'mcp',
      source_id: 'srv-1',
      source_name: 'GitHub',
    }),
    makeTool({ name: 'example_tool', source: 'builtin' }),
  ];

  const mcpServers = [{ id: 'srv-1', name: 'GitHub' }];

  it('groups agent-source tools by adapter and hides server actions', async () => {
    const editor = (await fixture(html`
      <tools-editor-component
        family="native"
        .tools=${mixedTools}
        .mcpServers=${mcpServers}
      ></tools-editor-component>
    `)) as ToolsEditorComponent;
    await editor.updateComplete;

    expect(groupTitles(editor)).to.deep.equal([
      'Claude Code',
      'Codex CLI',
      'Cursor',
      'Seen from agents',
    ]);

    const groups = (
      editor as unknown as {
        _getToolGroups: () => { name: string; tools: { name: string }[] }[];
      }
    )._getToolGroups();
    expect(
      groups.find((g) => g.name === 'Claude Code')?.tools.map((t) => t.name)
    ).to.deep.equal(['Bash', 'Edit']);
    expect(
      groups.find((g) => g.name === 'Cursor')?.tools.map((t) => t.name)
    ).to.deep.equal(['Edit']);
    expect(
      groups
        .find((g) => g.name === 'Seen from agents')
        ?.tools.map((t) => t.name)
    ).to.deep.equal(['mystery_hook']);

    expect(
      editor.shadowRoot?.querySelector('sl-icon-button[name="arrow-clockwise"]')
    ).to.equal(null);
    expect(
      editor.shadowRoot?.querySelector('sl-icon-button[name="magic"]')
    ).to.equal(null);
    expect(
      editor.shadowRoot?.querySelector('sl-icon-button[name="pencil"]')
    ).to.equal(null);
    expect(
      editor.shadowRoot?.querySelector('sl-icon-button[name="trash"]')
    ).to.equal(null);
    expect(editor.shadowRoot?.textContent).to.not.contain('enabled');
  });

  it('family="native" renders only agent tools', async () => {
    const editor = (await fixture(html`
      <tools-editor-component
        family="native"
        .tools=${mixedTools}
        .mcpServers=${mcpServers}
      ></tools-editor-component>
    `)) as ToolsEditorComponent;
    await editor.updateComplete;

    const titles = groupTitles(editor);
    expect(titles).to.not.include('Built-in');
    expect(titles).to.not.include('GitHub');
    const names = [
      ...(editor.shadowRoot?.querySelectorAll('tool-list-item') || []),
    ].map(
      (item) => (item as HTMLElement & { tool?: { name: string } }).tool?.name
    );
    expect(names).to.have.members([
      'Bash',
      'Edit',
      'Edit',
      'shell',
      'mystery_hook',
    ]);
    expect(names).to.not.include('github_search');
    expect(names).to.not.include('example_tool');
  });

  it('uses one empty-state string when no native rows remain', async () => {
    const editor = (await fixture(html`
      <tools-editor-component
        family="native"
        .tools=${[]}
      ></tools-editor-component>
    `)) as ToolsEditorComponent;
    await editor.updateComplete;

    const copy = editor.shadowRoot?.textContent || '';
    expect(copy).to.contain('No native tools match these filters.');
    expect(copy).to.not.contain('No native tools matching filter.');
    expect(copy).to.not.contain('No native tools found.');
  });

  it('groups OpenCode adapter tools under the OpenCode label', async () => {
    const editor = (await fixture(html`
      <tools-editor-component
        family="native"
        .tools=${[
          makeTool({ name: 'Bash', source: 'agent', adapters: ['opencode'] }),
          makeTool({ name: 'List', source: 'agent', adapters: ['OpenCode'] }),
        ]}
      ></tools-editor-component>
    `)) as ToolsEditorComponent;
    await editor.updateComplete;

    expect(groupTitles(editor)).to.deep.equal(['OpenCode']);
    const groups = (
      editor as unknown as {
        _getToolGroups: () => { name: string; tools: { name: string }[] }[];
      }
    )._getToolGroups();
    expect(
      groups.find((g) => g.name === 'OpenCode')?.tools.map((t) => t.name)
    ).to.deep.equal(['Bash', 'List']);
  });

  it('groups wire ids and unknown adapters through nativeAdapterGroupName', async () => {
    const editor = (await fixture(html`
      <tools-editor-component
        family="native"
        .tools=${[
          makeTool({
            name: 'Bash',
            source: 'agent',
            adapters: ['claude-code'],
          }),
          makeTool({
            name: 'mystery_hook',
            source: 'agent',
            adapters: ['not-a-catalogue-agent'],
          }),
        ]}
      ></tools-editor-component>
    `)) as ToolsEditorComponent;
    await editor.updateComplete;

    expect(groupTitles(editor)).to.deep.equal([
      'Claude Code',
      'Seen from agents',
    ]);
    const groups = (
      editor as unknown as {
        _getToolGroups: () => { name: string; tools: { name: string }[] }[];
      }
    )._getToolGroups();
    expect(
      groups.find((g) => g.name === 'Claude Code')?.tools.map((t) => t.name)
    ).to.deep.equal(['Bash']);
    expect(
      groups
        .find((g) => g.name === 'Seen from agents')
        ?.tools.map((t) => t.name)
    ).to.deep.equal(['mystery_hook']);
  });
});

describe('ToolsEditorComponent bounded pages', () => {
  beforeEach(() => sessionStorage.clear());
  afterEach(() => sessionStorage.clear());

  const exampleTool = (name: string, source: string, sourceId?: string) => ({
    name,
    source,
    source_id: sourceId,
    description: 'Example tool',
    is_enabled: true,
    is_supported: true,
    access_rules: [],
  });

  it('pages across server boundaries while retaining whole-group counts', async () => {
    const servers = [
      { id: 'first', name: 'First server' },
      { id: 'second', name: 'Second server' },
    ];
    const tools = Array.from({ length: 16 }, (_, i) =>
      exampleTool(`tool_${i}`, 'mcp', i < 8 ? 'first' : 'second')
    );
    const el = await fixture<ToolsEditorComponent>(
      html`<tools-editor-component
        .pageSize=${10}
        .tools=${tools}
        .mcpServers=${servers}
      ></tools-editor-component>`
    );
    const root = el.shadowRoot!;
    expect(root.querySelectorAll('tool-list-item').length).to.equal(10);
    expect(root.querySelectorAll('.section-meta')[0].textContent).to.contain(
      '8/8'
    );
    const pager = root.querySelector('table-pagination')!;
    pager.dispatchEvent(
      new CustomEvent('page-change', { detail: { pageIndex: 1 } })
    );
    await el.updateComplete;
    expect(root.querySelectorAll('.tool-group').length).to.equal(1);
    expect(root.querySelectorAll('tool-list-item').length).to.equal(6);
    el.tools = tools.map((tool) => ({ ...tool, schema: { type: 'object' } }));
    await el.updateComplete;
    expect(root.querySelectorAll('tool-list-item').length).to.equal(6);
    expect((pager as HTMLElement & { pageIndex: number }).pageIndex).to.equal(
      1
    );
    el.tools = [tools[0]];
    await el.updateComplete;
    expect(root.querySelectorAll('tool-list-item').length).to.equal(1);
  });

  it('counts restored collapsed groups once and repages after expanding', async () => {
    sessionStorage.setItem('preloopCollapsedGroups', JSON.stringify(['first']));
    const servers = [
      { id: 'first', name: 'First server' },
      { id: 'second', name: 'Second server' },
    ];
    const tools = Array.from({ length: 70 }, (_, i) =>
      exampleTool(`tool_${i}`, 'mcp', i < 50 ? 'first' : 'second')
    );
    const el = await fixture<ToolsEditorComponent>(
      html`<tools-editor-component
        .pageSize=${10}
        .tools=${tools}
        .mcpServers=${servers}
      ></tools-editor-component>`
    );
    const root = el.shadowRoot!;
    const pager = root.querySelector('table-pagination') as HTMLElement & {
      total: number;
      pageRows: number;
      pageIndex: number;
      updateComplete: Promise<unknown>;
    };
    expect(pager.total).to.equal(21);
    expect(pager.pageRows).to.equal(10);
    expect(root.querySelectorAll('.tool-group').length).to.equal(2);
    expect(root.querySelectorAll('tool-list-item').length).to.equal(9);
    pager.dispatchEvent(
      new CustomEvent('page-change', { detail: { pageIndex: 2 } })
    );
    await el.updateComplete;
    expect(pager.pageRows).to.equal(1);
    expect(root.querySelectorAll('.tool-group').length).to.equal(1);
    expect(root.querySelectorAll('tool-list-item').length).to.equal(1);
    // Expand the first server from page one: all 70 tools become reachable.
    pager.dispatchEvent(
      new CustomEvent('page-change', { detail: { pageIndex: 0 } })
    );
    await el.updateComplete;
    root.querySelector<HTMLButtonElement>('.section-toggle')!.click();
    await el.updateComplete;
    expect(pager.total).to.equal(70);
    expect(pager.pageIndex).to.equal(0);
    expect(root.querySelectorAll('tool-list-item').length).to.equal(10);
    // Collapse on a later page: reset the range rather than stranding it.
    pager.dispatchEvent(
      new CustomEvent('page-change', { detail: { pageIndex: 4 } })
    );
    await el.updateComplete;
    root.querySelector<HTMLButtonElement>('.section-toggle')!.click();
    await el.updateComplete;
    expect(pager.total).to.equal(21);
    expect(pager.pageIndex).to.equal(0);
    expect(root.querySelectorAll('tool-list-item').length).to.equal(9);
  });

  it('bounds empty server headers too', async () => {
    const servers = Array.from({ length: 30 }, (_, i) => ({
      id: `server-${i}`,
      name: `Example server ${i}`,
    }));
    const el = await fixture<ToolsEditorComponent>(
      html`<tools-editor-component
        .pageSize=${10}
        .mcpServers=${servers}
      ></tools-editor-component>`
    );
    const root = el.shadowRoot!;
    expect(root.querySelectorAll('.tool-group').length).to.equal(10);
    root
      .querySelector('table-pagination')!
      .dispatchEvent(
        new CustomEvent('page-change', { detail: { pageIndex: 2 } })
      );
    await el.updateComplete;
    expect(root.querySelectorAll('.tool-group').length).to.equal(10);
    expect(root.textContent).to.contain('Example server 29');
  });

  it('bounds native rows even when a tool appears under multiple adapters', async () => {
    const tools = Array.from({ length: 20 }, (_, i) => ({
      ...exampleTool(`native_${i}`, 'agent'),
      adapters: ['codex-cli', 'claude-code'],
    }));
    const el = await fixture<ToolsEditorComponent>(
      html`<tools-editor-component
        family="native"
        .pageSize=${10}
        .tools=${tools}
      ></tools-editor-component>`
    );
    const root = el.shadowRoot!;
    expect(root.querySelectorAll('tool-list-item').length).to.equal(10);
    root
      .querySelector('table-pagination')!
      .dispatchEvent(
        new CustomEvent('page-change', { detail: { pageIndex: 3 } })
      );
    await el.updateComplete;
    expect(root.querySelectorAll('tool-list-item').length).to.equal(10);
    expect(root.textContent).to.contain('Codex CLI');
  });
});
