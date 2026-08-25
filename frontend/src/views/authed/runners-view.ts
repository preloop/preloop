import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '../../components/view-header.ts';
import { getRunners, type RunnerRecord } from '../../api';
import { formatLocalDateTime } from '../../utils/date';
import consoleStyles from '../../styles/console-styles.css?inline';

@customElement('runners-view')
export class RunnersView extends LitElement {
  @state()
  private runners: RunnerRecord[] = [];

  @state()
  private loading = true;

  @state()
  private error: string | null = null;

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
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    void this.load();
  }

  private async load() {
    this.loading = true;
    this.error = null;
    try {
      this.runners = await getRunners();
    } catch (err) {
      this.error =
        err instanceof Error ? err.message : 'Failed to load runners';
    } finally {
      this.loading = false;
    }
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
      ${
        this.loading
          ? html`<sl-spinner></sl-spinner>`
          : this.error
            ? html`<p class="muted">${this.error}</p>`
            : this.runners.length === 0
              ? html`
                  <div
                    style="display: flex; justify-content: center; width: 100%; margin-top: var(--sl-spacing-large);"
                  >
                    <sl-card
                      style="width: 100%; max-width: 580px; border: 1px solid rgba(139, 92, 246, 0.35); box-shadow: 0 12px 40px rgba(139, 92, 246, 0.12); border-radius: var(--sl-border-radius-large); overflow: hidden;"
                    >
                      <div
                        style="display: flex; flex-direction: column; align-items: center; text-align: center; padding: var(--sl-spacing-large);"
                      >
                        <div
                          style="width: 72px; height: 72px; border-radius: 50%; background: radial-gradient(circle, rgba(139, 92, 246, 0.2) 0%, rgba(99, 102, 241, 0.05) 70%); display: flex; align-items: center; justify-content: center; margin-bottom: var(--sl-spacing-medium);"
                        >
                          <sl-icon
                            name="cpu"
                            style="font-size: 2.5rem; color: #8b5cf6;"
                          ></sl-icon>
                        </div>
                        <h3
                          style="margin: 0 0 var(--sl-spacing-2x-small); font-size: 1.25rem; font-weight: 700; color: var(--sl-color-neutral-900);"
                        >
                          No runners registered
                        </h3>
                        <p
                          class="muted"
                          style="margin: 0 0 var(--sl-spacing-large); max-width: 440px; font-size: 0.95rem; line-height: 1.55;"
                        >
                          Self-hosted CLI runners execute flow jobs and
                          automation tasks in your local environment with full
                          network and credential access.
                        </p>

                        <div
                          style="width: 100%; display: flex; align-items: center; justify-content: space-between; background: rgba(15, 23, 42, 0.65); border: 1px solid rgba(139, 92, 246, 0.25); border-radius: var(--sl-border-radius-medium); padding: var(--sl-spacing-small) var(--sl-spacing-medium); font-family: var(--sl-font-mono); font-size: 0.9rem; margin-bottom: var(--sl-spacing-large);"
                        >
                          <code style="color: #38bdf8;"
                            >preloop runner fg --labels local</code
                          >
                          <sl-copy-button
                            value="preloop runner fg --labels local"
                          ></sl-copy-button>
                        </div>

                        <sl-button
                          variant="primary"
                          href="https://docs.preloop.ai/guide/runners"
                          target="_blank"
                          rel="noopener noreferrer"
                          style="width: 100%; --sl-color-primary-600: #6366f1; --sl-color-primary-700: #4f46e5;"
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
