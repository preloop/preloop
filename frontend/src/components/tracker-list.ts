import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import { Router } from '@vaadin/router';
import { fetchWithAuth } from '../api.js';
import { formatRelativeTime } from '../utils/date';
import {
  effectiveViewMode,
  loadViewMode,
  saveViewMode,
  subscribeNarrowViewport,
  type ListViewMode,
  type NarrowViewportSubscription,
} from '../utils/view-mode';
import type { ResourceAction } from './resource-actions.ts';
import consoleStyles from '../styles/console-styles.css?inline';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import './tracker-item.ts';
import './list-toolbar.ts';
import './resource-actions.ts';
import type { Tracker } from './tracker-item.ts';
import { consoleDialogStyles } from '../styles/console-dialog';

const VIEW_MODE_KEY = 'preloop.trackers.view_mode';

const KIND_LABELS: Record<string, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
  jira: 'Jira',
};

export function trackerKindLabel(kind: string): string {
  return KIND_LABELS[kind.toLowerCase()] || kind;
}

export function trackerProjectsCount(tracker: Tracker): number | 'all' {
  const rules = tracker.scope_rules ?? [];
  const projectIncludes = rules.filter(
    (rule) =>
      rule.scope_type?.toUpperCase() === 'PROJECT' &&
      rule.rule_type?.toUpperCase() === 'INCLUDE'
  );
  if (projectIncludes.length > 0) {
    return projectIncludes.length;
  }
  return 'all';
}

export function trackerLastCheckedAt(tracker: Tracker): string | null {
  return tracker.last_validation ?? null;
}

export function filterTrackers(
  trackers: Tracker[],
  search: string,
  kind: string
): Tracker[] {
  const query = search.trim().toLowerCase();
  return trackers.filter((tracker) => {
    if (kind && tracker.tracker_type !== kind) {
      return false;
    }
    if (!query) {
      return true;
    }
    const haystack = [
      tracker.name,
      tracker.tracker_type,
      trackerKindLabel(tracker.tracker_type),
      tracker.url ?? '',
    ]
      .join(' ')
      .toLowerCase();
    return haystack.includes(query);
  });
}

@customElement('tracker-list')
export class TrackerList extends LitElement {
  @state()
  private trackers: Tracker[] = [];

  @state()
  private isLoading = false;

  @state()
  private error: string | null = null;

  @state()
  private search = '';

  @state()
  private kindFilter = '';

  @state()
  private currentView: ListViewMode = loadViewMode(VIEW_MODE_KEY);

  @state()
  private narrowViewport = false;

  @state()
  private trackerPendingDelete: Tracker | null = null;

  private narrowViewportSubscription: NarrowViewportSubscription | null = null;

  connectedCallback() {
    super.connectedCallback();
    this.narrowViewportSubscription = subscribeNarrowViewport((narrow) => {
      this.narrowViewport = narrow;
    });
    this.narrowViewport = this.narrowViewportSubscription.matches;
    this.fetchTrackers();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.narrowViewportSubscription?.disconnect();
    this.narrowViewportSubscription = null;
  }

  async fetchTrackers() {
    this.isLoading = true;
    this.error = null;
    try {
      const response = await fetchWithAuth('/api/v1/trackers');
      if (!response.ok) {
        throw new Error('Failed to fetch trackers');
      }
      this.trackers = await response.json();
      this.dispatchEvent(
        new CustomEvent('trackers-changed', {
          detail: { count: this.trackers.length, trackers: this.trackers },
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'An unknown error occurred';
    } finally {
      this.isLoading = false;
    }
  }

  private get visibleTrackers(): Tracker[] {
    return filterTrackers(this.trackers, this.search, this.kindFilter);
  }

  private get effectiveView(): ListViewMode {
    return effectiveViewMode(this.currentView, this.narrowViewport);
  }

  private get kindOptions(): string[] {
    return [...new Set(this.trackers.map((tracker) => tracker.tracker_type))]
      .filter(Boolean)
      .sort((a, b) => trackerKindLabel(a).localeCompare(trackerKindLabel(b)));
  }

  private get resultsLabel(): string {
    const shown = this.visibleTrackers.length;
    const total = this.trackers.length;
    const noun = total === 1 ? 'tracker' : 'trackers';
    if (shown === total) {
      return `${shown} ${noun}`;
    }
    return `${shown} of ${total} ${noun}`;
  }

  private _handleTrackerEdit(event: CustomEvent) {
    this.dispatchEvent(
      new CustomEvent('tracker-edit', {
        detail: event.detail,
        bubbles: true,
        composed: true,
      })
    );
  }

  private _handleAddTracker() {
    this.dispatchEvent(
      new CustomEvent('tracker-add-request', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private _editTracker(tracker: Tracker) {
    this.dispatchEvent(
      new CustomEvent('tracker-edit', {
        detail: { tracker },
        bubbles: true,
        composed: true,
      })
    );
  }

  private _requestDelete(tracker: Tracker) {
    this.trackerPendingDelete = tracker;
  }

  private _cancelDelete() {
    this.trackerPendingDelete = null;
  }

  private async _confirmDelete() {
    const tracker = this.trackerPendingDelete;
    this.trackerPendingDelete = null;
    if (!tracker) return;
    await this._deleteTracker(tracker.id);
  }

  private async _handleTrackerDeleted(event: CustomEvent) {
    const { id } = event.detail;
    await this._deleteTracker(id);
  }

  private async _deleteTracker(id: string) {
    try {
      const response = await fetchWithAuth(`/api/v1/trackers/${id}`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        throw new Error('Failed to delete tracker');
      }
      await this.fetchTrackers();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'An unknown error occurred';
    }
  }

  private handleSearchChange(event: CustomEvent<{ value: string }>) {
    this.search = event.detail.value;
  }

  private handleViewChange(event: CustomEvent<{ value: ListViewMode }>) {
    this.currentView = event.detail.value;
    saveViewMode(VIEW_MODE_KEY, event.detail.value);
  }

  private handleKindChange(event: Event) {
    const select = event.target as HTMLElement & { value: string | string[] };
    const value = Array.isArray(select.value) ? select.value[0] : select.value;
    this.kindFilter = value || '';
  }

  private handleRowClick(event: MouseEvent, tracker: Tracker) {
    const path = event.composedPath();
    const blocked = path.some((node) => {
      if (!(node instanceof HTMLElement)) return false;
      const tag = node.tagName.toLowerCase();
      return (
        tag === 'a' ||
        tag === 'sl-button' ||
        tag === 'resource-actions' ||
        tag === 'sl-dropdown'
      );
    });
    if (blocked) return;
    Router.go(`/console/trackers/${tracker.id}`);
  }

  private rowActions(tracker: Tracker): ResourceAction[] {
    return [
      {
        id: 'edit',
        label: 'Edit',
        icon: 'pencil',
        onClick: () => this._editTracker(tracker),
      },
      {
        id: 'delete',
        label: 'Delete',
        icon: 'trash',
        variant: 'danger',
        onClick: () => this._requestDelete(tracker),
      },
    ];
  }

  static styles = [
    consoleDialogStyles,
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }

      .toolbar-wrap {
        margin-bottom: var(--sl-spacing-medium);
      }

      .tracker-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
        gap: var(--sl-spacing-large);
        padding-top: var(--sl-spacing-medium);
      }

      .loading-indicator {
        display: flex;
        justify-content: center;
        align-items: center;
        height: 100px;
      }

      .empty-state-wrapper {
        display: flex;
        justify-content: center;
        width: 100%;
        margin-top: var(--sl-spacing-large);
      }

      .empty-card {
        width: 100%;
        max-width: 580px;
      }

      .empty-card::part(base) {
        border: 1px solid
          color-mix(in srgb, var(--sl-color-primary-600) 35%, transparent);
        box-shadow: var(--sl-shadow-large);
        border-radius: var(--sl-border-radius-large);
        overflow: hidden;
      }

      .empty-card-body {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        padding: var(--sl-spacing-large);
      }

      .empty-icon-circle {
        width: 72px;
        height: 72px;
        border-radius: 50%;
        background: color-mix(
          in srgb,
          var(--sl-color-primary-600) 15%,
          transparent
        );
        color: var(--sl-color-primary-600);
        display: flex;
        align-items: center;
        justify-content: center;
        margin-bottom: var(--sl-spacing-medium);
      }

      .empty-icon-circle sl-icon {
        font-size: 2.5rem;
      }

      .empty-card-title {
        margin: 0 0 var(--sl-spacing-2x-small);
        font-size: 1.25rem;
        font-weight: 700;
        color: var(--sl-color-neutral-900);
      }

      .empty-card-desc {
        margin: 0 0 var(--sl-spacing-large);
        max-width: 440px;
        font-size: 0.95rem;
        line-height: 1.55;
        color: var(--sl-color-neutral-600);
      }

      .empty-cta-btn {
        width: 100%;
        max-width: 280px;
      }

      .filter-empty {
        color: var(--console-meta-color);
        font-size: var(--console-text-body);
        padding: var(--sl-spacing-large) 0;
      }

      .trackers-table {
        table-layout: fixed;
        width: 100%;
      }

      .trackers-table th.actions-cell,
      .trackers-table td.actions-cell {
        width: 72px;
        text-align: right;
        padding-left: var(--sl-spacing-x-small);
        padding-right: var(--sl-spacing-x-small);
        overflow: visible;
      }

      .actions-cell resource-actions::part(container) {
        overflow: visible;
      }

      .tracker-row {
        cursor: pointer;
      }

      .row-link {
        color: var(--console-link-color);
        display: block;
        font-weight: var(--sl-font-weight-semibold);
        overflow: hidden;
        text-decoration: none;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .row-link:hover,
      .row-link:focus-visible {
        text-decoration: underline;
      }

      .row-subtitle {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .muted-cell {
        color: var(--console-meta-color);
      }

      .numeric {
        font-variant-numeric: tabular-nums;
        text-align: right;
      }

      .visually-hidden {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
      }

      sl-select::part(form-control-label) {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
        border: 0;
      }
    `,
  ];

  private renderToolbar() {
    return html`
      <div class="toolbar-wrap">
        <list-toolbar
          .search=${this.search}
          searchPlaceholder="Search trackers"
          toggleLabel="Trackers view"
          .view=${this.currentView}
          @search-change=${this.handleSearchChange}
          @view-change=${this.handleViewChange}
        >
          <sl-select
            class="kind-filter"
            label="Kind"
            clearable
            placeholder="All kinds"
            .value=${this.kindFilter}
            @sl-change=${this.handleKindChange}
          >
            ${this.kindOptions.map(
              (kind) =>
                html`<sl-option value=${kind}
                  >${trackerKindLabel(kind)}</sl-option
                >`
            )}
          </sl-select>
          <span slot="count">${this.resultsLabel}</span>
        </list-toolbar>
      </div>
    `;
  }

  private renderListView(trackers: Tracker[]) {
    return html`
      <sl-card class="table-card">
        <table class="styled-table trackers-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Kind</th>
              <th class="numeric">Projects</th>
              <th>Sync</th>
              <th>Last checked</th>
              <th class="actions-cell">
                <span class="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            ${repeat(
              trackers,
              (tracker) => tracker.id,
              (tracker) => this.renderListRow(tracker)
            )}
          </tbody>
        </table>
      </sl-card>
    `;
  }

  private renderListRow(tracker: Tracker) {
    const projects = trackerProjectsCount(tracker);
    const lastChecked = trackerLastCheckedAt(tracker);
    const connected = tracker.is_valid !== false;
    return html`
      <tr
        class="tracker-row"
        data-tracker-id=${tracker.id}
        @click=${(event: MouseEvent) => this.handleRowClick(event, tracker)}
      >
        <td>
          <a class="row-link" href=${`/console/trackers/${tracker.id}`}
            >${tracker.name}</a
          >
          ${
            tracker.url
              ? html`<div class="row-subtitle" title=${tracker.url}>
                  ${tracker.url}
                </div>`
              : nothing
          }
        </td>
        <td>
          <sl-badge variant="neutral" pill
            >${trackerKindLabel(tracker.tracker_type)}</sl-badge
          >
        </td>
        <td class="numeric">
          ${
            projects === 'all'
              ? html`<span class="muted-cell">All</span>`
              : projects
          }
        </td>
        <td>
          <sl-badge variant=${connected ? 'success' : 'danger'} pill>
            ${connected ? 'Connected' : 'Action Required'}
          </sl-badge>
        </td>
        <td class="muted-cell">
          ${lastChecked ? formatRelativeTime(lastChecked) : nothing}
        </td>
        <td class="actions-cell">
          <div
            class="row-actions"
            @click=${(event: Event) => event.stopPropagation()}
          >
            <resource-actions
              .actions=${this.rowActions(tracker)}
              menu-only
            ></resource-actions>
          </div>
        </td>
      </tr>
    `;
  }

  private renderCardsView(trackers: Tracker[]) {
    return html`
      <div class="tracker-grid">
        ${repeat(
          trackers,
          (tracker) => tracker.id,
          (tracker) =>
            html`<tracker-item
              .tracker=${tracker}
              @tracker-deleted=${this._handleTrackerDeleted}
              @tracker-edit=${this._handleTrackerEdit}
            ></tracker-item>`
        )}
      </div>
    `;
  }

  private renderDeleteDialog() {
    return html`
      <sl-dialog
        label="Confirm Deletion"
        ?open=${this.trackerPendingDelete !== null}
        @sl-hide=${this._cancelDelete}
      >
        Are you sure you want to delete the tracker
        "${this.trackerPendingDelete?.name}"?
        <sl-button slot="footer" @click=${this._cancelDelete}>Cancel</sl-button>
        <sl-button slot="footer" variant="danger" @click=${this._confirmDelete}
          >Delete</sl-button
        >
      </sl-dialog>
    `;
  }

  render() {
    if (this.isLoading && this.trackers.length === 0) {
      return html`<div class="loading-indicator">
        <sl-spinner></sl-spinner>
      </div>`;
    }

    if (this.error) {
      return html`<sl-alert variant="danger" open>
        <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
        <strong>Error:</strong> ${this.error}
      </sl-alert>`;
    }

    if (this.trackers.length === 0) {
      return html`
        <div class="empty-state-wrapper">
          <sl-card class="empty-state empty-card">
            <div class="empty-card-body">
              <div class="empty-icon-circle">
                <sl-icon name="link-45deg"></sl-icon>
              </div>
              <h3 class="empty-card-title">No trackers connected.</h3>
              <p class="empty-card-desc">
                Connect GitHub, GitLab, or Jira to give flows their triggers and
                agents their issue-tracking tools.
              </p>
              <sl-button
                class="empty-cta-btn"
                variant="primary"
                @click=${this._handleAddTracker}
              >
                <sl-icon slot="prefix" name="plus-lg"></sl-icon>
                Add New Tracker
              </sl-button>
            </div>
          </sl-card>
        </div>
      `;
    }

    const visible = this.visibleTrackers;
    const body = this.isLoading
      ? html`<div class="loading-indicator">
          <sl-spinner></sl-spinner>
        </div>`
      : visible.length === 0
        ? html`<div class="filter-empty">No trackers match these filters.</div>`
        : this.effectiveView === 'cards'
          ? this.renderCardsView(visible)
          : this.renderListView(visible);

    return html` ${this.renderToolbar()} ${body} ${this.renderDeleteDialog()} `;
  }
}
