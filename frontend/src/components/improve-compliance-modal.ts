import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  Issue,
  getComplianceImprovementSuggestion,
  updateIssueContent,
} from '../api';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';

import './single-issue-detail-view.ts';
import consoleStyles from '../styles/console-styles.css?inline';

@customElement('improve-compliance-modal')
export class ImproveComplianceModal extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      .comparison-container {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--sl-spacing-medium);
      }

      .comparison-panel h3 {
        margin-top: 0;
        padding-bottom: var(--sl-spacing-small);
        border-bottom: 1px solid var(--sl-color-neutral-200);
        margin-bottom: var(--sl-spacing-medium);
      }

      .loading-suggestion {
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100%;
        gap: var(--sl-spacing-small);
      }

      .issue-description {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-700);
        background-color: var(--sl-color-neutral-100);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
        white-space: pre-line;
        overflow-wrap: break-word;
        max-height: 400px;
        overflow-y: auto;
      }

      .compliance-title {
        display: block;
        margin-top: var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-x-small);
        font-weight: var(--sl-font-weight-semibold);
      }
    `,
  ];

  @property({ type: Boolean }) open = false;
  @property({ type: Object }) issue: Issue | null = null;
  @property({ type: String }) promptName = '';

  @state() private _isSubmitting = false;
  @state() private _isLoadingSuggestion = false;
  @state() private _suggestionError: string | null = null;

  @state() private _suggestedTitle = '';
  @state() private _suggestedDescription = '';
  @state() private _suggestedChanges = '';

  updated(changedProperties: Map<string, unknown>) {
    if (changedProperties.has('open') && this.open) {
      this.handleOpen();
    }
  }

  private async handleOpen() {
    if (!this.issue) return;
    this._suggestionError = null;
    this._suggestedTitle = '';
    this._suggestedDescription = '';
    this._suggestedChanges = '';
    this.fetchSuggestion();
  }

  private handleClose() {
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('close', { bubbles: true, composed: true })
    );
  }

  private async fetchSuggestion() {
    if (!this.issue) return;
    this._isLoadingSuggestion = true;
    this._suggestionError = null;
    try {
      const suggestion = await getComplianceImprovementSuggestion(
        this.issue.id,
        this.promptName
      );
      this._suggestedTitle = suggestion.title;
      this._suggestedDescription = suggestion.description;
      this._suggestedChanges = suggestion.changes;
    } catch (error) {
      this._suggestionError =
        error instanceof Error ? error.message : 'Failed to load suggestion.';
      console.error('Failed to get compliance suggestion:', error);
    } finally {
      this._isLoadingSuggestion = false;
    }
  }

  private async _handleSubmit() {
    if (!this.issue) return;

    this._isSubmitting = true;
    this._suggestionError = null;

    console.log('[Modal] Handling submit for issue:', this.issue.id);
    console.log('[Modal] With data:', {
      title: this._suggestedTitle,
      description: this._suggestedDescription,
    });

    try {
      const response = await updateIssueContent(
        this.issue.id,
        this._suggestedTitle,
        this._suggestedDescription
      );

      console.log('[Modal] API call successful, response:', response);

      const summary = `Issue ${this.issue.key} was successfully updated.`;
      const detail = { issueId: this.issue.id, summary };

      console.log('[Modal] Dispatching on-submit event with detail:', detail);
      this.dispatchEvent(
        new CustomEvent('on-submit', {
          bubbles: true,
          composed: true,
          detail,
        })
      );
      this.handleClose();
    } catch (error) {
      console.error('[Modal] API call failed:', error);
      // Consider showing a toast notification on error
    } finally {
      this._isSubmitting = false;
    }
  }

  render() {
    return html`
      <sl-dialog
        label="Improve Issue Compliance"
        class="dialog-overview large"
        .open=${this.open}
        @sl-hide=${this.handleClose}
      >
        ${
          this.issue
            ? html`
                <div class="comparison-container">
                  <div class="comparison-panel">
                    <h3>Original Issue</h3>
                    <single-issue-detail-view
                      .issue=${this.issue}
                    ></single-issue-detail-view>
                  </div>
                  <div class="comparison-panel">
                    <h3>Suggested Improvement</h3>
                    ${
                      this._isLoadingSuggestion
                        ? html`<div class="loading-suggestion">
                            <sl-spinner></sl-spinner>
                            <span>Generating suggestion...</span>
                          </div>`
                        : this._suggestionError
                          ? html`<sl-alert variant="danger" open
                              ><sl-icon
                                slot="icon"
                                name="exclamation-octagon"
                              ></sl-icon
                              >${this._suggestionError}</sl-alert
                            >`
                          : html`
                              <sl-input
                                label="Title"
                                .value=${this._suggestedTitle}
                                @sl-input=${(e: Event) =>
                                  (this._suggestedTitle = (
                                    e.target as HTMLInputElement
                                  ).value)}
                              ></sl-input>
                              <br />
                              <sl-textarea
                                label="Description"
                                .value=${this._suggestedDescription}
                                @sl-input=${(e: Event) =>
                                  (this._suggestedDescription = (
                                    e.target as HTMLInputElement
                                  ).value)}
                                rows="10"
                              ></sl-textarea>
                              <br />
                              <div>
                                <b class="compliance-title">Changes</b>
                                <div class="issue-description">
                                  ${this._suggestedChanges}
                                </div>
                              </div>
                            `
                    }
                  </div>
                </div>
              `
            : ''
        }
        <sl-button slot="footer" @click=${this.handleClose}>Cancel</sl-button>
        <sl-button
          slot="footer"
          type="button"
          variant="primary"
          .loading=${this._isSubmitting}
          @click=${this._handleSubmit}
          >Update Issue</sl-button
        >
      </sl-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'improve-compliance-modal': ImproveComplianceModal;
  }
}
