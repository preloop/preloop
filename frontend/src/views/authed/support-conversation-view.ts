import { randomUuid } from '../../utils/random-uuid';
import { LitElement, html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import { ConsoleStatus } from '../../controllers/console-status';
import { fetchWithAuth } from '../../api';
import '../../components/view-header';

@customElement('support-conversation-view')
export class SupportConversationView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  @state() private data?: {
    status: string;
    page: string;
    messages: { sequence: number; role: string; text: string }[];
    delivery_jobs: { id: string; status: string; last_error?: string }[];
  };
  @state() private error = '';
  private generation = 0;
  private conversationId = window.location.pathname.split('/').pop() || '';
  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }
  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.generation++;
    this.data = undefined;
  }
  private async request(action?: string) {
    const response = await fetchWithAuth(
      `/api/v1/support/operator/conversations/${encodeURIComponent(this.conversationId)}${action ? '/actions' : ''}`,
      {
        cache: 'no-store',
        method: action ? 'POST' : 'GET',
        ...(action
          ? {
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action, event_id: randomUuid() }),
            }
          : {}),
      }
    );
    if (!response.ok)
      throw new Error(
        response.status === 403
          ? 'Only the configured support operator can open this conversation.'
          : 'Conversation unavailable or operator delivery needs attention.'
      );
    return response.json();
  }
  private async load(action?: string) {
    const generation = this.generation;
    this.error = '';
    try {
      if (action) await this.request(action);
      const data = await this.request();
      if (this.isConnected && generation === this.generation) this.data = data;
    } catch (error) {
      if (generation === this.generation)
        this.error =
          error instanceof Error ? error.message : 'Support unavailable.';
    }
  }
  private async retryDelivery(jobId: string): Promise<void> {
    try {
      const response = await fetchWithAuth(
        `/api/v1/support/operator/conversations/${encodeURIComponent(this.conversationId)}/deliveries/${encodeURIComponent(jobId)}/retry`,
        { method: 'POST' }
      );
      if (!response.ok)
        throw new Error(
          'Only definitively failed deliveries can be retried. Reconcile uncertain sends first.'
        );
      await this.load();
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Retry unavailable.';
    }
  }
  render() {
    return html`<view-header headerText="Support conversation"></view-header>
      <main>
        ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}${
          this.data
            ? html`<p role="status">${this.data.page} · ${this.data.status}</p>
                <p>
                  Reply in the private Mattermost thread after taking over.
                  These actions require the configured support operator account.
                </p>
                ${['takeover', 'resume', 'close', 'reopen'].map((action) => html`<sl-button @click=${() => void this.load(action)}>${{ takeover: 'Take over', resume: 'Return to assistant', close: 'Close', reopen: 'Reopen' }[action]}</sl-button>`)}<sl-button
                  @click=${() => void this.load()}
                  >Refresh</sl-button
                >${this.data.messages.map(
                  (message) =>
                    html`<article>
                      <strong>${message.role}</strong>
                      <p>${message.text}</p>
                    </article>`
                )}
                <h2>Operator delivery</h2>
                ${this.data.delivery_jobs.map((job) => html`<p>${job.status}${job.last_error ? `: ${job.last_error}` : ''}${job.status === 'failed' ? html`<sl-button @click=${() => void this.retryDelivery(job.id)}>Retry delivery</sl-button>` : nothing}</p>`)}`
            : html`<p role="status">Loading conversation…</p>`
        }
      </main>`;
  }
  static styles = css`
    :host {
      display: block;
    }
    main {
      max-width: 60rem;
      padding: 1.5rem;
    }
    article {
      border-bottom: 1px solid var(--sl-color-neutral-200);
    }
    p {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    sl-button {
      margin: 0.25rem;
    }
  `;
}
