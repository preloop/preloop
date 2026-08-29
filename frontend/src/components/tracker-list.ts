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
      this.dispatchEvent(
        new CustomEvent('trackers-changed', {
          detail: { count: this.trackers.length },
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

    .empty-state-wrapper {
      display: flex;
      justify-content: center;
      width: 100%;
      margin-top: var(--sl-spacing-medium);
    }

    .empty-card {
      width: 100%;
      max-width: 580px;
    }

    .empty-card-body {
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      padding: var(--sl-spacing-large);
    }

    .empty-card-icon {
      font-size: 2.5rem;
      color: var(--sl-color-primary-600);
      margin-bottom: var(--sl-spacing-small);
    }

    .empty-card-title {
      margin: 0 0 var(--sl-spacing-x-small);
      font-size: var(--sl-font-size-large);
      font-weight: var(--sl-font-weight-semibold);
      color: var(--sl-color-neutral-900);
    }

    .empty-card-desc {
      margin: 0 0 var(--sl-spacing-large);
      max-width: 440px;
      line-height: 1.5;
      color: var(--sl-color-neutral-600);
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
        <div class="empty-state-wrapper">
          <sl-card class="empty-state empty-card">
            <div class="empty-card-body">
              <sl-icon class="empty-card-icon" name="link-45deg"></sl-icon>
              <h3 class="empty-card-title">No trackers connected.</h3>
              <p class="empty-card-desc">
                Connect GitHub, GitLab, or Jira to give flows their triggers and
                agents their issue-tracking tools.
              </p>
              <sl-button variant="primary" @click=${this._handleAddTracker}>
                <sl-icon slot="prefix" name="plus-lg"></sl-icon>
                Add New Tracker
              </sl-button>
            </div>
          </sl-card>
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
