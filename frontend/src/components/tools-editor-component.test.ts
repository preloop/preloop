import { html, fixture, expect } from '@open-wc/testing';

import './tools-editor-component';
import type { ToolsEditorComponent } from './tools-editor-component';

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
    el.addEventListener('scan-server', (event: Event) => {
      events.push('scan-server');
      expect((event as CustomEvent).detail).to.equal('srv-1');
    });
    el.addEventListener('suggest-starter-policy', () => {
      events.push('suggest-starter-policy');
    });

    const refresh = el.shadowRoot?.querySelector(
      'sl-icon-button[name="arrow-clockwise"]'
    ) as HTMLElement | null;
    expect(refresh).to.exist;
    refresh!.click();

    expect(events).to.deep.equal(['scan-server']);
  });
});
