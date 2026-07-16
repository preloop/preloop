import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { repeat } from 'lit/directives/repeat.js';
import { fetchWithAuth } from '../api.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import './tracker-item.ts';
import type { Tracker } from './tracker-item.ts';

@customElement('tracker-list')
export class TrackerList extends LitElement {
  @state()
  private trackers: Tracker[] = [];

  @state()
  private isLoading = false;

  @state()
  private error: string | null = null;

  connectedCallback() {
    super.connectedCallback();
    this.fetchTrackers();
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
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'An unknown error occurred';
    } finally {
      this.isLoading = false;
    }
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

  private async _handleTrackerDeleted(event: CustomEvent) {
    const { id } = event.detail;
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

  static styles = css`
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

    .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: var(--sl-spacing-medium);
      padding: var(--sl-spacing-2x-large) var(--sl-spacing-large);
      text-align: center;
      color: var(--sl-color-neutral-500);
    }

    .empty-state sl-icon {
      font-size: 2rem;
    }
  `;

  render() {
    if (this.isLoading) {
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
        <div class="empty-state">
          <sl-icon name="link-45deg"></sl-icon>
          <div>
            No trackers connected. Connect GitHub, GitLab, or Jira to give flows
            their triggers and agents their issue-tracking tools.
          </div>
          <sl-button variant="primary" @click=${this._handleAddTracker}>
            <sl-icon slot="prefix" name="plus-lg"></sl-icon>
            Add New Tracker
          </sl-button>
        </div>
      `;
    }

    return html`
      <div class="tracker-grid">
        ${repeat(
          this.trackers,
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
}
