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
      margin-top: var(--sl-spacing-large);
    }

    .empty-card {
      width: 100%;
      max-width: 580px;
      border: 1px solid rgba(139, 92, 246, 0.35);
      box-shadow: 0 12px 40px rgba(139, 92, 246, 0.12);
      border-radius: var(--sl-border-radius-large);
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
      background: radial-gradient(
        circle,
        rgba(139, 92, 246, 0.2) 0%,
        rgba(99, 102, 241, 0.05) 70%
      );
      display: flex;
      align-items: center;
      justify-content: center;
      margin-bottom: var(--sl-spacing-medium);
    }

    .empty-icon-circle sl-icon {
      font-size: 2.5rem;
      color: #8b5cf6;
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
      --sl-color-primary-600: #6366f1;
      --sl-color-primary-700: #4f46e5;
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
              <div class="empty-icon-circle">
                <sl-icon name="link-45deg"></sl-icon>
              </div>
              <h3 class="empty-card-title">
                No trackers connected.
              </h3>
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
