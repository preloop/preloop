import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { classMap } from 'lit/directives/class-map.js';

import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';

import consoleStyles from '../styles/console-styles.css?inline';

export const BLANK_PRESET_ID = 'blank';

export const PRESET_GROUP_LABELS = {
  yours: 'Your presets',
  tracker: 'Tracker automation',
  scheduled: 'Scheduled review',
  security: 'Security and compliance',
} as const;

export interface FlowPresetRecord {
  id?: string;
  name?: string;
  description?: string;
  icon?: string;
  account_id?: string | null;
  slug?: string;
  trigger_event_types?: string[] | null;
  allowed_mcp_tools?: unknown[] | null;
  git_clone_config?: {
    enabled?: boolean;
    create_pull_request?: boolean;
  } | null;
}

export interface PresetChip {
  key: string;
  label: string;
}

export interface PresetGroup {
  id: string;
  label: string;
  presets: FlowPresetRecord[];
}

/** Derive a grouping slug. Presets from the API have no slug field (loader-internal). */
export function presetSlug(preset: FlowPresetRecord): string {
  if (typeof preset.slug === 'string' && preset.slug.trim()) {
    return preset.slug.trim().toLowerCase();
  }
  return String(preset.name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

function isSecuritySlug(slug: string): boolean {
  return (
    slug.startsWith('sbom-') ||
    slug === 'release-security-audit' ||
    slug.startsWith('component-due-diligence')
  );
}

/**
 * Group catalog presets client-side until YAML carries `category`.
 *
 * Account presets first. Catalog order is kept inside each group.
 */
export function presetGroups(presets: FlowPresetRecord[]): PresetGroup[] {
  const yours: FlowPresetRecord[] = [];
  const tracker: FlowPresetRecord[] = [];
  const scheduled: FlowPresetRecord[] = [];
  const security: FlowPresetRecord[] = [];

  for (const preset of presets) {
    if (preset.account_id) {
      yours.push(preset);
      continue;
    }
    const slug = presetSlug(preset);
    if (isSecuritySlug(slug)) {
      security.push(preset);
      continue;
    }
    if (preset.trigger_event_types && preset.trigger_event_types.length > 0) {
      tracker.push(preset);
      continue;
    }
    scheduled.push(preset);
  }

  const groups: PresetGroup[] = [];
  if (yours.length) {
    groups.push({
      id: 'yours',
      label: PRESET_GROUP_LABELS.yours,
      presets: yours,
    });
  }
  if (tracker.length) {
    groups.push({
      id: 'tracker',
      label: PRESET_GROUP_LABELS.tracker,
      presets: tracker,
    });
  }
  if (scheduled.length) {
    groups.push({
      id: 'scheduled',
      label: PRESET_GROUP_LABELS.scheduled,
      presets: scheduled,
    });
  }
  if (security.length) {
    groups.push({
      id: 'security',
      label: PRESET_GROUP_LABELS.security,
      presets: security,
    });
  }
  return groups;
}

export function presetChips(preset: FlowPresetRecord): PresetChip[] {
  const chips: PresetChip[] = [];
  if (preset.trigger_event_types && preset.trigger_event_types.length > 0) {
    chips.push({ key: 'tracker', label: 'Tracker' });
  }
  chips.push({ key: 'model', label: 'Model' });
  const tools = preset.allowed_mcp_tools || [];
  if (tools.length > 0) {
    chips.push({
      key: 'tools',
      label: tools.length === 1 ? '1 tool' : `${tools.length} tools`,
    });
  }
  if (preset.git_clone_config?.enabled) {
    chips.push({ key: 'clone', label: 'Clones repo' });
  }
  if (preset.git_clone_config?.create_pull_request) {
    chips.push({ key: 'pr', label: 'Opens PRs' });
  }
  return chips;
}

export function firstSentence(description: string | null | undefined): string {
  const text = (description || '').replace(/\s+/g, ' ').trim();
  if (!text) {
    return '';
  }
  const match = text.match(/^.*?[.!?](?:\s|$)/);
  return (match ? match[0] : text).trim();
}

@customElement('preloop-flow-preset-picker')
export class PreloopFlowPresetPicker extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }

      .header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-small);
      }

      .label {
        font-size: var(--console-text-card-title);
        font-weight: 600;
        color: var(--console-body-color);
      }

      .search {
        width: 14rem;
        max-width: 50%;
        margin-bottom: 0;
      }

      .listbox:focus-visible {
        outline: 2px solid var(--sl-color-primary-600);
        outline-offset: 2px;
      }

      .group-label {
        font-size: var(--console-text-meta);
        font-weight: 600;
        color: var(--console-meta-color);
        padding: var(--sl-spacing-small) 0 var(--sl-spacing-2x-small);
      }

      .row {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr) auto auto;
        grid-template-rows: auto auto;
        column-gap: var(--sl-spacing-small);
        row-gap: 2px;
        align-items: center;
        padding: var(--sl-spacing-small) var(--sl-spacing-2x-small);
        border-bottom: 1px solid var(--console-hairline);
        cursor: pointer;
      }

      .row:last-child {
        border-bottom: none;
      }

      .row:hover:not(.selected) {
        background: var(--console-hover-tint);
      }

      .row.selected {
        background: color-mix(
          in srgb,
          var(--sl-color-primary-600) 16%,
          transparent
        );
      }

      .row.active {
        box-shadow: inset 0 0 0 2px var(--sl-color-primary-600);
      }

      .row-icon {
        grid-column: 1;
        grid-row: 1 / span 2;
        color: var(--console-meta-color);
        font-size: 1rem;
      }

      .row.selected .row-icon {
        color: var(--sl-color-primary-800);
      }

      .row-name {
        grid-column: 2;
        grid-row: 1;
        font-size: var(--console-text-body);
        font-weight: 500;
        color: var(--console-body-color);
        min-width: 0;
      }

      .row.selected .row-name {
        color: var(--sl-color-primary-800);
      }

      .row-desc {
        grid-column: 2 / span 2;
        grid-row: 2;
        display: -webkit-box;
        -webkit-line-clamp: 1;
        -webkit-box-orient: vertical;
        overflow: hidden;
        font-size: var(--console-text-meta);
        color: var(--console-meta-color);
      }

      .chips {
        grid-column: 3;
        grid-row: 1;
        display: flex;
        flex-wrap: wrap;
        justify-content: flex-end;
        gap: 4px;
      }

      .row-check {
        grid-column: 4;
        grid-row: 1 / span 2;
        color: var(--sl-color-primary-800);
        font-size: 1rem;
      }

      .missing {
        margin: 0 0 var(--sl-spacing-small);
        color: var(--sl-color-neutral-600);
        font-size: var(--console-text-meta);
      }

      .summary {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
        font-size: var(--console-text-body);
        color: var(--console-body-color);
      }

      .summary a {
        color: var(--console-link-color);
      }
    `,
  ];

  @property({ type: Array })
  presets: FlowPresetRecord[] = [];

  /** `''` none, `blank` for Blank flow, otherwise a preset id. */
  @property({ type: String })
  selectedId = '';

  @property({ type: Boolean })
  collapsed = false;

  @state()
  private search = '';

  @state()
  private activeId: string = BLANK_PRESET_ID;

  willUpdate(changedProperties: Map<string | number | symbol, unknown>): void {
    if (
      changedProperties.has('selectedId') &&
      this.selectedId &&
      this.visibleOptionIds().includes(this.selectedId)
    ) {
      this.activeId = this.selectedId;
    }
  }

  private filteredGroups(): PresetGroup[] {
    const query = this.search.trim().toLowerCase();
    const groups = presetGroups(this.presets);
    if (!query) {
      return groups;
    }
    return groups
      .map((group) => ({
        ...group,
        presets: group.presets.filter((preset) => {
          const name = (preset.name || '').toLowerCase();
          const description = (preset.description || '').toLowerCase();
          return name.includes(query) || description.includes(query);
        }),
      }))
      .filter((group) => group.presets.length > 0);
  }

  private visibleOptionIds(): string[] {
    const ids = [BLANK_PRESET_ID];
    for (const group of this.filteredGroups()) {
      for (const preset of group.presets) {
        if (preset.id) {
          ids.push(preset.id);
        }
      }
    }
    return ids;
  }

  private isUnknownSelected(): boolean {
    return Boolean(
      this.selectedId &&
      this.selectedId !== BLANK_PRESET_ID &&
      !this.presets.some((preset) => preset.id === this.selectedId)
    );
  }

  private selectedPreset(): FlowPresetRecord | undefined {
    if (!this.selectedId || this.selectedId === BLANK_PRESET_ID) {
      return undefined;
    }
    return this.presets.find((preset) => preset.id === this.selectedId);
  }

  private emitSelect(presetId: string): void {
    this.dispatchEvent(
      new CustomEvent('preset-select', {
        detail: { presetId },
        bubbles: true,
        composed: true,
      })
    );
  }

  private requestChange(): void {
    this.dispatchEvent(
      new CustomEvent('preset-change-request', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private handleSearch(event: Event): void {
    const target = event.target as HTMLInputElement;
    this.search = target.value || '';
  }

  private handleListKeydown(event: KeyboardEvent): void {
    const ids = this.visibleOptionIds();
    if (ids.length === 0) {
      return;
    }
    const current = ids.includes(this.activeId) ? this.activeId : ids[0];
    const index = ids.indexOf(current);

    if (event.key === 'ArrowDown') {
      event.preventDefault();
      this.activeId = ids[Math.min(index + 1, ids.length - 1)];
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      this.activeId = ids[Math.max(index - 1, 0)];
    } else if (event.key === 'Home') {
      event.preventDefault();
      this.activeId = ids[0];
    } else if (event.key === 'End') {
      event.preventDefault();
      this.activeId = ids[ids.length - 1];
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      this.emitSelect(current);
    }
  }

  private renderChips(preset: FlowPresetRecord) {
    const chips = presetChips(preset);
    if (chips.length === 0) {
      return nothing;
    }
    return html`
      <div class="chips">
        ${chips.map(
          (chip) => html` <sl-badge class="chip" pill>${chip.label}</sl-badge> `
        )}
      </div>
    `;
  }

  private renderRow(options: {
    optionId: string;
    icon: string;
    name: string;
    description: string;
    preset?: FlowPresetRecord;
  }) {
    const selected = this.selectedId === options.optionId;
    const active = this.activeId === options.optionId;
    return html`
      <div
        id=${`preset-option-${options.optionId}`}
        class=${classMap({ row: true, selected, active })}
        role="option"
        data-preset-id=${options.optionId}
        aria-selected=${selected ? 'true' : 'false'}
        @click=${() => this.emitSelect(options.optionId)}
      >
        <sl-icon class="row-icon" name=${options.icon}></sl-icon>
        <div class="row-name">${options.name}</div>
        ${options.preset ? this.renderChips(options.preset) : html`<div></div>`}
        ${
          selected
            ? html`<sl-icon class="row-check" name="check-lg"></sl-icon>`
            : html`<div></div>`
        }
        <div class="row-desc">${options.description}</div>
      </div>
    `;
  }

  private renderCollapsed() {
    const preset = this.selectedPreset();
    if (this.selectedId === BLANK_PRESET_ID || !preset) {
      return html`
        <div class="summary">
          <span>Blank flow.</span>
          <sl-button
            variant="text"
            size="small"
            @click=${() => this.requestChange()}
          >
            Change
          </sl-button>
        </div>
      `;
    }
    return html`
      <div class="summary">
        <span>
          Started from
          <a href="/console/flows">${preset.name}</a>.
        </span>
        <sl-button
          variant="text"
          size="small"
          @click=${() => this.requestChange()}
        >
          Change
        </sl-button>
      </div>
    `;
  }

  private renderList() {
    const groups = this.filteredGroups();
    const activeId = this.visibleOptionIds().includes(this.activeId)
      ? this.activeId
      : BLANK_PRESET_ID;

    return html`
      <div class="header">
        <div class="label">Start from</div>
        <sl-input
          class="search"
          size="small"
          clearable
          placeholder="Search presets"
          .value=${this.search}
          @sl-input=${this.handleSearch}
          @sl-clear=${() => {
            this.search = '';
          }}
        ></sl-input>
      </div>
      ${
        this.isUnknownSelected()
          ? html`<p class="missing">That preset is no longer available.</p>`
          : nothing
      }
      <div
        class="listbox"
        role="listbox"
        tabindex="0"
        aria-label="Start from"
        aria-activedescendant=${`preset-option-${activeId}`}
        @keydown=${this.handleListKeydown}
      >
        ${this.renderRow({
          optionId: BLANK_PRESET_ID,
          icon: 'pencil',
          name: 'Blank flow',
          description: 'Write the prompt and choose the trigger yourself.',
        })}
        ${groups.map(
          (group) => html`
            <div class="group-label">${group.label}</div>
            ${group.presets.map((preset) =>
              this.renderRow({
                optionId: preset.id || '',
                icon: preset.icon || 'gear',
                name: preset.name || 'Untitled preset',
                description: firstSentence(preset.description),
                preset,
              })
            )}
          `
        )}
      </div>
    `;
  }

  render() {
    if (this.collapsed && !this.isUnknownSelected()) {
      return this.renderCollapsed();
    }
    return this.renderList();
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'preloop-flow-preset-picker': PreloopFlowPresetPicker;
  }
}
