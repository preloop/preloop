import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '../../components/view-header.ts';
import {
  getAccountOrganization,
  getRunners,
  updateAccountOrganization,
  type RunnerRecord,
} from '../../api';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { formatLocalDateTime, formatRelativeTime } from '../../utils/date';
import { AUTO_RUNNER_POOL } from '../../utils/runner-pool';
import '../../components/preloop-runner-pool-select';
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
  private hostedMinutesLeft: number | null = null;

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
      /*
       * An empty page states one fact and hands over one command. The old
       * treatment (a 580px card, a 72px badge icon and a full width primary
       * button) spent a screen saying "nothing here yet".
       */
      .empty-state {
        box-sizing: border-box;
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: center;
        gap: var(--sl-spacing-x-small) var(--sl-spacing-small);
        min-height: 72px;
        padding: var(--sl-spacing-medium);
        color: var(--sl-color-neutral-600);
        font-size: 13px;
      }
      .empty-command {
        display: inline-flex;
        align-items: center;
        gap: var(--sl-spacing-2x-small);
        font-family: var(--sl-font-mono);
      }
      .empty-command code {
        background: var(--sl-color-neutral-100);
        border-radius: var(--sl-border-radius-small);
        color: var(--sl-color-neutral-800);
        padding: 1px 6px;
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
      this.hostedMinutesLeft = account?.hosted_minutes_remaining ?? null;
    } catch (err) {
      this.error =
        err instanceof Error ? err.message : 'Failed to load runners';
    } finally {
      this.loading = false;
    }
  }

  private async handleDefaultPoolChange(
    event: CustomEvent<{ value: string | null }>
  ) {
    const raw = (event.detail?.value || '').trim();
    const next = !raw || raw === AUTO_RUNNER_POOL ? null : raw;
    const previous = this.defaultRunnerPool;
    this.defaultRunnerPool = next;
    this.savingDefault = true;
    this.defaultError = null;
    try {
      const updated = await updateAccountOrganization({
        default_runner_pool: next,
      });
      this.defaultRunnerPool = updated.default_runner_pool ?? null;
    } catch (err) {
      this.defaultRunnerPool = previous;
      this.defaultError =
        err instanceof Error ? err.message : 'Failed to save default runner';
    } finally {
      this.savingDefault = false;
    }
  }

  private renderDefaultPoolControl() {
    return html`
      <div class="default-pool">
        <preloop-runner-pool-select
          label="Default runner pool"
          .helpText=${'Applies to every flow that does not pin a runner.'}
          context="account"
          .value=${this.defaultRunnerPool}
          .runners=${this.runners}
          .accountPool=${this.defaultRunnerPool}
          .hostedMinutesLeft=${this.hostedMinutesLeft}
          ?disabled=${this.savingDefault}
          @pool-change=${this.handleDefaultPoolChange}
        ></preloop-runner-pool-select>
        ${
          this.defaultError
            ? html`<p class="muted">${this.defaultError}</p>`
            : nothing
        }
      </div>
    `;
  }

  /** "online" is a wire value; "Online" is what a person reads. */
  private statusLabel(status: string): string {
    const raw = (status || '').trim();
    if (!raw) return 'Unknown';
    return raw.charAt(0).toUpperCase() + raw.slice(1).toLowerCase();
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
        description="Self-hosted CLI runners for this account."
      ></view-header>
      ${this.loading ? nothing : this.renderDefaultPoolControl()}
      ${
        this.loading
          ? html`<sl-spinner></sl-spinner>`
          : this.error
            ? html`<p class="muted">${this.error}</p>`
            : this.runners.length === 0
              ? html`
                  <p class="empty-state">
                    <span>No runners registered. Start one:</span>
                    <span class="empty-command">
                      <code>preloop runner fg --labels local</code>
                      <sl-copy-button
                        value="preloop runner fg --labels local"
                      ></sl-copy-button>
                    </span>
                    <a
                      class="empty-docs"
                      href="https://docs.preloop.ai/guide/runners"
                      target="_blank"
                      rel="noopener noreferrer"
                      >Runner docs</a
                    >
                  </p>
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
                                    html`<sl-badge class="chip" pill
                                      >${label}</sl-badge
                                    >`
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
                                class="chip"
                                pill
                                variant=${this.statusVariant(row.status)}
                              >
                                ${this.statusLabel(row.status)}
                              </sl-badge>
                            </td>
                            <td class="muted">
                              ${
                                row.last_heartbeat
                                  ? html`<span
                                      title=${formatLocalDateTime(
                                        row.last_heartbeat
                                      )}
                                      >${formatRelativeTime(
                                        row.last_heartbeat
                                      )}</span
                                    >`
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
                                  : html`<span class="muted">Idle</span>`
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
