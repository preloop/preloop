import { LitElement, css, html, nothing, type PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { fetchWithAuth } from '../api';
import { REST_API_PREFIX } from '../api-path';
import { formatLocalDateTime } from '../utils/date';

interface Readiness {
  enforced?: boolean;
  workflow_id: string | null;
  workflow_name: string | null;
  recipients: string[];
  deciders: string[];
  destinations: string[];
  issues: string[];
  timeout_seconds: number | null;
  ai_fallback: string | null;
  external: boolean;
}

/** Shared routing facts beside native mode controls and pending requests. */
@customElement('approval-readiness')
export class ApprovalReadiness extends LitElement {
  @property({ type: Boolean }) compact = false;
  @property() agentId = '';
  @property() workflowId = '';
  @property() requestId = '';
  @property() expiresAt = '';
  @property({ attribute: false }) revision: unknown = null;
  @property({ type: Boolean }) enforced = true;
  @state() private data: Readiness | null = null;
  @state() private failed = false;
  private generation = 0;

  static styles = css`
    :host {
      display: block;
      font-size: var(--console-text-meta, 13px);
    }
    summary {
      cursor: pointer;
    }
    summary span {
      color: var(--console-link-color);
    }
    p {
      margin: 0.5rem 0;
      overflow-wrap: anywhere;
    }
    a,
    button {
      color: var(--console-link-color);
    }
    button {
      font: inherit;
      background: none;
      border: none;
      padding: 0;
      text-decoration: underline;
      cursor: pointer;
    }
    .issue {
      border-left: 2px solid var(--sl-color-warning-600);
      padding-left: 0.6rem;
    }
  `;

  protected updated(changed: PropertyValues): void {
    if (
      ['agentId', 'workflowId', 'requestId', 'enforced', 'revision'].some(
        (key) => changed.has(key)
      )
    )
      void this.load();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    ++this.generation;
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    this.data = null;
    this.failed = false;
    if (!this.enforced) return;
    const query = new URLSearchParams();
    if (this.agentId) query.set('agent_id', this.agentId);
    if (this.workflowId) query.set('workflow_id', this.workflowId);
    const path = this.requestId
      ? `/approval-requests/${encodeURIComponent(this.requestId)}/readiness`
      : `/account/approval-readiness?${query}`;
    try {
      const response = await fetchWithAuth(`${REST_API_PREFIX}${path}`);
      if (!response.ok) throw new Error('Readiness unavailable');
      const data = await response.json();
      if (generation === this.generation) this.data = data;
    } catch {
      if (generation === this.generation) this.failed = true;
    }
  }

  render() {
    if (!this.enforced) return nothing;
    if (this.failed)
      return html`<p role="status">
        Approval readiness could not be checked.
        <button @click=${() => void this.load()}>Retry readiness check</button>
      </p>`;
    const data = this.data;
    if (!data) return html`<p role="status">Checking approval readiness…</p>`;
    if (data.enforced === false) return nothing;
    const details = html`
      ${data.issues?.map((issue) => html`<p class="issue" role="status">${issue}</p>`)}
      <p>
        <strong
          >${this.requestId ? 'Waiting in' : 'Requests that need review go to'}</strong
        >
        ${data.external ? 'the external approval service' : html`<a href="/console/approvals">Approvals</a>`}
        ${data.workflow_name ? html` via ${data.workflow_name}.` : '.'}
      </p>
      ${data.ai_fallback && !this.requestId ? html`<p>AI approval is enabled. When uncertain: ${data.ai_fallback === 'escalate' ? 'send for human review' : data.ai_fallback}.</p>` : nothing}
      ${
        data.external
          ? html`<p>The external service decides this request.</p>`
          : data.ai_fallback && data.ai_fallback !== 'escalate'
            ? nothing
            : html` <p>
                  Who can decide in the console:
                  ${data.deciders?.join(', ') || 'No active member with approval permission'}.
                </p>
                <p>
                  Workflow recipients: ${data.recipients?.join(', ') || 'None'}.
                  Notification destinations:
                  ${data.destinations?.join(', ') || 'None'}.
                </p>`
      }
      ${this.requestId ? html`<p>Deadline: ${this.expiresAt ? formatLocalDateTime(this.expiresAt) : 'Not provided by the server'}. The request's stored expiry controls this wait.</p>` : data.timeout_seconds ? html`<p>Default approval window: ${data.timeout_seconds} seconds. A specific rule or client deadline can change the wait; each request shows its actual expiry.</p>` : nothing}
      ${data.issues?.length ? html`<p><a href=${`/console/tools?tab=mcp&edit_workflow=${encodeURIComponent(data.workflow_id || 'new')}`}>Configure approval workflow</a> · <a href="/console/settings/notification-preferences">My notifications</a> · <a href="/console/settings/users">Manage members</a></p>` : nothing}
      <p>
        Configured destinations do not confirm delivery or that someone is
        online.
        <button @click=${() => void this.load()}>Refresh readiness</button>
      </p>
    `;
    return html`<section aria-label="Approval readiness">
      ${
        this.compact
          ? html`<details>
              <summary>
                Approval routing
                ${data.issues?.length ? 'needs attention' : 'is configured'} ·
                <span>Details</span>
              </summary>
              ${details}
            </details>`
          : details
      }
    </section>`;
  }
}
