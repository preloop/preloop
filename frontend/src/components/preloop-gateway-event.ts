import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type {
  FlowGatewayEvent,
  FlowGatewayEventPayload,
  FlowGatewayConversationPreviewMessage,
} from '../types';
import { formatLocalTime, formatUTCDateTime } from '../utils/date';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';
import './json-tree.js';

@customElement('preloop-gateway-event')
export class PreloopGatewayEvent extends LitElement {
  @property({ type: Object })
  event!: FlowGatewayEvent;

  @property({ type: Boolean })
  expanded = false;

  static styles = css`
    :host {
      display: block;
      margin-bottom: var(--sl-spacing-small);
    }

    .gateway-event::part(base) {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      overflow: hidden;
    }

    .gateway-event::part(header) {
      padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      background: var(--sl-color-neutral-50);
    }

    .gateway-event::part(header):hover {
      background: var(--sl-color-neutral-100);
    }

    .gateway-event::part(content) {
      padding: var(--sl-spacing-medium);
      border-top: 1px solid var(--sl-color-neutral-200);
    }

    .gateway-event-summary {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
      align-items: center;
      width: 100%;
    }

    .gateway-event-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
      padding-bottom: 16px;
      margin-bottom: 16px;
      border-bottom: 1px solid var(--sl-color-neutral-200);
    }

    .gateway-event-field {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .gateway-event-label {
      font-size: 0.7rem;
      font-weight: 600;
      text-transform: uppercase;
      color: var(--sl-color-neutral-500);
    }

    .gateway-event-value {
      font-size: 0.85rem;
      color: var(--sl-color-neutral-900);
      word-break: break-all;
    }

    .gateway-capture-policy {
      background: var(--sl-color-neutral-50);
      border-radius: var(--sl-border-radius-medium);
      padding: 12px;
      margin-bottom: 16px;
    }

    .gateway-capture-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }

    .gateway-badges {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }

    .conversation-preview-list {
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-bottom: 16px;
    }

    .conversation-preview-message {
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      overflow: hidden;
    }

    .conversation-preview-header {
      background: var(--sl-color-neutral-50);
      padding: 8px 12px;
      border-bottom: 1px solid var(--sl-color-neutral-200);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px;
    }

    .conversation-preview-title {
      font-weight: 600;
      font-size: 0.85rem;
      color: var(--sl-color-neutral-800);
    }

    .conversation-preview-text {
      margin: 0;
      padding: 12px;
      font-family: var(--sl-font-mono);
      font-size: 0.85rem;
      color: var(--sl-color-neutral-800);
      background: var(--sl-color-neutral-0);
      max-height: min(40vh, 360px);
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }

    .conversation-preview-redacted {
      color: var(--sl-color-neutral-500);
      font-style: italic;
    }

    .payload-section-title {
      font-size: 0.9rem;
      font-weight: 600;
      color: var(--sl-color-neutral-900);
      margin-bottom: 8px;
    }

    .payload-block {
      background: var(--sl-color-neutral-100);
      border-radius: var(--sl-border-radius-medium);
      max-height: min(55vh, 520px);
      padding: 12px;
      overflow: auto;
      margin-bottom: 16px;
    }

    .payload-block pre {
      margin: 0;
      font-family: var(--sl-font-mono);
      font-size: 0.8rem;
      color: var(--sl-color-neutral-800);
    }

    .search-summary {
      font-size: 0.85rem;
      color: var(--sl-color-neutral-600);
      margin-top: 4px;
      margin-bottom: 12px;
      font-style: italic;
    }
  `;

  private renderGatewayField(label: string, value: unknown) {
    return html`
      <div class="gateway-event-field">
        <div class="gateway-event-label">${label}</div>
        <div class="gateway-event-value">${value ?? 'n/a'}</div>
      </div>
    `;
  }

  private getGatewayModelLabel(event: FlowGatewayEvent): string {
    return (
      event.payload.model_alias ||
      event.payload.requested_model ||
      (event.payload.metadata as any)?.requested_model ||
      'Unknown model'
    );
  }

  private getGatewayProviderLabel(event: FlowGatewayEvent): string {
    return (
      event.payload.provider_name ||
      event.payload.gateway_provider ||
      (event.payload.metadata as any)?.provider ||
      'Unknown provider'
    );
  }

  private getGatewayOutcomeVariant(outcome?: string | null) {
    switch (outcome) {
      case 'success':
        return 'success';
      case 'budget_denied':
        return 'warning';
      case 'error':
        return 'danger';
      default:
        return 'neutral';
    }
  }

  private formatGatewayOutcome(outcome?: string | null): string {
    if (!outcome) return 'Unknown';
    return outcome
      .split('_')
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  }

  private formatGatewayCost(cost?: number | null): string {
    if (typeof cost !== 'number' || Number.isNaN(cost)) return 'n/a';
    if (cost === 0) return '$0.00';
    return cost >= 0.01 ? `$${cost.toFixed(2)}` : `$${cost.toFixed(4)}`;
  }

  private formatGatewayTokens(tokens?: number | null): string {
    if (typeof tokens !== 'number' || Number.isNaN(tokens)) return 'n/a';
    return tokens.toLocaleString();
  }

  private formatGatewayPayload(payload: unknown): string {
    return JSON.stringify(payload, null, 2);
  }

  private formatGatewayLabel(value?: string | null): string {
    if (!value) return 'Unknown';
    return value
      .split('_')
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  }

  private getGatewayPreviewMessages(
    payload: FlowGatewayEventPayload
  ): FlowGatewayConversationPreviewMessage[] {
    return Array.isArray(payload.conversation_preview?.messages)
      ? payload.conversation_preview?.messages
      : [];
  }

  private renderGatewayCapturePolicy(payload: FlowGatewayEventPayload) {
    const policy = payload.capture_policy;
    if (!policy) return '';

    return html`
      <div class="payload-section-title">Capture Policy</div>
      <div class="gateway-capture-policy">
        <div class="gateway-capture-grid">
          ${this.renderGatewayField(
            'Content',
            policy.content_capture_enabled
              ? 'Preview captured'
              : 'Preview redacted'
          )}
          ${this.renderGatewayField(
            'Max Preview',
            typeof policy.max_preview_chars === 'number'
              ? `${policy.max_preview_chars} chars`
              : 'n/a'
          )}
          ${this.renderGatewayField(
            'Conversation',
            policy.conversation_preview_available
              ? 'Available'
              : 'Not available'
          )}
        </div>
        <div class="gateway-badges">
          ${
            policy.sensitive_fields_redacted
              ? html`<sl-badge pill>Sensitive fields redacted</sl-badge>`
              : ''
          }
          ${
            policy.content_redacted
              ? html`<sl-badge pill variant="warning"
                  >Content redacted</sl-badge
                >`
              : ''
          }
          ${
            policy.content_truncated
              ? html`<sl-badge pill variant="warning"
                  >Content truncated</sl-badge
                >`
              : ''
          }
        </div>
      </div>
    `;
  }

  private renderGatewayPreviewMessage(
    message: FlowGatewayConversationPreviewMessage
  ) {
    const previewText = message.text
      ? message.text
      : message.redacted
        ? 'Content redacted by capture policy.'
        : 'No text content captured.';

    return html`
      <div class="conversation-preview-message">
        <div class="conversation-preview-header">
          <div class="conversation-preview-title">
            ${this.formatGatewayLabel(message.source)}
            ${this.formatGatewayLabel(message.role)}
          </div>
          <div class="gateway-badges">
            ${
              message.redacted
                ? html`<sl-badge pill variant="warning">Redacted</sl-badge>`
                : ''
            }
            ${
              message.truncated
                ? html`<sl-badge pill variant="warning">Truncated</sl-badge>`
                : ''
            }
            ${
              typeof message.original_length === 'number'
                ? html`
                    <sl-badge pill variant="neutral">
                      ${message.original_length.toLocaleString()} chars
                    </sl-badge>
                  `
                : ''
            }
          </div>
        </div>
        <pre
          class="conversation-preview-text ${
            message.redacted ? 'conversation-preview-redacted' : ''
          }"
        >
${previewText}</pre>
        ${
          message.truncated
            ? html`
                <div class="search-summary">
                  This stored preview was truncated before display.
                </div>
              `
            : ''
        }
      </div>
    `;
  }

  private renderGatewayConversationPreview(payload: FlowGatewayEventPayload) {
    const messages = this.getGatewayPreviewMessages(payload);
    const metadata = payload.conversation_preview?.metadata;

    return html`
      <div class="payload-section-title">Conversation Preview</div>
      <div class="gateway-badges" style="margin-bottom: 12px;">
        <sl-badge pill>${messages.length} messages</sl-badge>
        ${
          metadata?.has_redacted_content
            ? html`<sl-badge pill variant="warning"
                >Contains redactions</sl-badge
              >`
            : ''
        }
        ${
          metadata?.has_truncated_content
            ? html`<sl-badge pill variant="warning"
                >Contains truncation</sl-badge
              >`
            : ''
        }
      </div>
      ${
        messages.length > 0
          ? html`
              <div class="conversation-preview-list">
                ${messages.map((message) =>
                  this.renderGatewayPreviewMessage(message)
                )}
              </div>
            `
          : html`
              <div class="payload-block" style="margin-bottom: 16px;">
                <pre>No conversation preview captured for this event.</pre>
              </div>
            `
      }
    `;
  }

  private handleExpand(e: Event) {
    const details = e.target as HTMLDetailsElement;
    this.expanded = details.open;
    this.dispatchEvent(
      new CustomEvent('gateway-event-expand', {
        detail: { eventId: this.event.id, expanded: this.expanded },
        bubbles: true,
        composed: true,
      })
    );
  }

  render() {
    if (!this.event) return html``;

    const payload = this.event.payload || {};
    const isModelEvent = this.event.type.includes('model_gateway_call');

    const timestamp = this.event.timestamp
      ? html`
          <sl-tooltip content=${formatUTCDateTime(this.event.timestamp)}>
            <span>${formatLocalTime(this.event.timestamp)}</span>
          </sl-tooltip>
        `
      : 'Unknown';

    if (!isModelEvent) {
      // Render generic or tool event compactly
      // Used in session-history-widget when everything is mixed
      const title = payload.tool_name
        ? `Tool Call: ${payload.tool_name}`
        : this.event.type;
      const icon = payload.tool_name ? 'tools' : 'activity';
      return html`
        <sl-details
          class="gateway-event"
          ?open=${this.expanded}
          @sl-show=${this.handleExpand}
          @sl-hide=${this.handleExpand}
        >
          <div slot="summary" class="gateway-event-summary">
            <div
              style="display: flex; gap: 8px; align-items: center; font-weight: 600;"
            >
              <sl-icon name=${icon}></sl-icon> ${title}
            </div>
            ${this.renderGatewayField('Timestamp', timestamp)}
            ${
              payload.outcome
                ? this.renderGatewayField(
                    'Outcome',
                    html` <sl-badge
                      variant=${this.getGatewayOutcomeVariant(payload.outcome)}
                      >${this.formatGatewayOutcome(payload.outcome)}</sl-badge
                    >`
                  )
                : ''
            }
          </div>

          ${
            payload.error_detail
              ? html`
                  <sl-alert variant="danger" open style="margin-bottom: 16px;">
                    <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                    ${payload.error_detail}
                  </sl-alert>
                `
              : ''
          }

          <div class="payload-section-title">Event Payload</div>
          <div class="payload-block">
            <pre>${this.formatGatewayPayload(payload)}</pre>
          </div>
        </sl-details>
      `;
    }

    return html`
      <sl-details
        class="gateway-event"
        ?open=${this.expanded}
        @sl-show=${this.handleExpand}
        @sl-hide=${this.handleExpand}
      >
        <div slot="summary" class="gateway-event-summary">
          <div style="display: flex; align-items: center; gap: 8px;">
            <sl-icon name="cpu"></sl-icon>
            <strong>Gateway</strong>
          </div>
          ${this.renderGatewayField('Timestamp', timestamp)}
          ${this.renderGatewayField(
            'Model',
            this.getGatewayModelLabel(this.event)
          )}
          ${this.renderGatewayField(
            'Provider',
            this.getGatewayProviderLabel(this.event)
          )}
          ${this.renderGatewayField(
            'Outcome',
            html`
              <sl-badge
                variant=${this.getGatewayOutcomeVariant(payload.outcome)}
              >
                ${this.formatGatewayOutcome(payload.outcome)}
              </sl-badge>
            `
          )}
          ${this.renderGatewayField(
            'Cost',
            this.formatGatewayCost(payload.estimated_cost)
          )}
          ${this.renderGatewayField(
            'Tokens',
            this.formatGatewayTokens(payload.total_tokens)
          )}
        </div>

        <div class="gateway-event-meta">
          ${this.renderGatewayField(
            'HTTP',
            payload.status_code
              ? `${payload.method || 'POST'} ${payload.status_code}`
              : payload.method || 'n/a'
          )}
          ${
            payload.gateway_attempt
              ? this.renderGatewayField(
                  'Gateway Attempt',
                  payload.is_retry
                    ? `Retry #${payload.gateway_attempt}`
                    : `#${payload.gateway_attempt}`
                )
              : ''
          }
          ${
            payload.retry_of_api_usage_id
              ? this.renderGatewayField(
                  'Retry Of',
                  String(payload.retry_of_api_usage_id)
                )
              : ''
          }
          ${this.renderGatewayField(
            'Endpoint',
            payload.endpoint_kind || payload.endpoint || 'n/a'
          )}
          ${this.renderGatewayField(
            'Duration',
            typeof payload.duration_ms === 'number'
              ? `${payload.duration_ms} ms`
              : 'n/a'
          )}
          ${this.renderGatewayField(
            'Prompt Tokens',
            this.formatGatewayTokens(payload.prompt_tokens)
          )}
          ${this.renderGatewayField(
            'Completion Tokens',
            this.formatGatewayTokens(payload.completion_tokens)
          )}
          ${this.renderGatewayField(
            'Finish Reason',
            payload.finish_reason || 'n/a'
          )}
          ${this.renderGatewayField(
            'Request ID',
            payload.upstream_request_id || 'n/a'
          )}
        </div>

        ${
          payload.error_detail
            ? html`
                <sl-alert variant="danger" open style="margin-bottom: 16px;">
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${payload.error_detail}
                </sl-alert>
              `
            : ''
        }
        ${this.renderGatewayCapturePolicy(payload)}
        ${this.renderGatewayConversationPreview(payload)}

        <div class="payload-section-title">Event Payload</div>
        <div class="payload-block">
          <pre>${this.formatGatewayPayload(payload)}</pre>
        </div>
      </sl-details>
    `;
  }
}
