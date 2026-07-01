import { LitElement, html, css, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  DuplicatePair,
  executeIssueDuplicateResolution,
  IssueDuplicateResolutionRequest,
  getResolutionSuggestion,
} from '../api';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/tag/tag.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab-panel/tab-panel.js';

import consoleStyles from '../styles/console-styles.css?inline';

type ResolutionStep = 'initial' | 'close' | 'merge' | 'deconflict';

@customElement('resolve-issue-modal')
export class ResolveIssueModal extends LitElement {
  @property({ type: Boolean }) isOpen = false;
  @property({ type: Object }) duplicatePair: DuplicatePair | null = null;

  @state() private _isSubmitting = false;
  @state() private _resolutionStep: ResolutionStep = 'initial';
  @state() private _isLoadingSuggestion = false;
  @state() private _mergeTarget: 'A' | 'B' = 'B';
  @state() private _mergedTitle = '';
  @state() private _mergedDescription = '';

  // State for Deconflict step
  @state() private _deconflictedTitle1 = '';
  @state() private _deconflictedDescription1 = '';
  @state() private _deconflictedTitle2 = '';
  @state() private _deconflictedDescription2 = '';

  @state() private _resolutionSummary = '';
  @state() private _modalTitle = 'Resolve Similar Issues';

  private updateTitleForStep(step: ResolutionStep) {
    switch (step) {
      case 'initial':
        this._modalTitle = 'Resolve Similar Issues';
        break;
      case 'close':
        this._modalTitle = 'Resolve Similar Issues → Close as Duplicate';
        break;
      case 'merge':
        this._modalTitle = 'Resolve Similar Issues → Merge Issues';
        break;
      case 'deconflict':
        this._modalTitle = 'Resolve Similar Issues → Deconflict Issues';
        break;
    }
  }

  private handleOpen() {
    this._isSubmitting = false;
    this._resolutionStep = 'initial';
    this.updateTitleForStep('initial');
  }

  private handleClose() {
    this.isOpen = false;
    this.dispatchEvent(
      new CustomEvent('on-close', { bubbles: true, composed: true })
    );
  }

  private goBack() {
    this._resolutionStep = 'initial';
    this.updateTitleForStep('initial');
  }

  private async startResolution(step: ResolutionStep) {
    this._resolutionStep = step;
    this.updateTitleForStep(step);

    // Reset previous suggestions
    this._mergedTitle = '';
    this._mergedDescription = '';
    this._deconflictedTitle1 = '';
    this._deconflictedDescription1 = '';
    this._deconflictedTitle2 = '';
    this._deconflictedDescription2 = '';

    if (step === 'merge' || step === 'deconflict') {
      if (!this.duplicatePair?.issue1 || !this.duplicatePair?.issue2) return;
      this._isLoadingSuggestion = true;
      try {
        const resolutionType = step === 'merge' ? 'merged' : 'deconflicted';
        const suggestion = await getResolutionSuggestion(
          this.duplicatePair.issue1.id,
          this.duplicatePair.issue2.id,
          resolutionType
        );

        if (step === 'merge') {
          this._mergedTitle = suggestion.merged_title || '';
          this._mergedDescription = suggestion.merged_description || '';
        } else {
          this._deconflictedTitle1 = suggestion.deconflicted_title1 || '';
          this._deconflictedDescription1 =
            suggestion.deconflicted_description1 || '';
          this._deconflictedTitle2 = suggestion.deconflicted_title2 || '';
          this._deconflictedDescription2 =
            suggestion.deconflicted_description2 || '';
        }
      } catch (error) {
        console.error('Failed to get suggestion:', error);
        // Optionally, show an error message to the user
      } finally {
        this._isLoadingSuggestion = false;
      }
    }
  }

  private async handleFinalResolve(resolutionType: string) {
    if (!this.duplicatePair) return;
    this._isSubmitting = true;

    const { issue1, issue2 } = this.duplicatePair;
    let resolutionData: IssueDuplicateResolutionRequest;
    let resolutionSummary = '';

    switch (resolutionType) {
      case 'CLOSE_A':
        resolutionData = {
          issue1_id: issue1.id,
          issue2_id: issue2.id,
          resolution: 'close_a',
        };
        resolutionSummary = `Closed ${issue1.key} as duplicate of ${issue2.key}.`;
        break;
      case 'CLOSE_B':
        resolutionData = {
          issue1_id: issue1.id,
          issue2_id: issue2.id,
          resolution: 'close_b',
        };
        resolutionSummary = `Closed ${issue2.key} as duplicate of ${issue1.key}.`;
        break;
      case 'MERGE':
        resolutionData = {
          issue1_id: issue1.id,
          issue2_id: issue2.id,
          resolution:
            this._mergeTarget === 'A' ? 'merge_b_to_a' : 'merge_a_to_b',
          resulting_issue_1_title:
            this._mergeTarget === 'A' ? this._mergedTitle : undefined,
          resulting_issue_1_description:
            this._mergeTarget === 'A' ? this._mergedDescription : undefined,
          resulting_issue_2_title:
            this._mergeTarget === 'B' ? this._mergedTitle : undefined,
          resulting_issue_2_description:
            this._mergeTarget === 'B' ? this._mergedDescription : undefined,
        };
        resolutionSummary = `Merged ${issue1.key} and ${issue2.key}.`;
        break;
      case 'DECONFLICT':
        resolutionData = {
          issue1_id: issue1.id,
          issue2_id: issue2.id,
          resolution: 'deconflict',
          resulting_issue_1_title: this._deconflictedTitle1,
          resulting_issue_1_description: this._deconflictedDescription1,
          resulting_issue_2_title: this._deconflictedTitle2,
          resulting_issue_2_description: this._deconflictedDescription2,
        };
        resolutionSummary = `Deconflicted ${issue1.key} and ${issue2.key}.`;
        break;
      case 'UNRELATED':
        resolutionData = {
          issue1_id: issue1.id,
          issue2_id: issue2.id,
          resolution: 'unrelated',
        };
        resolutionSummary = `Marked ${issue1.key} and ${issue2.key} as unrelated.`;
        break;
      default:
        // Handle other cases if necessary
        this._isSubmitting = false;
        return;
    }

    try {
      await executeIssueDuplicateResolution(resolutionData);
      this.dispatchEvent(
        new CustomEvent('on-resolved', {
          bubbles: true,
          composed: true,
          detail: { summary: resolutionSummary },
        })
      );
      this.handleClose();
    } catch (error) {
      console.error('Failed to resolve duplicate:', error);
      // TODO: Add a user-facing error notification (e.g., a toast)
    } finally {
      this._isSubmitting = false;
    }
  }

  // Step 1: Initial choice
  private renderInitialStep() {
    const actions = [
      {
        id: 'close',
        title: 'Close as Duplicate',
        description:
          'One issue will be closed, the other will remain. You will choose which one to close in the next step.',
        handler: () => this.startResolution('close'),
      },
      {
        id: 'merge',
        title: 'Merge Issues',
        description:
          'Combine both issues into a single issue. You will edit the new title and description in the next step.',
        handler: () => this.startResolution('merge'),
      },
      {
        id: 'deconflict',
        title: 'Deconflict Issues',
        description:
          'Edit the titles and descriptions of both issues to make them distinct. Both issues will remain open.',
        handler: () => this.startResolution('deconflict'),
      },
      {
        id: 'unrelated',
        title: 'Mark as Unrelated',
        description:
          'Mark the issues as unrelated. This action is immediate and requires no further steps.',
        handler: () => this.handleFinalResolve('UNRELATED'),
      },
    ];

    return html`
      <div class="step-container">
        <div class="initial-options-group">
          ${actions.map(
            (action) => html`
              <div class="action-card" @click=${action.handler}>
                <div class="action-title">${action.title}</div>
                <div class="action-description">${action.description}</div>
              </div>
            `
          )}
        </div>
      </div>
    `;
  }

  // Step 2a: Close
  private renderCloseStep() {
    const issueA = this.duplicatePair?.issue1;
    const issueB = this.duplicatePair?.issue2;
    return html`
      <div class="step-container">
        <p>Select which issue to close. The other will remain open.</p>
        <div class="close-options-group">
          <div
            class="action-card"
            @click=${() => this.handleFinalResolve('CLOSE_A')}
          >
            <div class="action-title">
              Close ${issueA?.key}: ${issueA?.title}
            </div>
            <div class="action-description">
              This will mark ${issueA?.key} as a duplicate of ${issueB?.key} and
              close it. ${issueB?.key} will remain open.
            </div>
          </div>
          <div
            class="action-card"
            @click=${() => this.handleFinalResolve('CLOSE_B')}
          >
            <div class="action-title">
              Close ${issueB?.key}: ${issueB?.title}
            </div>
            <div class="action-description">
              This will mark ${issueB?.key} as a duplicate of ${issueA?.key} and
              close it. ${issueA?.key} will remain open.
            </div>
          </div>
        </div>
        <div class="footer-buttons">
          <sl-button @click="${this.goBack}">Back</sl-button>
        </div>
      </div>
    `;
  }

  // Step 2b: Merge
  private renderMergeStep() {
    const issueA = this.duplicatePair?.issue1;
    const issueB = this.duplicatePair?.issue2;
    return html`
      <div class="step-container">
        <h2 class="issue-comparison-header">Original Issues</h2>
        <div class="comparison-container">
          <div class="issue-panel">
            <div class="issue-header">
              <a href="${issueA?.url}" target="_blank">${issueA?.key}</a>
            </div>
            <h3 class="issue-title">${issueA?.title}</h3>
            <div class="issue-description">${issueA?.description}</div>
          </div>
          <div class="issue-panel">
            <div class="issue-header">
              <a href="${issueB?.url}" target="_blank">${issueB?.key}</a>
            </div>
            <h3 class="issue-title">${issueB?.title}</h3>
            <div class="issue-description">${issueB?.description}</div>
          </div>
        </div>

        <h2 class="issue-comparison-header">Proposed Issue</h2>
        ${
          this._isLoadingSuggestion
            ? html`<div class="loading-suggestion">
                <sl-spinner></sl-spinner>
                <div>Generating suggestion...</div>
              </div>`
            : html`
                <div class="form-group">
                  <sl-input
                    .value=${this._mergedTitle}
                    @sl-input=${(e: any) => (this._mergedTitle = e.target.value)}
                  ></sl-input>
                  <sl-textarea
                    .value=${this._mergedDescription}
                    @sl-input=${(e: any) =>
                      (this._mergedDescription = e.target.value)}
                    rows="8"
                  ></sl-textarea>
                </div>
              `
        }

        <div class="footer-buttons">
          <sl-button @click="${this.goBack}">Back</sl-button>
          <sl-button-group label="Merge Direction">
            <sl-button
              variant=${this._mergeTarget === 'A' ? 'primary' : 'default'}
              @click=${() => (this._mergeTarget = 'A')}
              ?disabled=${this._isLoadingSuggestion}
              >Merge into ${issueA?.key}</sl-button
            >
            <sl-button
              variant=${this._mergeTarget === 'B' ? 'primary' : 'default'}
              @click=${() => (this._mergeTarget = 'B')}
              ?disabled=${this._isLoadingSuggestion}
              >Merge into ${issueB?.key}</sl-button
            >
          </sl-button-group>
          <sl-button
            variant="primary"
            .loading=${this._isSubmitting}
            ?disabled=${this._isLoadingSuggestion}
            @click="${() => this.handleFinalResolve('MERGE')}"
            >Resolve by Merging</sl-button
          >
        </div>
      </div>
    `;
  }

  // Step 2c: Deconflict
  private renderDeconflictStep() {
    const issueA = this.duplicatePair?.issue1;
    const issueB = this.duplicatePair?.issue2;
    return html`
      <div class="step-container">
        <h2 class="issue-comparison-header">Original Issues</h2>
        <div class="comparison-container">
          <div class="issue-panel">
            <div class="issue-header">
              <a href="${issueA?.url}" target="_blank">${issueA?.key}</a>
            </div>
            <h3 class="issue-title">${issueA?.title}</h3>
            <div class="issue-description">${issueA?.description}</div>
          </div>
          <div class="issue-panel">
            <div class="issue-header">
              <a href="${issueB?.url}" target="_blank">${issueB?.key}</a>
            </div>
            <h3 class="issue-title">${issueB?.title}</h3>
            <div class="issue-description">${issueB?.description}</div>
          </div>
        </div>

        <h2 class="issue-comparison-header">Proposed Changes</h2>
        ${
          this._isLoadingSuggestion
            ? html`<div class="loading-suggestion">
                <sl-spinner></sl-spinner>
                <div>Generating suggestion...</div>
              </div>`
            : html`
                <div class="comparison-container">
                  <div class="issue-panel form-group">
                    <div class="issue-header">${issueA?.key}</div>
                    <sl-input
                      .value=${this._deconflictedTitle1}
                      @sl-input=${(e: any) =>
                        (this._deconflictedTitle1 = e.target.value)}
                    ></sl-input>
                    <sl-textarea
                      .value=${this._deconflictedDescription1}
                      @sl-input=${(e: any) =>
                        (this._deconflictedDescription1 = e.target.value)}
                      rows="8"
                    ></sl-textarea>
                  </div>
                  <div class="issue-panel form-group">
                    <div class="issue-header">${issueB?.key}</div>
                    <sl-input
                      .value=${this._deconflictedTitle2}
                      @sl-input=${(e: any) =>
                        (this._deconflictedTitle2 = e.target.value)}
                    ></sl-input>
                    <sl-textarea
                      .value=${this._deconflictedDescription2}
                      @sl-input=${(e: any) =>
                        (this._deconflictedDescription2 = e.target.value)}
                      rows="8"
                    ></sl-textarea>
                  </div>
                </div>
              `
        }

        <div class="footer-buttons">
          <sl-button @click="${this.goBack}">Back</sl-button>
          <sl-button
            variant="primary"
            .loading=${this._isSubmitting}
            ?disabled=${this._isLoadingSuggestion}
            @click="${() => this.handleFinalResolve('DECONFLICT')}"
            >Resolve by Deconflicting</sl-button
          >
        </div>
      </div>
    `;
  }

  render() {
    let content;
    switch (this._resolutionStep) {
      case 'close':
        content = this.renderCloseStep();
        break;
      case 'merge':
        content = this.renderMergeStep();
        break;
      case 'deconflict':
        content = this.renderDeconflictStep();
        break;
      default:
        content = this.renderInitialStep();
    }

    const issue1 = this.duplicatePair?.issue1;
    const issue2 = this.duplicatePair?.issue2;

    return html`
      <sl-dialog
        label=${this._modalTitle}
        .open=${this.isOpen}
        @sl-show=${this.handleOpen}
        @sl-after-hide=${this.handleClose}
        @sl-initial-focus=${(e: Event) => e.preventDefault()}
        class="resolve-issue-dialog large"
      >
        ${
          this._resolutionStep !== 'merge' &&
          this._resolutionStep !== 'deconflict'
            ? html`
                <div class="comparison-container">
                  <div class="issue-panel">
                    <div class="issue-header">
                      <a href="${issue1?.url}" target="_blank"
                        >${issue1?.key}</a
                      >
                    </div>
                    <h3 class="issue-title">${issue1?.title}</h3>
                    <div class="issue-description">${issue1?.description}</div>
                  </div>
                  <div class="issue-panel">
                    <div class="issue-header">
                      <a href="${issue2?.url}" target="_blank"
                        >${issue2?.key}</a
                      >
                    </div>
                    <h3 class="issue-title">${issue2?.title}</h3>
                    <div class="issue-description">${issue2?.description}</div>
                  </div>
                </div>
              `
            : ''
        }
        ${content}
      </sl-dialog>
    `;
  }

  static styles = [
    unsafeCSS(consoleStyles),
    css`
      h2 {
        position: relative;
        text-align: left;
        font-size: 1.2rem;
        font-weight: 300;
        margin-bottom: 1rem;
      }

      .initial-options-group,
      .close-options-group {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 1rem;
        margin-top: 1rem;
      }

      @media (min-width: 992px) {
        .initial-options-group {
          grid-template-columns: repeat(4, 1fr);
        }
      }

      .action-card {
        border: 1px solid var(--sl-color-neutral-300);
        border-radius: var(--sl-border-radius-medium);
        padding: 1rem;
        cursor: pointer;
        transition: all 0.2s ease;
      }
      .action-card:hover {
        background-color: var(--sl-color-primary-50);
        border-color: var(--sl-color-primary-300);
      }
      .action-title {
        font-weight: var(--sl-font-weight-semibold);
      }
      .action-description {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-600);
        margin-top: 0.5rem;
      }
      .footer-buttons {
        display: flex;
        justify-content: space-between;
        margin-top: 1.5rem;
      }
      sl-radio-group {
        margin-bottom: 1rem;
      }
      sl-radio-group::part(label) {
        font-weight: bold;
      }

      .issue-header {
        font-size: var(--sl-font-size-medium);
        margin-bottom: var(--sl-spacing-small);
      }
      .issue-title {
        font-size: var(--sl-font-size-medium);
        font-weight: var(--sl-font-weight-semibold);
        margin-top: 0;
        margin-bottom: var(--sl-spacing-medium);
      }
      .issue-description {
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-neutral-700);
        background-color: var(--sl-color-neutral-100);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-medium);
        white-space: pre-wrap;
        word-wrap: break-word;
        max-height: 300px;
        overflow-y: auto;
      }
      .form-group {
        display: flex;
        flex-direction: column;
        gap: 1rem;
      }
      sl-textarea::part(textarea) {
        font-size: var(--sl-font-size-small);
        max-height: 200px;
        overflow-y: auto;
      }
      .loading-suggestion {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 2rem;
        gap: 1rem;
        color: var(--sl-color-neutral-600);
      }
    `,
  ];
}
