import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '../../components/view-header.ts';
import {
  getAccountOrganization,
  getRunners,
  updateAccountOrganization,
  type RunnerRecord,
} from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { formatLocalDateTime } from '../../utils/date';
import { buildRunnerPoolOptions } from '../../utils/runner-pool';
import consoleStyles from '../../styles/console-styles.css?inline';

@customElement('runners-view')
export class RunnersView extends LitElement {
  @state()
  private runners: RunnerRecord[] = [];

  @state()
  private loading = true;

  @state()
  private error: string | null = null;

  @state()
  private defaultRunnerPool: string | null = null;

  @state()
  private savingDefault = false;

  @state()
  private defaultError: string | null = null;

  private unsubscribe?: () => void;

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
        font-size: 14px;
      }
      .muted {
        color: var(--sl-color-neutral-500);
        font-size: 13px;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th,
      td {
        text-align: left;
        padding: 8px 10px;
        border-bottom: 1px solid var(--sl-color-neutral-200);
        font-size: 14px;
        vertical-align: top;
      }
      th {
        font-size: 13px;
        font-weight: 600;
        color: var(--sl-color-neutral-600);
      }
      .labels {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
      }
      a {
        color: var(--sl-color-primary-600);
      }
      .empty-wrap {
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
      .empty-body {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        padding: var(--sl-spacing-large);
      }
      .empty-icon {
        width: 72px;
        height: 72px;
        border-radius: 50%;
        background: var(--sl-color-primary-100);
        color: var(--sl-color-primary-600);
        display: flex;
        align-items: center;
        justify-content: center;
        margin-bottom: var(--sl-spacing-medium);
      }
      .empty-icon sl-icon {
        font-size: 2.5rem;
      }
      .empty-title {
        margin: 0 0 var(--sl-spacing-2x-small);
        font-size: 1.25rem;
        font-weight: 700;
        color: var(--sl-color-neutral-900);
      }
      .empty-copy {
        margin: 0 0 var(--sl-spacing-large);
        max-width: 440px;
        font-size: 0.95rem;
        line-height: 1.55;
      }
      .empty-command {
        width: 100%;
        display: flex;
        align-items: center;
        justify-content: space-between;
        background: var(--sl-color-neutral-100);
        border: 1px solid var(--sl-color-neutral-300);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
        font-family: var(--sl-font-mono);
        font-size: 0.9rem;
        margin-bottom: var(--sl-spacing-large);
      }
      .empty-command code {
        color: var(--sl-color-primary-700);
      }
      .empty-docs {
        width: 100%;
      }
      .default-pool {
        margin: 0 0 var(--sl-spacing-large);
        max-width: 420px;
      }
      .default-pool sl-select {
        margin-bottom: var(--sl-spacing-2x-small);
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    void this.load();
    this.unsubscribe = unifiedWebSocketManager.subscribe(
      'runners',
      (message: { type?: string; payload?: Partial<RunnerRecord> }) =>
        this.handleRunnerEvent(message)
    );
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.unsubscribe?.();
  }

  private handleRunnerEvent(message: {
    type?: string;
    payload?: Partial<RunnerRecord>;
  }) {
    if (message.type !== 'runner_updated' || !message.payload?.id) {
      return;
    }
    const incoming = message.payload as RunnerRecord;
    const index = this.runners.findIndex((row) => row.id === incoming.id);
    if (index === -1) {
      this.runners = [...this.runners, incoming];
      return;
    }
    const current = this.runners[index];
    this.runners = [
      ...this.runners.slice(0, index),
      {
        ...current,
        ...incoming,
        registered_by_email:
          incoming.registered_by_email || current.registered_by_email,
      },
      ...this.runners.slice(index + 1),
    ];
  }

  private async load() {
    this.loading = true;
    this.error = null;
    try {
      const [runners, account] = await Promise.all([
        getRunners(),
        getAccountOrganization().catch(() => null),
      ]);
      this.runners = runners;
      this.defaultRunnerPool = account?.default_runner_pool ?? null;
    } catch (err) {
      this.error =
        err instanceof Error ? err.message : 'Failed to load runners';
    } finally {
      this.loading = false;
    }
  }

  private async handleDefaultPoolChange(event: Event) {
    const target = event.target as HTMLSelectElement;
    const next = (target.value || '').trim() || null;
    this.savingDefault = true;
    this.defaultError = null;
    try {
      const updated = await updateAccountOrganization({
        default_runner_pool: next,
      });
      this.defaultRunnerPool = updated.default_runner_pool ?? null;
    } catch (err) {
      this.defaultError =
        err instanceof Error ? err.message : 'Failed to save default runner';
    } finally {
      this.savingDefault = false;
    }
  }

  private renderDefaultPoolControl() {
    const options = buildRunnerPoolOptions(this.runners);
    return html`
      <div class="default-pool">
        <sl-select
          label="Default runner pool"
          help-text="Used when a flow does not pin a runner. Private runners are preferred when one is online."
          .value=${this.defaultRunnerPool || ''}
          ?disabled=${this.savingDefault}
          @sl-change=${this.handleDefaultPoolChange}
        >
          ${options.map(
            (option) =>
              html`<sl-option value=${option.value}>${option.label}</sl-option>`
          )}
        </sl-select>
        ${
          this.defaultError
            ? html`<p class="muted">${this.defaultError}</p>`
            : nothing
        }
      </div>
    `;
  }

  private statusVariant(status: string): string {
    switch ((status || '').toLowerCase()) {
      case 'online':
        return 'success';
      case 'busy':
        return 'warning';
      default:
        return 'neutral';
    }
  }

  render() {
    return html`
      <view-header
        headerText="Runners"
        description="Self-hosted CLI runners for this account. Start one with preloop runner fg."
      ></view-header>
      ${this.loading ? nothing : this.renderDefaultPoolControl()}
      ${
        this.loading
          ? html`<sl-spinner></sl-spinner>`
          : this.error
            ? html`<p class="muted">${this.error}</p>`
            : this.runners.length === 0
              ? html`
                  <div class="empty-wrap">
                    <sl-card class="empty-card">
                      <div class="empty-body">
                        <div class="empty-icon">
                          <sl-icon name="cpu"></sl-icon>
                        </div>
                        <h3 class="empty-title">No runners registered</h3>
                        <p class="muted empty-copy">
                          Self-hosted CLI runners execute flow jobs and
                          automation tasks in your local environment with full
                          network and credential access.
                        </p>

                        <div class="empty-command">
                          <code>preloop runner fg --labels local</code>
                          <sl-copy-button
                            value="preloop runner fg --labels local"
                          ></sl-copy-button>
                        </div>

                        <sl-button
                          class="empty-docs"
                          variant="primary"
                          href="https://docs.preloop.ai/guide/runners"
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          <sl-icon slot="prefix" name="book"></sl-icon>
                          View Runner Documentation →
                        </sl-button>
                      </div>
                    </sl-card>
                  </div>
                `
              : html`
                  <table>
                    <thead>
                      <tr>
                        <th>Name</th>
                        <th>Labels</th>
                        <th>Registered by</th>
                        <th>Host</th>
                        <th>Status</th>
                        <th>Last heartbeat</th>
                        <th>Current execution</th>
                      </tr>
                    </thead>
                    <tbody>
                      ${this.runners.map(
                        (row) => html`
                          <tr>
                            <td>${row.name}</td>
                            <td>
                              <div class="labels">
                                ${(row.labels || []).map(
                                  (label) =>
                                    html`<sl-badge pill>${label}</sl-badge>`
                                )}
                              </div>
                            </td>
                            <td class="muted">
                              ${row.registered_by_email || '-'}
                            </td>
                            <td>
                              ${row.hostname || '-'}
                              <div class="muted">
                                ${[row.os, row.arch].filter(Boolean).join('/')}
                              </div>
                            </td>
                            <td>
                              <sl-badge
                                pill
                                variant=${this.statusVariant(row.status)}
                              >
                                ${row.status}
                              </sl-badge>
                            </td>
                            <td class="muted">
                              ${
                                row.last_heartbeat
                                  ? formatLocalDateTime(row.last_heartbeat)
                                  : '-'
                              }
                            </td>
                            <td>
                              ${
                                row.current_execution_id
                                  ? html`<a
                                      href="/console/flows/executions/${row.current_execution_id}"
                                      >${row.current_execution_id.slice(0, 8)}…</a
                                    >`
                                  : html`<span class="muted">idle</span>`
                              }
                            </td>
                          </tr>
                        `
                      )}
                    </tbody>
                  </table>
                `
      }
    `;
  }
}
