import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import type { Tool, ApprovalWorkflow } from './tool-card';
import type { AccessRuleSummary } from './governance-rule-set-editor';
import type { RuleFormData } from './tool-rule-editor';
import type { GatewayUsageByTool } from '../types';
import './tool-list-item';

export interface ToolWithRules extends Tool {
  access_rules?: AccessRuleSummary[];
}

export const NATIVE_ADAPTERS: ReadonlyArray<{
  value: string;
  label: string;
}> = [
  { value: 'claude-code', label: 'Claude Code' },
  { value: 'codex-cli', label: 'Codex CLI' },
  { value: 'cursor', label: 'Cursor' },
  { value: 'openclaw', label: 'OpenClaw' },
  { value: 'hermes', label: 'Hermes' },
  { value: 'opencode', label: 'OpenCode' },
];

export const NATIVE_ADAPTER_LABELS = NATIVE_ADAPTERS.map(
  (adapter) => adapter.label
);

export const SEEN_FROM_AGENTS_LABEL = 'Seen from agents';

/** Map a wire adapter (id or display label) onto the catalogue label. */
export function nativeAdapterGroupName(raw: string | undefined): string {
  const name = (raw || '').trim();
  if (!name || name === SEEN_FROM_AGENTS_LABEL) {
    return SEEN_FROM_AGENTS_LABEL;
  }
  const hit = NATIVE_ADAPTERS.find(
    (adapter) => adapter.value === name || adapter.label === name
  );
  return hit ? hit.label : SEEN_FROM_AGENTS_LABEL;
}

export type ToolsEditorFamily = 'mcp' | 'native';

interface ToolGroup {
  id: string;
  name: string;
  type: 'builtin' | 'mcp' | 'http' | 'agent';
  server?: any;
  tools: ToolWithRules[];
  collapsed: boolean;
}

@customElement('tools-editor-component')
export class ToolsEditorComponent extends LitElement {
  @property({ type: Array }) tools: ToolWithRules[] = [];
  @property({ type: Array }) mcpServers: any[] = [];
  @property({ type: Object }) scopedToolRules: Record<
    string,
    AccessRuleSummary[]
  > = {};
  @property({ type: Object }) toolEnabledOverrides: Record<string, boolean> =
    {};
  @property({ type: Array }) approvalPolicies: ApprovalWorkflow[] = [];
  @property({ type: Object }) features: Record<string, any> = {};
  @property({ type: String }) filterText: string = '';
  @property({ type: String }) mode: 'global' | 'scoped' = 'global';
  @property({ type: Boolean }) hasDefaultAIModel: boolean = false;
  @property({ type: Boolean }) collapseByDefault: boolean = false;
  @property({ type: String }) family: ToolsEditorFamily = 'mcp';
  @property({ type: Object }) toolStats: Record<string, GatewayUsageByTool> =
    {};

  @state() private expandedTools: Set<string> = new Set();
  @state() private collapsedGroups: Set<string> = new Set();
  private _hasAutoCollapsed = false;

  constructor() {
    super();
    try {
      const savedExpanded = sessionStorage.getItem('preloopExpandedTools');
      if (savedExpanded) {
        this.expandedTools = new Set(JSON.parse(savedExpanded));
      }
      const savedCollapsed = sessionStorage.getItem('preloopCollapsedGroups');
      if (savedCollapsed) {
        this.collapsedGroups = new Set(JSON.parse(savedCollapsed));
        this._hasAutoCollapsed = true;
      }
    } catch (e) {
      console.warn('Failed to load tool editor state', e);
    }
  }

  willUpdate(changedProperties: Map<string | number | symbol, unknown>) {
    // Only collapse by default if it's the first time we load tools
    if (
      this.collapseByDefault &&
      !this._hasAutoCollapsed &&
      this.tools.length > 0
    ) {
      this._hasAutoCollapsed = true;
      const groupsToCollapse = new Set<string>();
      for (const server of this.mcpServers) groupsToCollapse.add(server.id);
      groupsToCollapse.add('builtin');
      groupsToCollapse.add('http');
      for (const adapter of NATIVE_ADAPTER_LABELS) {
        groupsToCollapse.add(`agent:${adapter}`);
      }
      groupsToCollapse.add(`agent:${SEEN_FROM_AGENTS_LABEL}`);
      this.collapsedGroups = groupsToCollapse;
      try {
        sessionStorage.setItem(
          'preloopCollapsedGroups',
          JSON.stringify([...groupsToCollapse])
        );
      } catch {}
    }
  }

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
      position: relative;
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
      margin-left: var(--sl-spacing-medium);
    }
    .section-actions {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-x-small);
      margin-left: auto;
      background: var(--sl-color-neutral-50);
      padding-left: var(--sl-spacing-small);
      z-index: 2;
    }
    .section-actions sl-icon-button {
      font-size: 1.1rem;
      color: var(--sl-color-neutral-600);
    }
    .section-actions sl-icon-button::part(base) {
      padding: 0.25rem;
    }
    .section-actions sl-icon-button:hover {
      color: var(--sl-color-primary-600);
    }
    .section-line {
      flex: 1;
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

  private _getToolKey(tool: Tool): string {
    return `${tool.name}-${tool.source}-${tool.source_id || 'none'}`;
  }

  private _getToolGroups(): ToolGroup[] {
    if (this.family === 'native') {
      return this._getNativeToolGroups();
    }
    return this._getMcpToolGroups();
  }

  private _getMcpToolGroups(): ToolGroup[] {
    const groups: ToolGroup[] = [];
    const tools = this.tools.filter((t) => t.source !== 'agent');

    for (const server of this.mcpServers) {
      const serverTools = tools.filter(
        (t) => t.source === 'mcp' && t.source_id === server.id
      );
      groups.push({
        id: server.id,
        name: server.name,
        type: 'mcp',
        server,
        tools: serverTools,
        collapsed: this.collapsedGroups.has(server.id),
      });
    }

    const httpTools = tools.filter((t) => t.source === 'http');
    if (httpTools.length > 0) {
      groups.push({
        id: 'http',
        name: 'HTTP Tools',
        type: 'http',
        tools: httpTools,
        collapsed: this.collapsedGroups.has('http'),
      });
    }

    const builtinTools = tools.filter((t) => t.source === 'builtin');
    if (builtinTools.length > 0 || this.mode === 'scoped') {
      // Only show Built-in by default if there are tools or in scoped mode (where we always might want to override)
      if (builtinTools.length > 0) {
        groups.push({
          id: 'builtin',
          name: 'Built-in',
          type: 'builtin',
          tools: builtinTools,
          collapsed: this.collapsedGroups.has('builtin'),
        });
      }
    }

    return groups;
  }

  private _adapterGroupName(adapter: string | undefined): string {
    return nativeAdapterGroupName(adapter);
  }

  private _getNativeToolGroups(): ToolGroup[] {
    const groups: ToolGroup[] = [];
    const tools = this.tools.filter((t) => t.source === 'agent');
    const byAdapter = new Map<string, ToolWithRules[]>();

    for (const tool of tools) {
      const adapters =
        tool.adapters && tool.adapters.length > 0
          ? tool.adapters
          : [SEEN_FROM_AGENTS_LABEL];
      const seen = new Set<string>();
      for (const adapter of adapters) {
        const name = this._adapterGroupName(adapter);
        if (seen.has(name)) {
          continue;
        }
        seen.add(name);
        const list = byAdapter.get(name) || [];
        list.push(tool);
        byAdapter.set(name, list);
      }
    }

    const extraNames = [...byAdapter.keys()]
      .filter(
        (name) =>
          !(NATIVE_ADAPTER_LABELS as readonly string[]).includes(name) &&
          name !== SEEN_FROM_AGENTS_LABEL
      )
      .sort((a, b) => a.localeCompare(b));
    const order = [
      ...NATIVE_ADAPTER_LABELS,
      ...extraNames,
      SEEN_FROM_AGENTS_LABEL,
    ];

    for (const name of order) {
      const adapterTools = byAdapter.get(name);
      if (!adapterTools || adapterTools.length === 0) {
        continue;
      }
      const id = `agent:${name}`;
      groups.push({
        id,
        name,
        type: 'agent',
        tools: adapterTools,
        collapsed: this.collapsedGroups.has(id),
      });
    }

    return groups;
  }

  private _toggleGroup(groupId: string) {
    const updated = new Set(this.collapsedGroups);
    if (updated.has(groupId)) {
      updated.delete(groupId);
    } else {
      updated.add(groupId);
    }
    this.collapsedGroups = updated;
    this._hasAutoCollapsed = true;
    try {
      sessionStorage.setItem(
        'preloopCollapsedGroups',
        JSON.stringify([...updated])
      );
    } catch {}
  }

  private _handleToggleExpand(e: CustomEvent) {
    const key = this._getToolKey(e.detail.tool);
    const updated = new Set(this.expandedTools);
    if (updated.has(key)) {
      updated.delete(key);
    } else {
      updated.add(key);
    }
    this.expandedTools = updated;
    try {
      sessionStorage.setItem(
        'preloopExpandedTools',
        JSON.stringify([...updated])
      );
    } catch {}
  }

  private _renderToolGroup(group: ToolGroup) {
    const isEnabled = (t: ToolWithRules) =>
      this.mode === 'scoped' && t.name in this.toolEnabledOverrides
        ? this.toolEnabledOverrides[t.name]
        : t.is_enabled;

    const enabledCount = group.tools.filter(isEnabled).length;
    const totalCount = group.tools.length;

    return html`
      <div class="tool-group">
        <div class="section-header" @click=${() => this._toggleGroup(group.id)}>
          <sl-icon
            class="section-icon ${!group.collapsed ? 'open' : ''}"
            name="chevron-right"
          ></sl-icon>
          <span class="section-title">${group.name}</span>
          <span class="section-meta">
            ${
              group.type === 'agent'
                ? `${enabledCount}/${totalCount}`
                : `${enabledCount}/${totalCount} enabled`
            }
          </span>
          <div class="section-line"></div>
          ${
            group.type === 'mcp' && group.server && this.mode === 'global'
              ? html`
                  <div
                    class="section-actions"
                    @click=${(e: Event) => e.stopPropagation()}
                  >
                    <sl-tooltip content="Scan for new tools">
                      <sl-icon-button
                        name="arrow-clockwise"
                        label="Scan for new tools"
                        @click=${() =>
                          this.dispatchEvent(
                            new CustomEvent('scan-server', {
                              detail: group.server.id,
                            })
                          )}
                      ></sl-icon-button>
                    </sl-tooltip>
                    <sl-tooltip
                      content=${
                        this.hasDefaultAIModel
                          ? 'Suggest starter policy'
                          : 'Set a default AI model to suggest a starter policy'
                      }
                    >
                      <sl-icon-button
                        name="magic"
                        label="Suggest starter policy"
                        @click=${() =>
                          this.dispatchEvent(
                            new CustomEvent('suggest-starter-policy', {
                              detail: group.server.id,
                            })
                          )}
                      ></sl-icon-button>
                    </sl-tooltip>
                    <sl-tooltip content="Edit server">
                      <sl-icon-button
                        name="pencil"
                        @click=${() =>
                          this.dispatchEvent(
                            new CustomEvent('edit-server', {
                              detail: group.server,
                            })
                          )}
                      ></sl-icon-button>
                    </sl-tooltip>
                    <sl-tooltip content="Delete server">
                      <sl-icon-button
                        name="trash"
                        @click=${() => {
                          if (
                            confirm(
                              `Delete MCP server "${group.name}" and all its tools?`
                            )
                          ) {
                            this.dispatchEvent(
                              new CustomEvent('delete-server', {
                                detail: group.server.id,
                              })
                            );
                          }
                        }}
                      ></sl-icon-button>
                    </sl-tooltip>
                  </div>
                `
              : ''
          }
        </div>

        ${
          !group.collapsed
            ? html`
                <div class="tool-list">
                  ${
                    group.tools.length === 0
                      ? html`<div
                          style="padding: var(--sl-spacing-small); color: var(--sl-color-neutral-400); font-size: var(--sl-font-size-small);"
                        >
                          No tools${this.filterText ? ' matching filter' : ''}.
                          ${
                            group.type === 'mcp' && this.mode === 'global'
                              ? html`<sl-button
                                  size="small"
                                  variant="text"
                                  @click=${() =>
                                    this.dispatchEvent(
                                      new CustomEvent('scan-server', {
                                        detail: group.id,
                                      })
                                    )}
                                  >Scan for tools</sl-button
                                >`
                              : ''
                          }
                        </div>`
                      : repeat(
                          group.tools,
                          (tool) => this._getToolKey(tool),
                          (tool) => {
                            // determine rules
                            const hasScopedRules =
                              tool.name in this.scopedToolRules;
                            const rules =
                              this.mode === 'scoped' && hasScopedRules
                                ? this.scopedToolRules[tool.name]
                                : tool.access_rules || [];

                            const rulesInherited =
                              this.mode === 'scoped' && !hasScopedRules;
                            const toolIsEnabled =
                              this.mode === 'scoped' &&
                              tool.name in this.toolEnabledOverrides
                                ? this.toolEnabledOverrides[tool.name]
                                : tool.is_enabled;

                            return html`
                              <tool-list-item
                                .mode=${this.mode}
                                .tool=${{ ...tool, is_enabled: toolIsEnabled }}
                                .usageStat=${this.toolStats[tool.name] || null}
                                .accessRules=${rules}
                                .rulesInherited=${rulesInherited}
                                .policies=${this.approvalPolicies}
                                .features=${this.features}
                                .expanded=${this.expandedTools.has(
                                  this._getToolKey(tool)
                                )}
                                @toggle-expand=${this._handleToggleExpand}
                              ></tool-list-item>
                            `;
                          }
                        )
                  }
                </div>
              `
            : ''
        }
      </div>
    `;
  }

  render() {
    const groups = this._getToolGroups();
    return html`
      <div class="tool-groups">
        ${
          groups.length === 0
            ? html`<div
                style="padding: 2rem; text-align: center; color: var(--sl-color-neutral-400);"
              >
                ${
                  this.family === 'native'
                    ? 'No native tools match these filters.'
                    : this.mode === 'global'
                      ? 'No tools found. Add an MCP server to get started.'
                      : 'No managed tools found for this scope.'
                }
              </div>`
            : repeat(
                groups,
                (group) => group.id,
                (group) => this._renderToolGroup(group)
              )
        }
      </div>
    `;
  }
}
