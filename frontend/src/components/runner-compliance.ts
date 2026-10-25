import { LitElement, html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { getFeatures, getUserProfile, hasPermission } from '../api';
import {
  runnerPolicyRequest,
  complianceLabel,
  type RunnerCompliance,
} from '../api/runner-policy';
import { formatLocalDateTime } from '../utils/date';

@customElement('runner-compliance')
export class RunnerComplianceView extends LitElement {
  @state() private enabled = false;
  @state() private page: RunnerCompliance | null = null;
  @state() private error = '';
  @state() private loading = false;
  private generation = 0;
  private refresh = () => {
    void this.load(this.page?.skip ?? 0);
  };
  static styles = css`
    :host {
      display: block;
    }
    h2 {
      margin: 0 0 0.5rem;
      font-size: var(--console-text-card-title, 0.9375rem);
      font-weight: 600;
    }
    section {
      margin: 1rem 0;
      padding: 1rem;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 0.5rem;
    }
    .scroll {
      overflow-x: auto;
    }
    table {
      border-collapse: collapse;
      width: 100%;
    }
    th,
    td {
      text-align: left;
      vertical-align: top;
      padding: 0.75rem;
      border-bottom: 1px solid var(--sl-color-neutral-200);
    }
    small {
      display: block;
    }
    button {
      font: inherit;
      margin: 0.5rem;
    }
    .pagination {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
    }
  `;
  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener('runner-policy-changed', this.refresh);
    void this.start();
  }
  disconnectedCallback(): void {
    ++this.generation;
    this.enabled = false;
    this.page = null;
    this.error = '';
    this.loading = false;
    window.removeEventListener('runner-policy-changed', this.refresh);
    super.disconnectedCallback();
  }
  private async start(): Promise<void> {
    const generation = ++this.generation;
    try {
      const features = await getFeatures();
      if (
        generation !== this.generation ||
        !features.features.runner_requirement_policy
      )
        return;
      const user = await getUserProfile();
      if (
        generation !== this.generation ||
        !hasPermission(user.permissions, 'view_users')
      )
        return;
      this.enabled = true;
      await this.load(0);
    } catch {
      if (generation === this.generation) {
        this.enabled = true;
        this.error = 'Could not load runner compliance.';
      }
    }
  }
  private async load(skip: number): Promise<void> {
    if (!this.enabled) return;
    const generation = ++this.generation;
    this.loading = true;
    this.error = '';
    try {
      const page = await runnerPolicyRequest<RunnerCompliance>(
        `runner-compliance?skip=${skip}&limit=100`
      );
      if (generation === this.generation) this.page = page;
    } catch (error) {
      if (generation === this.generation)
        this.error =
          error instanceof Error
            ? error.message
            : 'Could not load runner compliance.';
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }
  render() {
    if (!this.enabled) return nothing;
    return html`<section aria-label="Member runner compliance">
      <h2>Member runner compliance</h2>
      <p>
        Compliance reflects runner heartbeats. Inventory freshness is shown
        separately; unenrolled machines are not visible.
      </p>
      <button type="button" ?disabled=${this.loading} @click=${this.refresh}>
        Refresh compliance
      </button>
      ${this.loading ? html`<p role="status">Loading compliance…</p>` : nothing}
      ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}
      ${
        this.page
          ? html`<p>
                ${this.page.total} active members. Updated
                ${formatLocalDateTime(this.page.generated_at)}. Counts below
                cover this page.
              </p>
              <p>
                ${Object.entries(this.page.counts)
                  .map(
                    ([state, count]) => `${complianceLabel(state)}: ${count}`
                  )
                  .join(' · ')}
              </p>
              <div
                class="scroll"
                role="region"
                aria-label="Runner compliance roster"
                tabindex="0"
              >
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Member</th>
                      <th scope="col">Compliance</th>
                      <th scope="col">Last heartbeat</th>
                      <th scope="col">Runners</th>
                      <th scope="col">Harness inventory</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${this.page.members.map(
                      (member) =>
                        html`<tr>
                          <th scope="row">
                            ${member.username}<small
                              >${member.email ?? ''}</small
                            >
                          </th>
                          <td>
                            ${complianceLabel(member.state)}<small
                              >${member.requirement} ·
                              ${member.enforcement === 'warn' ? 'Warn' : 'Report only'}
                              · ${member.policy_source}</small
                            >${member.grace_until ? html`<small>Grace until ${formatLocalDateTime(member.grace_until)}</small>` : nothing}
                          </td>
                          <td>
                            ${member.last_heartbeat ? formatLocalDateTime(member.last_heartbeat) : 'Never'}
                          </td>
                          <td>
                            ${member.runners.length ? member.runners.map((runner) => html`<div>${runner.name} · ${runner.status}</div>`) : 'No runners'}
                          </td>
                          <td>
                            ${!member.inventory_known ? 'Inventory unknown' : html`<small>${member.inventory_fresh ? 'Fresh inventory' : 'Stale inventory'}</small>${member.harnesses.length ? member.harnesses.map((harness) => html`<div>${harness.display_name || harness.harness} · ${harness.governance} <small>${harness.runner_name}</small></div>`) : 'No harnesses reported'}`}
                          </td>
                        </tr>`
                    )}
                  </tbody>
                </table>
              </div>
              ${!this.page.members.length ? html`<p>No members on this page.</p>` : nothing}
              <div class="pagination">
                <button
                  type="button"
                  ?disabled=${this.loading || this.page.skip === 0}
                  @click=${() => void this.load(Math.max(0, this.page!.skip - 100))}
                >
                  Previous members</button
                ><span
                  >${this.page.skip + (this.page.members.length ? 1 : 0)}–${this.page.skip + this.page.members.length}
                  of ${this.page.total}</span
                ><button
                  type="button"
                  ?disabled=${this.loading || this.page.skip + this.page.members.length >= this.page.total}
                  @click=${() => void this.load(this.page!.skip + 100)}
                >
                  Next members
                </button>
              </div>`
          : nothing
      }
    </section>`;
  }
}
