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
