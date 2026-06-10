import { LitElement, css, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import type { FlowGatewayEvent, RuntimeSessionActivityItem } from '../types';
import type {
  ObservedSession,
  SessionOptimizationSuggestion,
} from '../utils/session-observer';
import {
  formatCost,
  formatNumber,
  suggestSessionOptimizations,
} from '../utils/session-observer';

@customElement('session-optimization-panel')
export class SessionOptimizationPanel extends LitElement {
  @property({ type: Object })
  session: ObservedSession | null = null;

  @property({ type: Array })
  events: FlowGatewayEvent[] = [];

  @property({ type: Array })
  activity: RuntimeSessionActivityItem[] = [];

  @property({ type: Array })
  suggestions: SessionOptimizationSuggestion[] | null = null;

  static styles = css`
    :host {
      display: block;
    }

    .panel {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
    }

    .suggestion {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      padding: var(--sl-spacing-medium);
    }

    .suggestion-header {
      align-items: start;
      display: flex;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
    }

    .title {
      color: var(--sl-color-neutral-900);
      font-weight: 700;
    }

    .description,
    .evidence,
    .savings {
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-small);
      line-height: 1.45;
      margin-top: var(--sl-spacing-2x-small);
    }

    .savings {
      color: var(--sl-color-success-700);
      font-weight: 600;
    }

    .actions {
      display: flex;
      justify-content: flex-end;
      margin-top: var(--sl-spacing-small);
    }
  `;

  private getConfidenceVariant(confidence: string) {
    if (confidence === 'high') return 'success';
    if (confidence === 'medium') return 'primary';
    return 'neutral';
  }

  private emitSuggestion(suggestion: SessionOptimizationSuggestion): void {
    this.dispatchEvent(
      new CustomEvent('session-optimization-selected', {
        detail: { suggestion, session: this.session },
        bubbles: true,
        composed: true,
      })
    );
  }

  render() {
    if (!this.session) return '';
    const suggestions =
      this.suggestions || suggestSessionOptimizations(this.session);

    return html`
      <div class="panel">
        ${suggestions.map(
          (suggestion) => html`
            <div class="suggestion">
              <div class="suggestion-header">
                <div>
                  <div class="title">${suggestion.title}</div>
                  <div class="description">${suggestion.description}</div>
                </div>
                <sl-badge
                  variant=${this.getConfidenceVariant(suggestion.confidence)}
                  pill
                >
                  ${suggestion.confidence} confidence
                </sl-badge>
              </div>
              <div class="savings">
                Expected savings:
                ${formatNumber(suggestion.expectedSavingsTokens)} tokens ·
                ${formatCost(suggestion.expectedSavingsUsd)}
              </div>
              ${suggestion.evidence.length
                ? html`
                    <div class="evidence">
                      Evidence: ${suggestion.evidence.join(' · ')}
                    </div>
                  `
                : ''}
              <div class="actions">
                <sl-button
                  size="small"
                  @click=${() => this.emitSuggestion(suggestion)}
                >
                  <sl-icon slot="prefix" name="magic"></sl-icon>
                  ${suggestion.actionLabel}
                </sl-button>
              </div>
            </div>
          `
        )}
      </div>
    `;
  }
}
