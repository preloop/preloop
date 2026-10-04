import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import {
  getDiscoveryCandidates,
  updateDiscoveryCandidate,
  type DiscoveredAgentCandidate,
} from '../api';
import { formatRelativeTime } from '../utils/date';

/** Onboard command an admin can paste on the reporting workstation. */
export function onboardCommandFor(candidate: DiscoveredAgentCandidate): string {
  return `preloop agents onboard ${candidate.agent_kind}`;
}

/** Short, non-reversible label for a salted workstation fingerprint. */
export function shortWorkstation(fingerprint: string): string {
  return fingerprint.slice(0, 8);
}

/**
 * "Not yet governed": agent tools reported by opt-in workstation discovery
 * (`preloop agents discover --report`) that nobody has onboarded or ignored.
 * The server only holds salted hashes, so a workstation is shown as a short
 * hash, never a hostname or user.
 */
@customElement('discovered-agents-panel')
export class DiscoveredAgentsPanel extends LitElement {
  /** Overridable for tests. */
  @property({ attribute: false })
  loader: () => Promise<DiscoveredAgentCandidate[]> = () =>
    getDiscoveryCandidates(['new']);

  /** Overridable for tests. */
  @property({ attribute: false })
  updater: (
    id: string,
    status: 'new' | 'ignored'
  ) => Promise<DiscoveredAgentCandidate> = updateDiscoveryCandidate;

  @state() private candidates: DiscoveredAgentCandidate[] = [];
  @state() private loaded = false;
  @state() private error: string | null = null;
  @state() private busyId: string | null = null;

  static styles = css`
    :host {
      display: block;
    }
    section {
      margin-top: var(--sl-spacing-large);
    }
    h2 {
      font-size: var(--sl-font-size-large);
      margin: 0 0 var(--sl-spacing-2x-small);
    }
    p.hint {
      margin: 0 0 var(--sl-spacing-small);
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: var(--sl-font-size-small);
    }
    th,
    td {
      text-align: left;
      padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
      border-bottom: 1px solid var(--sl-color-neutral-200);
      vertical-align: middle;
    }
    code {
      font-family: var(--sl-font-mono);
    }
    .actions {
      display: flex;
      gap: var(--sl-spacing-x-small);
      align-items: center;
    }
    .error {
      color: var(--sl-color-danger-700);
    }
  `;

  connectedCallback(): void {
    super.connectedCallback();
    void this.refresh();
  }

  async refresh(): Promise<void> {
    try {
      this.candidates = await this.loader();
      this.error = null;
    } catch {
      // A load failure (older server, no view permission) hides the section:
      // it is an add-on to the Agents page, not something to alarm about.
      this.candidates = [];
    } finally {
      this.loaded = true;
    }
  }

  private async markIgnored(candidate: DiscoveredAgentCandidate) {
    this.busyId = candidate.id;
    try {
      await this.updater(candidate.id, 'ignored');
      this.candidates = this.candidates.filter((c) => c.id !== candidate.id);
    } catch (err) {
      this.error =
        err instanceof Error
          ? err.message
          : 'Failed to update discovered agent';
    } finally {
      this.busyId = null;
    }
  }

  render() {
    // Nothing reported means discovery reporting is not in use: stay out of
    // the way instead of showing an empty section on every Agents page.
    if (!this.loaded || this.candidates.length === 0) {
      return nothing;
    }
    return html`
      <section aria-labelledby="not-yet-governed">
        <h2 id="not-yet-governed">
          Not yet governed
          <sl-badge variant="warning" pill>${this.candidates.length}</sl-badge>
        </h2>
        <p class="hint">
          Agent tools reported by <code>preloop agents discover --report</code>.
          Workstations are shown as salted hashes; no hostnames, user names or
          paths are collected.
        </p>
        ${this.error ? html`<p class="error">${this.error}</p>` : nothing}
        <table>
          <thead>
            <tr>
              <th>Kind</th>
              <th>Workstation</th>
              <th>MCP servers</th>
              <th>First seen</th>
              <th>Last seen</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${this.candidates.map(
              (candidate) => html`
                <tr data-candidate-id=${candidate.id}>
                  <td>${candidate.agent_kind}</td>
                  <td>
                    <code title=${candidate.workstation_fingerprint}
                      >${shortWorkstation(candidate.workstation_fingerprint)}</code
                    >
                  </td>
                  <td>${candidate.mcp_server_count}</td>
                  <td>${formatRelativeTime(candidate.first_seen_at)}</td>
                  <td>${formatRelativeTime(candidate.last_seen_at)}</td>
                  <td>
                    <div class="actions">
                      <sl-copy-button
                        class="copy-onboard"
                        value=${onboardCommandFor(candidate)}
                        copy-label="Copy onboard command"
                        success-label="Onboard command copied"
                      ></sl-copy-button>
                      <sl-button
                        class="mark-ignored"
                        size="small"
                        ?loading=${this.busyId === candidate.id}
                        @click=${() => this.markIgnored(candidate)}
                        >Mark ignored</sl-button
                      >
                    </div>
                  </td>
                </tr>
              `
            )}
          </tbody>
        </table>
      </section>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'discovered-agents-panel': DiscoveredAgentsPanel;
  }
}
