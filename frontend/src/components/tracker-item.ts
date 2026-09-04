import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import { consoleDialogStyles } from '../styles/console-dialog';

export interface Tracker {
  id: string;
  name: string;
  tracker_type: string;
  created: string;
  is_valid: boolean;
  url?: string | null;
  is_active?: boolean;
  last_validation?: string | null;
  last_updated?: string;
  scope_rules?: Array<{
    scope_type: string;
    rule_type: string;
    identifier: string;
  }>;
}

@customElement('tracker-item')
export class TrackerItem extends LitElement {
  @property({ type: Object })
  tracker?: Tracker;

  @state()
  private isConfirmingDelete = false;

  static styles = [
    consoleDialogStyles,
    css`
      .tracker-card {
        width: 250px;
        height: 320px;
        display: flex;
        flex-direction: column;
      }

      .card-content {
        flex-grow: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: var(--sl-spacing-large);
        text-align: center;
        cursor: pointer;
      }

      .card-content:hover {
        background-color: var(--sl-color-neutral-50);
      }

      .tracker-icon {
        font-size: 4rem;
        margin-bottom: var(--sl-spacing-medium);
        color: var(--sl-color-primary-600);
      }

      .tracker-name {
        font-size: var(--sl-font-size-large);
        font-weight: var(--sl-font-weight-semibold);
        margin: 0 0 var(--sl-spacing-x-small) 0;
      }

      .tracker-type {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
        text-transform: capitalize;
        margin: 0;
      }

      .tracker-created {
        font-size: var(--sl-font-size-x-small);
        color: var(--sl-color-neutral-500);
        margin-top: var(--sl-spacing-medium);
      }

      sl-card::part(footer) {
        display: flex;
        justify-content: space-evenly;
        padding: var(--sl-spacing-small);
        border-top: 1px solid var(--sl-color-neutral-200);
      }

      sl-button {
        flex: 1 1 50%;
        margin: 0 var(--sl-spacing-small);
      }
    `,
  ];

  private _requestDeleteConfirmation() {
    this.isConfirmingDelete = true;
  }

  private _cancelDelete() {
    this.isConfirmingDelete = false;
  }

  private _confirmDelete() {
    if (!this.tracker) return;
    const event = new CustomEvent('tracker-deleted', {
      detail: { id: this.tracker.id },
      bubbles: true,
      composed: true,
    });
    this.dispatchEvent(event);
    this.isConfirmingDelete = false;
  }

  private _navigateToDetail() {
    if (!this.tracker) return;
    Router.go(`/console/trackers/${this.tracker.id}`);
  }

  private _editTracker() {
    if (!this.tracker) return;
    const event = new CustomEvent('tracker-edit', {
      detail: { tracker: this.tracker },
      bubbles: true,
      composed: true,
    });
    this.dispatchEvent(event);
  }

  private getTrackerIcon(tracker: Tracker): { name: string; library: string } {
    const type = tracker.tracker_type?.toLowerCase() || '';
    const name = tracker.name?.toLowerCase() || '';

    if (type.includes('jira') || name.includes('jira')) {
      return { name: 'git', library: 'default' };
    }
    if (type.includes('github') || name.includes('github')) {
      return { name: 'github', library: 'default' };
    }
    if (type.includes('gitlab') || name.includes('gitlab')) {
      return { name: 'gitlab', library: 'default' };
    }
    return { name: 'box-seam', library: 'default' };
  }

  render() {
    if (!this.tracker) {
      return html``;
    }

    const createdAt = new Date(this.tracker.created).toLocaleDateString();
    const icon = this.getTrackerIcon(this.tracker);

    return html`
      <sl-dialog
        label="Confirm Deletion"
        ?open=${this.isConfirmingDelete}
        @sl-hide=${this._cancelDelete}
      >
        Are you sure you want to delete the tracker "${this.tracker?.name}"?
        <sl-button slot="footer" @click=${this._cancelDelete}>Cancel</sl-button>
        <sl-button slot="footer" variant="danger" @click=${this._confirmDelete}
          >Delete</sl-button
        >
      </sl-dialog>

      <sl-card class="tracker-card">
        <div class="card-content" @click=${this._navigateToDetail}>
          <sl-icon
            class="tracker-icon"
            name=${icon.name}
            library=${icon.library}
          ></sl-icon>
          <h3 class="tracker-name">${this.tracker.name}</h3>
          <p class="tracker-type">${this.tracker.tracker_type}</p>
          <sl-badge
            variant=${this.tracker.is_valid !== false ? 'success' : 'danger'}
            size="small"
            style="margin-top: var(--sl-spacing-2x-small);"
          >
            ${this.tracker.is_valid !== false ? 'Connected' : 'Action Required'}
          </sl-badge>
          <div class="tracker-created">Created: ${createdAt}</div>
        </div>
        <div slot="footer">
          <sl-button size="small" @click=${this._editTracker} pill
            >Edit</sl-button
          >
          <sl-button
            size="small"
            variant="danger"
            @click=${this._requestDeleteConfirmation}
            pill
            >Delete</sl-button
          >
        </div>
      </sl-card>
    `;
  }
}
