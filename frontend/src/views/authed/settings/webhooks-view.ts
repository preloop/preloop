import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../../components/view-header.ts';
import {
  createWebhookEndpoint,
  deleteWebhookEndpoint,
  getWebhookCatalogue,
  getWebhookDeliveries,
  getWebhookEndpoints,
  replayWebhookEvent,
  sendWebhookTest,
  updateWebhookEndpoint,
} from '../../../api';
import type {
  WebhookCatalogue,
  WebhookDelivery,
  WebhookEndpoint,
  WebhookEndpointCreated,
} from '../../../types';
import { confirmDialog, showToast } from '../../../components/confirm-dialog';
import { consoleDialogStyles } from '../../../styles/console-dialog';
import consoleStyles from '../../../styles/console-styles.css?inline';
import { formatLocalDateTime, formatRelativeTime } from '../../../utils/date';

/** Rows the approval-workflow shim owns are shown, but edited on the workflow. */
const SHIM_SOURCE = 'approval_workflow';

@customElement('webhooks-view')
export class WebhooksView extends LitElement {
  @state()
  private endpoints: WebhookEndpoint[] = [];

  @state()
  private deliveries: WebhookDelivery[] = [];

  @state()
  private catalogue: WebhookCatalogue | null = null;

  @state()
  private loading = true;

  @state()
  private error: string | null = null;

  @state()
  private createOpen = false;

  @state()
  private createUrl = '';

  @state()
  private createDescription = '';

  @state()
  private createEventTypes: string[] = [];

  @state()
  private createError: string | null = null;

  @state()
  private creating = false;

  @state()
  private createdSecret: WebhookEndpointCreated | null = null;

  @state()
  private busyEndpointId: string | null = null;

  connectedCallback() {
    super.connectedCallback();
    void this.load();
  }

  private async load() {
    this.loading = true;
    this.error = null;
    try {
      const [catalogue, endpoints, deliveries] = await Promise.all([
        getWebhookCatalogue(),
        getWebhookEndpoints(),
        getWebhookDeliveries({ limit: 20 }),
      ]);
      this.catalogue = catalogue;
      this.endpoints = endpoints;
      this.deliveries = deliveries;
    } catch (err) {
      this.error =
        err instanceof Error ? err.message : 'Failed to load webhooks';
    } finally {
      this.loading = false;
    }
  }

  private async refreshDeliveries() {
    try {
      this.deliveries = await getWebhookDeliveries({ limit: 20 });
    } catch {
      // The delivery log is secondary: a failed refresh must not blank the
      // endpoint list the operator came here for.
    }
  }

  private openCreate() {
    this.createUrl = '';
    this.createDescription = '';
    this.createEventTypes = [];
    this.createError = null;
    this.createOpen = true;
  }

  private toggleEventType(name: string, checked: boolean) {
    const next = new Set(this.createEventTypes);
    if (checked) {
      next.add(name);
    } else {
      next.delete(name);
    }
    this.createEventTypes = Array.from(next);
  }

  private async handleCreate() {
    this.creating = true;
    this.createError = null;
    try {
      const created = await createWebhookEndpoint({
        url: this.createUrl.trim(),
        description: this.createDescription.trim() || null,
        event_types: this.createEventTypes,
      });
      this.createOpen = false;
      // The secret is readable exactly once, so the dialog that shows it
      // opens before the list refresh that would otherwise steal focus.
      this.createdSecret = created;
      this.endpoints = [created, ...this.endpoints];
    } catch (err) {
      this.createError =
        err instanceof Error ? err.message : 'Failed to create the endpoint';
    } finally {
      this.creating = false;
    }
  }

  private async handleToggleActive(endpoint: WebhookEndpoint) {
    this.busyEndpointId = endpoint.id;
    try {
      const updated = await updateWebhookEndpoint(endpoint.id, {
        active: !endpoint.active,
      });
      this.endpoints = this.endpoints.map((row) =>
        row.id === updated.id ? updated : row
      );
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : 'Failed to update the endpoint',
        'danger'
      );
    } finally {
      this.busyEndpointId = null;
    }
  }

  private async handleTest(endpoint: WebhookEndpoint) {
    this.busyEndpointId = endpoint.id;
    try {
      await sendWebhookTest(endpoint.id);
      // Queued, not delivered: the worker posts it on its next pass, so the
      // toast says what actually happened.
      showToast(
        'Test event queued. It appears below once attempted.',
        'success'
      );
      await this.refreshDeliveries();
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : 'Failed to queue the test event',
        'danger'
      );
    } finally {
      this.busyEndpointId = null;
    }
  }

  private async handleDelete(endpoint: WebhookEndpoint) {
    const confirmed = await confirmDialog({
      title: 'Delete webhook endpoint',
      message: `Delete ${endpoint.url}?`,
      detail:
        'Events stop being sent to it immediately and its delivery history is removed. This cannot be undone.',
      confirmLabel: 'Delete endpoint',
      variant: 'danger',
    });
    if (!confirmed) {
      return;
    }
    this.busyEndpointId = endpoint.id;
    try {
      await deleteWebhookEndpoint(endpoint.id);
      this.endpoints = this.endpoints.filter((row) => row.id !== endpoint.id);
      await this.refreshDeliveries();
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : 'Failed to delete the endpoint',
        'danger'
      );
    } finally {
      this.busyEndpointId = null;
    }
  }

  private async handleReplay(delivery: WebhookDelivery) {
    try {
      await replayWebhookEvent(delivery.event_id);
      showToast('Event re-queued.', 'success');
      await this.refreshDeliveries();
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : 'Failed to replay the event',
        'danger'
      );
    }
  }

  /** An endpoint's state in one chip, worst news first. */
  private endpointState(endpoint: WebhookEndpoint): {
    label: string;
    variant: 'neutral' | 'success' | 'warning' | 'danger';
  } {
    if (!endpoint.active) {
      return { label: 'Paused', variant: 'neutral' };
    }
    if (endpoint.circuit_open) {
      return { label: 'Circuit open', variant: 'warning' };
    }
    if (endpoint.last_delivery_status === 'dead') {
      return { label: 'Failing', variant: 'danger' };
    }
    if (endpoint.last_delivery_status === 'delivered') {
      return { label: 'Delivering', variant: 'success' };
    }
    return { label: 'No traffic yet', variant: 'neutral' };
  }

  private deliveryVariant(
    status: string
  ): 'neutral' | 'success' | 'warning' | 'danger' {
    if (status === 'delivered') return 'success';
    if (status === 'dead') return 'danger';
    return 'neutral';
  }

  private renderEndpointRow(endpoint: WebhookEndpoint) {
    const state = this.endpointState(endpoint);
    const managed = endpoint.source === SHIM_SOURCE;
    const busy = this.busyEndpointId === endpoint.id;
    return html`
      <tr>
        <td>
          <div class="url" title=${endpoint.url}>${endpoint.url}</div>
          ${
            endpoint.description
              ? html`<div class="muted">${endpoint.description}</div>`
              : nothing
          }
          ${
            managed
              ? html`<div class="muted">
                  Managed by an approval workflow. Edit it there.
                </div>`
              : nothing
          }
        </td>
        <td>
          ${
            endpoint.event_types.length === 0
              ? html`<span class="muted">All events</span>`
              : html`<div class="filters">
                  ${endpoint.event_types.map(
                    (name) =>
                      html`<sl-badge class="chip" pill>${name}</sl-badge>`
                  )}
                </div>`
          }
        </td>
        <td>
          <sl-badge class="chip" pill variant=${state.variant}
            >${state.label}</sl-badge
          >
          ${
            endpoint.last_error
              ? html`<div class="muted" title=${endpoint.last_error}>
                  ${endpoint.last_error}
                </div>`
              : nothing
          }
        </td>
        <td class="muted">
          ${
            endpoint.last_delivery_at
              ? html`<span
                  title=${formatLocalDateTime(endpoint.last_delivery_at)}
                  >${formatRelativeTime(endpoint.last_delivery_at)}</span
                >`
              : '-'
          }
        </td>
        <td class="actions">
          ${
            managed
              ? nothing
              : html`
                  <sl-button
                    size="small"
                    ?loading=${busy}
                    @click=${() => this.handleTest(endpoint)}
                    >Send test</sl-button
                  >
                  <sl-button
                    size="small"
                    ?loading=${busy}
                    @click=${() => this.handleToggleActive(endpoint)}
                    >${endpoint.active ? 'Pause' : 'Resume'}</sl-button
                  >
                  <sl-button
                    class="danger-action"
                    size="small"
                    variant="danger"
                    outline
                    ?loading=${busy}
                    @click=${() => this.handleDelete(endpoint)}
                    >Delete</sl-button
                  >
                `
          }
        </td>
      </tr>
    `;
  }

  private renderCreateDialog() {
    return html`
      <sl-dialog
        label="Add webhook endpoint"
        ?open=${this.createOpen}
        @sl-after-hide=${() => (this.createOpen = false)}
      >
        <div class="form">
          <sl-input
            label="Endpoint URL"
            placeholder="https://collector.example.com/preloop"
            .value=${this.createUrl}
            @sl-input=${(e: Event) =>
              (this.createUrl = (e.target as HTMLInputElement).value)}
          ></sl-input>
          <sl-input
            label="Description"
            placeholder="SIEM"
            .value=${this.createDescription}
            @sl-input=${(e: Event) =>
              (this.createDescription = (e.target as HTMLInputElement).value)}
          ></sl-input>
          <div>
            <div class="field-label">Events</div>
            <div class="muted">Select none to receive every event.</div>
            <div class="events">
              ${(this.catalogue?.event_types ?? []).map(
                (item) => html`
                  <sl-checkbox
                    ?checked=${this.createEventTypes.includes(item.name)}
                    @sl-change=${(e: Event) =>
                      this.toggleEventType(
                        item.name,
                        (e.target as HTMLInputElement).checked
                      )}
                  >
                    <span class="event-name">${item.name}</span>
                    <div class="muted">${item.description}</div>
                  </sl-checkbox>
                `
              )}
            </div>
          </div>
          ${this.createError ? html`<p class="muted">${this.createError}</p>` : nothing}
        </div>
        <sl-button slot="footer" @click=${() => (this.createOpen = false)}
          >Cancel</sl-button
        >
        <sl-button
          slot="footer"
          variant="primary"
          ?loading=${this.creating}
          ?disabled=${!this.createUrl.trim()}
          @click=${this.handleCreate}
          >Add endpoint</sl-button
        >
      </sl-dialog>
    `;
  }

  private renderSecretDialog() {
    const created = this.createdSecret;
    if (!created) {
      return nothing;
    }
    return html`
      <sl-dialog
        label="Signing secret"
        open
        @sl-after-hide=${() => (this.createdSecret = null)}
      >
        <p>
          Store this now. It is shown once and cannot be read back; losing it
          means registering a new endpoint.
        </p>
        <div class="secret">
          <code>${created.secret}</code>
          <sl-copy-button value=${created.secret}></sl-copy-button>
        </div>
        <p class="muted">
          Requests carry
          <code
            >${this.catalogue?.signature_header ?? 'X-Preloop-Signature'}</code
          >
          as <code>t=&lt;unix seconds&gt;,v1=&lt;hex hmac&gt;</code> over
          <code>timestamp + "." + body</code>. Verification samples are in the
          webhooks guide.
        </p>
        <sl-button
          slot="footer"
          variant="primary"
          @click=${() => (this.createdSecret = null)}
          >Done</sl-button
        >
      </sl-dialog>
    `;
  }

  private renderDeliveries() {
    if (this.deliveries.length === 0) {
      return html`<p class="empty-state">
        No deliveries yet. Events appear here as they are sent.
      </p>`;
    }
    return html`
      <table>
        <thead>
          <tr>
            <th>Event</th>
            <th>Status</th>
            <th>Attempts</th>
            <th>Response</th>
            <th>Occurred</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${this.deliveries.map(
            (delivery) => html`
              <tr>
                <td>
                  ${delivery.event_type}
                  <div class="muted">${delivery.event_id.slice(0, 8)}…</div>
                </td>
                <td>
                  <sl-badge
                    class="chip"
                    pill
                    variant=${this.deliveryVariant(delivery.status)}
                    >${delivery.status}</sl-badge
                  >
                  ${
                    delivery.last_error
                      ? html`<div class="muted" title=${delivery.last_error}>
                          ${delivery.last_error}
                        </div>`
                      : nothing
                  }
                </td>
                <td class="num">${delivery.attempt_count}</td>
                <td class="num">${delivery.response_status ?? '-'}</td>
                <td class="muted">
                  <span title=${formatLocalDateTime(delivery.occurred_at)}
                    >${formatRelativeTime(delivery.occurred_at)}</span
                  >
                </td>
                <td class="actions">
                  ${
                    delivery.status === 'dead'
                      ? html`<sl-button
                          size="small"
                          @click=${() => this.handleReplay(delivery)}
                          >Replay</sl-button
                        >`
                      : nothing
                  }
                </td>
              </tr>
            `
          )}
        </tbody>
      </table>
    `;
  }

  render() {
    return html`
      <view-header
        headerText="Webhooks"
        description="Signed events Preloop sends to your systems."
      >
        <sl-button
          slot="main-column"
          variant="primary"
          size="small"
          @click=${this.openCreate}
          >Add endpoint</sl-button
        >
      </view-header>
      ${
        this.loading
          ? html`<sl-spinner></sl-spinner>`
          : this.error
            ? html`<p class="muted">${this.error}</p>`
            : html`
                ${
                  this.endpoints.length === 0
                    ? html`<p class="empty-state">
                        No endpoints yet. Add one to receive approval, policy,
                        session, budget and flow events.
                      </p>`
                    : html`
                        <table>
                          <thead>
                            <tr>
                              <th>Endpoint</th>
                              <th>Events</th>
                              <th>State</th>
                              <th>Last delivery</th>
                              <th></th>
                            </tr>
                          </thead>
                          <tbody>
                            ${this.endpoints.map((endpoint) =>
                              this.renderEndpointRow(endpoint)
                            )}
                          </tbody>
                        </table>
                      `
                }
                <h2 class="section-title">Recent deliveries</h2>
                ${this.renderDeliveries()}
              `
      }
      ${this.renderCreateDialog()} ${this.renderSecretDialog()}
    `;
  }

  static styles = [
    unsafeCSS(consoleStyles),
    consoleDialogStyles,
    css`
      :host {
        display: block;
        font-size: var(--console-text-body);
      }
      .muted {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th,
      td {
        text-align: left;
        padding: 8px 10px;
        border-bottom: 1px solid var(--console-hairline);
        vertical-align: top;
      }
      th {
        font-size: var(--console-text-meta);
        font-weight: 600;
        color: var(--console-meta-color);
      }
      td.num {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      td.actions {
        text-align: right;
        white-space: nowrap;
      }
      /* Delete sits last, after a gap, so it is never the button next to the
         one an operator meant to press. */
      .danger-action {
        margin-left: var(--sl-spacing-large);
      }
      .url {
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 28rem;
        white-space: nowrap;
      }
      .filters {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
      }
      .section-title {
        font-size: var(--console-text-card-title);
        font-weight: 600;
        margin: var(--sl-spacing-x-large) 0 var(--sl-spacing-small);
      }
      .form {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-medium);
      }
      .field-label {
        font-size: var(--console-text-meta);
        font-weight: 600;
      }
      .events {
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-x-small);
        margin-top: var(--sl-spacing-x-small);
      }
      .event-name {
        font-weight: 500;
      }
      .secret {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
        background: var(--console-page);
        padding: var(--sl-spacing-x-small) var(--sl-spacing-small);
        border-radius: var(--sl-border-radius-medium);
        word-break: break-all;
      }
      @media (max-width: 640px) {
        .url {
          max-width: 12rem;
        }
      }
    `,
  ];
}
