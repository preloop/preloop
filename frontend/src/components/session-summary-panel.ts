import { LitElement, css, html } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import type { FlowGatewayEvent, RuntimeSessionActivityItem } from '../types';
import type { ObservedSession } from '../utils/session-observer';
import { summarizeSessionLocally } from '../utils/session-observer';

@customElement('session-summary-panel')
export class SessionSummaryPanel extends LitElement {
  @property({ type: Object })
  session: ObservedSession | null = null;

  @property({ type: Array })
  events: FlowGatewayEvent[] = [];

  @property({ type: Array })
  activity: RuntimeSessionActivityItem[] = [];

  @property({ type: Object })
  modelSummary: {
    title: string;
    description: string;
    highlights?: string[];
    next_action?: string | null;
    generated_by?: string;
    estimated_summary_cost?: number;
  } | null = null;

  static styles = css`
    :host {
      display: block;
    }

    .panel {
      background: var(--sl-color-neutral-50);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-small);
      padding: var(--sl-spacing-medium);
    }

    .header {
      align-items: center;
      display: flex;
      gap: var(--sl-spacing-small);
      justify-content: space-between;
    }

    .title {
      color: var(--sl-color-neutral-900);
      font-weight: 700;
    }

    .description,
    .hint,
    .highlight {
      color: var(--sl-color-neutral-700);
      font-size: var(--sl-font-size-small);
      line-height: 1.45;
    }

    .highlights {
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-2x-small);
    }

    .highlight {
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 999px;
      padding: 2px 8px;
    }
  `;

  private getVariant(riskLevel: string | undefined) {
    if (riskLevel === 'high') return 'danger';
    if (riskLevel === 'medium') return 'warning';
    return 'success';
  }

  render() {
    if (!this.session) return '';

    const local = summarizeSessionLocally(
      this.session,
      this.events,
      this.activity
    );
    const summary = this.modelSummary || local;
    const highlights = summary.highlights || local.highlights;
    const generatedBy =
      summary.generated_by ||
      ('generatedBy' in summary ? summary.generatedBy : 'local');
    const estimatedCost =
      summary.estimated_summary_cost ??
      ('estimatedSummaryCost' in summary ? summary.estimatedSummaryCost : 0);

    return html`
      <div class="panel">
        <div class="header">
          <div class="title">${summary.title}</div>
          <sl-badge variant=${this.getVariant(local.riskLevel)} pill>
            ${generatedBy === 'model' ? 'AI summary' : 'Local summary'}
          </sl-badge>
        </div>
        <div class="description">${summary.description}</div>
        <div class="highlights">
          ${highlights.map(
            (highlight) => html`<span class="highlight">${highlight}</span>`
          )}
        </div>
        ${
          summary.next_action || local.nextAction
            ? html`
                <div class="hint">
                  <strong>Next:</strong> ${
                    summary.next_action || local.nextAction
                  }
                </div>
              `
            : ''
        }
        <div class="hint">
          Summary inspection cost:
          ${estimatedCost > 0 ? `$${estimatedCost.toFixed(4)}` : '$0.00'}
        </div>
      </div>
    `;
  }
}
