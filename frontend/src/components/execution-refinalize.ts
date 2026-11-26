import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import consoleStyles from '../styles/console-styles.css?inline';
import { refinalizeExecution, type RefinalizePreview } from '../api';
import { reasonText } from '../utils/platform-review';

/** An action exists only after the server confirms this person and run qualify. */
@customElement('execution-refinalize')
export class ExecutionRefinalize extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      p {
        font-size: var(--console-text-body, 14px);
      }
      .meta {
        color: var(--console-meta-color);
      }
      pre {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }
      ul {
        padding-inline-start: var(--sl-spacing-large);
      }
    `,
  ];

  @property({ type: String }) executionId = '';
  @property({ attribute: false }) mark: {
    at: string;
    user_name: string;
  } | null = null;
  @state() private preview: RefinalizePreview | null = null;
  @state() private busy = false;
  @state() private error = '';
  private generation = 0;

  protected updated(changed: Map<string, unknown>): void {
    if (changed.has('executionId')) void this.load();
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    this.preview = null;
    if (!this.executionId || this.mark) return;
    try {
      const preview = await refinalizeExecution(this.executionId);
      if (generation === this.generation) this.preview = preview;
    } catch {
      // A denied preview or an older server offers no recovery action.
    }
  }

  private async apply(): Promise<void> {
    if (!this.preview?.eligible || this.busy) return;
    const executionId = this.executionId;
    this.busy = true;
    this.error = '';
    try {
      const response = await refinalizeExecution(
        executionId,
        this.preview.digest
      );
      if (this.executionId !== executionId) return;
      this.mark = response.refinalization ?? null;
      this.preview = null;
      this.dispatchEvent(
        new CustomEvent('execution-refinalized', {
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Re-finalization failed';
      await this.load();
    } finally {
      this.busy = false;
    }
  }

  render() {
    if (this.mark)
      return html`<p class="meta">
        Re-finalized by ${this.mark.user_name} on
        <time datetime=${this.mark.at}
          >${new Date(this.mark.at).toLocaleString()}</time
        >.
      </p>`;
    if (!this.preview?.eligible)
      return this.error ? html`<p role="alert">${this.error}</p>` : nothing;
    const publication = this.preview.publication;
    const labels: Record<string, string> = {
      terminal_outcome: 'Recoverable control plane failure',
      flow_revision: 'Flow unchanged',
      read_only_review: 'Read-only review',
      capture_provenance: 'Saved answer captured',
      schema: 'Answer matches the required format',
      binding: 'Reviewed revision',
      inspection: 'Change inspected',
      integrity: 'Review integrity',
      confirmation: 'Review complete',
      tool_evidence: 'Evidence available',
      evidence_settled: 'Evidence capture finished',
      publication_receipt: 'Publication requirements',
    };
    return html`
      <p>
        The saved answer passed the review checks. Re-finalize it without
        running the model again.
      </p>
      <p class="meta">
        ${publication?.would_publish ? 'The review is eligible for posting. Posting checks run again when applied.' : `The review will not be posted: ${reasonText(publication?.reason) || publication?.reason || 'posting unavailable'}.`}
      </p>
      <sl-details summary="Review checks"
        ><ul>
          ${Object.entries(this.preview.gates).map(([name, gate]) => html`<li>${labels[name] || name}: ${gate.passed ? 'passed' : gate.reason}</li>`)}
        </ul></sl-details
      >
      <sl-details summary="Saved answer">
        <pre>${JSON.stringify(this.preview.selected_result, null, 2)}</pre>
      </sl-details>
      ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}
      <sl-button variant="default" .loading=${this.busy} @click=${this.apply}
        >Re-finalize saved answer</sl-button
      >
    `;
  }
}
