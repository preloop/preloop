import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import type { Tool, ApprovalWorkflow } from './tool-card';
import type { AccessRuleSummary } from './governance-rule-set-editor';

interface ToolGroup {
  id: string;
  name: string;
  type: 'builtin' | 'mcp' | 'http';
  server?: any;
  tools: Tool[];
  collapsed: boolean;
}

@customElement('scoped-tools-editor')
export class ScopedToolsEditor extends LitElement {
  @property({ type: Array }) tools: Tool[] = [];
  @property({ type: Array }) mcpServers: any[] = [];
  @property({ type: Object }) scopedToolRules: Record<
    string,
    AccessRuleSummary[]
  > = {};
  @property({ type: Array }) approvalPolicies: ApprovalWorkflow[] = [];
  @property({ type: Object }) features: Record<string, any> = {};

  @state() private expandedTools: Set<string> = new Set();
  @state() private collapsedGroups: Set<string> = new Set();

  static styles = css`
    :host {
      display: block;
    }
    .tool-groups {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
    }
    .tool-group {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      overflow: hidden;
      background: var(--sl-color-neutral-0);
    }
    .section-header {
      display: flex;
      align-items: center;
      padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      background: var(--sl-color-neutral-50);
      cursor: pointer;
      user-select: none;
      gap: var(--sl-spacing-small);
    }
    .section-icon {
      transition: transform 0.2s ease;
      color: var(--sl-color-neutral-500);
    }
    .section-icon.open {
      transform: rotate(90deg);
    }
    .section-title {
      font-weight: 600;
      color: var(--sl-color-neutral-900);
    }
    .section-meta {
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-500);
      margin-left: auto;
    }
    .tool-list {
      display: flex;
      flex-direction: column;
      border-top: 1px solid var(--sl-color-neutral-200);
    }
    .tool-list > *:not(:last-child) {
      border-bottom: 1px solid var(--sl-color-neutral-200);
    }
  `;

  private _getToolGroups(): ToolGroup[] {
    const groups: Record<string, ToolGroup> = {};

    groups['builtin'] = {
      id: 'builtin',
      name: 'Built-in Tools',
      type: 'builtin',
      tools: [],
      collapsed: this.collapsedGroups.has('builtin'),
    };

    for (const server of this.mcpServers) {
      groups[server.id] = {
        id: server.id,
        name: server.name,
        type: 'mcp',
        server,
        tools: [],
        collapsed: this.collapsedGroups.has(server.id),
      };
    }

    const mcpServersByName = new Map(this.mcpServers.map((s) => [s.name, s]));

    for (const tool of this.tools) {
      if (tool.source === 'builtin') {
        groups['builtin'].tools.push(tool);
      } else if (tool.source === 'mcp') {
        let serverId = tool.source_id;
        if (!serverId && tool.source_name) {
          serverId = mcpServersByName.get(tool.source_name)?.id;
        }
        if (serverId && groups[serverId]) {
          groups[serverId].tools.push(tool);
        }
      }
    }

    return Object.values(groups)
      .filter((g) => g.tools.length > 0 || g.type === 'mcp')
      .sort((a, b) => {
        if (a.type === 'builtin' && b.type !== 'builtin') return -1;
        if (a.type !== 'builtin' && b.type === 'builtin') return 1;
        return a.name.localeCompare(b.name);
      });
  }

  private _toggleGroup(groupId: string) {
    const updated = new Set(this.collapsedGroups);
    if (updated.has(groupId)) {
      updated.delete(groupId);
    } else {
      updated.add(groupId);
    }
    this.collapsedGroups = updated;
  }

  private _handleToggleExpand(e: CustomEvent) {
    const key = e.detail.tool.name;
    const updated = new Set(this.expandedTools);
    if (updated.has(key)) {
      updated.delete(key);
    } else {
      updated.add(key);
    }
    this.expandedTools = updated;
  }

  render() {
    const groups = this._getToolGroups();

    return html`
      <div class="tool-groups">
        ${groups.map(
          (group) => html`
            <div class="tool-group">
              <div
                class="section-header"
                @click=${() => this._toggleGroup(group.id)}
              >
                <sl-icon
                  class="section-icon ${!group.collapsed ? 'open' : ''}"
                  name="chevron-right"
                ></sl-icon>
                <span class="section-title">${group.name}</span>
              </div>
              ${
                !group.collapsed
                  ? html`
                      <div class="tool-list">
                        ${
                          group.tools.length === 0
                            ? html`
                                <div
                                  style="padding: var(--sl-spacing-small); color: var(--sl-color-neutral-400); font-size: var(--sl-font-size-small);"
                                >
                                  No tools discovered.
                                </div>
                              `
                            : repeat(
                                group.tools,
                                (t) => t.name,
                                (tool) => {
                                  const rules =
                                    this.scopedToolRules[tool.name] || [];
                                  return html`
                                    <tool-list-item
                                      .tool=${tool}
                                      .accessRules=${rules}
                                      .policies=${this.approvalPolicies}
                                      .features=${this.features}
                                      ?expanded=${this.expandedTools.has(tool.name)}
                                      @toggle-expand=${this._handleToggleExpand}
                                    ></tool-list-item>
                                  `;
                                }
                              )
                        }
                      </div>
                    `
                  : null
              }
            </div>
          `
        )}
      </div>
    `;
  }
}
