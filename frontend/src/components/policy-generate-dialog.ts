import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { fetchWithAuth, generatePolicy, generatePolicyFromAudit } from '../api';
import {
  unifiedYamlDiff,
  yamlDocumentsEqual,
} from '../utils/yaml-unified-diff';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';
import '@shoelace-style/shoelace/dist/components/tab-panel/tab-panel.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import { consoleDialogStyles } from '../styles/console-dialog';

@customElement('policy-generate-dialog')
export class PolicyGenerateDialog extends LitElement {
  @property({ type: Boolean }) open = false;
  @property({ type: String }) currentYaml = '';

  @state() private _prompt = '';
  @state() private _includeContext = true;
  @state() private _loading = false;
  @state() private _error = '';
  @state() private _generatedYaml = '';
  @state() private _unifiedDiff = '';
  @state() private _unchanged = false;
  @state() private _diffSummary = '';
  @state() private _warnings: string[] = [];
  @state() private _activeTab = 'prompt';
  @state() private _startDate = '';
  @state() private _endDate = '';

  static styles = [
    consoleDialogStyles,
    css`
      :host {
        --dialog-width: 720px;
      }

      sl-dialog::part(panel) {
        max-width: var(--dialog-width);
        width: 90vw;
      }

      sl-dialog::part(body) {
        padding: 1rem 1.5rem;
      }

      .description {
        color: var(--sl-color-neutral-600);
        font-size: 0.875rem;
        margin-bottom: 1rem;
        line-height: 1.5;
      }

      .form-group {
        margin-bottom: 1rem;
      }

      .form-row {
        display: flex;
        gap: 1rem;
      }

      .form-row > * {
        flex: 1;
      }

      .options-row {
        display: flex;
        align-items: center;
        gap: 1rem;
        margin-top: 0.75rem;
      }

      .options-row sl-switch {
        font-size: 0.875rem;
      }

      .yaml-preview {
        background: var(--sl-color-neutral-50);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: 1rem;
        margin-top: 1rem;
        position: relative;
        max-height: 400px;
        overflow: auto;
      }

      .yaml-preview pre,
      .yaml-diff pre {
        margin: 0;
        font-family: var(--sl-font-mono);
        font-size: 0.8125rem;
        line-height: 1.6;
        white-space: pre-wrap;
        word-break: break-word;
      }

      .yaml-diff {
        background: var(--sl-color-neutral-50);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: 4px;
        padding: 0.75rem 1rem;
        margin-top: 0.5rem;
        max-height: 360px;
        overflow: auto;
      }

      .models-link {
        color: var(--sl-color-primary-700);
      }

      .yaml-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-top: 1rem;
        margin-bottom: 0.5rem;
      }

      .yaml-header h4 {
        margin: 0;
        font-size: 0.875rem;
        color: var(--sl-color-neutral-700);
      }

      .yaml-actions {
        display: flex;
        gap: 0.5rem;
      }

      .loading-container {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 2rem;
        gap: 1rem;
        color: var(--sl-color-neutral-600);
      }

      .loading-container sl-spinner {
        font-size: 2rem;
        --track-width: 3px;
      }

      .warning-list {
        margin-top: 0.5rem;
      }

      .footer-actions {
        display: flex;
        gap: 0.75rem;
        justify-content: flex-end;
      }
    `,
  ];

  render() {
    return html`
      <sl-dialog
        label="Describe a change"
        ?open=${this.open}
        @sl-request-close=${this._handleClose}
        @sl-after-hide=${this._handleClose}
      >
        <p class="description">
          Describe the policy you want, or the edits to the current policy. The
          account default model proposes YAML. Review the diff, then Save.
          Nothing is applied until you Save.
        </p>

        <sl-tab-group @sl-tab-show=${this._handleTabChange}>
          <sl-tab
            slot="nav"
            panel="prompt"
            ?active=${this._activeTab === 'prompt'}
          >
            From Description
          </sl-tab>
          <sl-tab
            slot="nav"
            panel="audit"
            ?active=${this._activeTab === 'audit'}
          >
            From Audit Logs
          </sl-tab>

          <sl-tab-panel name="prompt">
            <div class="form-group">
              <sl-textarea
                label="Describe your policy"
                placeholder="e.g. Require approval for any payment over $500. Deny all file deletion tools. Allow read-only tools without approval."
                rows="5"
                .value=${this._prompt}
                @sl-input=${(e: Event) =>
                  (this._prompt = (e.target as HTMLTextAreaElement).value)}
                ?disabled=${this._loading}
              ></sl-textarea>
            </div>
            <div class="options-row">
              <sl-switch
                ?checked=${this._includeContext}
                @sl-change=${(e: Event) =>
                  (this._includeContext = (e.target as any).checked)}
                ?disabled=${this._loading}
              >
                Include current config as context
              </sl-switch>
            </div>
          </sl-tab-panel>

          <sl-tab-panel name="audit">
            <p class="description">
              Analyse your historical tool-call patterns and generate a policy
              that allows normal usage and flags outliers.
            </p>
            <div class="form-row">
              <sl-input
                label="Start date (optional)"
                type="date"
                .value=${this._startDate}
                @sl-input=${(e: Event) =>
                  (this._startDate = (e.target as HTMLInputElement).value)}
                ?disabled=${this._loading}
              ></sl-input>
              <sl-input
                label="End date (optional)"
                type="date"
                .value=${this._endDate}
                @sl-input=${(e: Event) =>
                  (this._endDate = (e.target as HTMLInputElement).value)}
                ?disabled=${this._loading}
              ></sl-input>
            </div>
          </sl-tab-panel>
        </sl-tab-group>

        ${
          this._loading
            ? html`
                <div class="loading-container">
                  <sl-spinner></sl-spinner>
                  <span>Generating policy…</span>
                </div>
              `
            : ''
        }
        ${
          this._error
            ? html`
                <sl-alert variant="danger" open>
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${this._error}
                  ${
                    this._isMissingModelError
                      ? html`
                          <div>
                            <a class="models-link" href="/console/ai-models"
                              >Open Models settings</a
                            >
                          </div>
                        `
                      : ''
                  }
                </sl-alert>
              `
            : ''
        }
        ${
          this._warnings.length > 0
            ? html`
                <div class="warning-list">
                  ${this._warnings.map(
                    (w) => html`
                      <sl-alert variant="warning" open>
                        <sl-icon
                          slot="icon"
                          name="exclamation-triangle"
                        ></sl-icon>
                        ${w}
                      </sl-alert>
                    `
                  )}
                </div>
              `
            : ''
        }
        ${
          this._unchanged
            ? html`
                <sl-alert variant="primary" open>
                  The generated policy matches the current export. Nothing to
                  save.
                </sl-alert>
              `
            : ''
        }
        ${
          this._generatedYaml && !this._unchanged
            ? html`
                <div class="yaml-header">
                  <h4>YAML diff vs current policy</h4>
                  <div class="yaml-actions">
                    <sl-copy-button
                      .value=${this._generatedYaml}
                    ></sl-copy-button>
                    <sl-button size="small" @click=${this._downloadYaml}>
                      <sl-icon slot="prefix" name="download"></sl-icon>
                      Download
                    </sl-button>
                  </div>
                </div>
                ${
                  this._diffSummary
                    ? html`<p class="description">${this._diffSummary}</p>`
                    : ''
                }
                <div class="yaml-diff">
                  <pre>${this._unifiedDiff}</pre>
                </div>
                <sl-details>
                  <span slot="summary">Full generated YAML</span>
                  <div class="yaml-preview">
                    <pre>${this._generatedYaml}</pre>
                  </div>
                </sl-details>
              `
            : ''
        }

        <div slot="footer" class="footer-actions">
          ${
            this._generatedYaml
              ? html`
                  <sl-button variant="default" @click=${this._discard}>
                    Discard
                  </sl-button>
                  <sl-button
                    variant="primary"
                    @click=${this._applyPolicy}
                    ?disabled=${this._unchanged}
                    ?loading=${this._loading}
                  >
                    Save
                  </sl-button>
                `
              : html`
                  <sl-button variant="default" @click=${this._handleClose}
                    >Cancel</sl-button
                  >
                  <sl-button
                    variant="primary"
                    @click=${this._generate}
                    ?loading=${this._loading}
                    ?disabled=${
                      this._activeTab === 'prompt' && !this._prompt.trim()
                    }
                  >
                    <sl-icon slot="prefix" name="magic"></sl-icon>
                    Generate
                  </sl-button>
                `
          }
        </div>
      </sl-dialog>
    `;
  }

  private _handleTabChange(e: CustomEvent) {
    this._activeTab = e.detail.name;
  }

  private get _isMissingModelError(): boolean {
    return /no ai model/i.test(this._error);
  }

  private async _loadCurrentYaml(): Promise<string> {
    if (this.currentYaml.trim()) {
      return this.currentYaml;
    }
    const response = await fetchWithAuth('/api/v1/policies/export?format=yaml');
    if (!response.ok) {
      return '';
    }
    return response.text();
  }

  private async _loadDiffSummary(yamlText: string): Promise<string> {
    try {
      const formData = new FormData();
      formData.append(
        'file',
        new File([yamlText], 'generated.yaml', { type: 'application/x-yaml' })
      );
      const response = await fetchWithAuth('/api/v1/policies/diff', {
        method: 'POST',
        body: formData,
      });
      if (!response.ok) {
        return '';
      }
      const data = await response.json();
      return data.summary || '';
    } catch {
      return '';
    }
  }

  private async _generate() {
    this._loading = true;
    this._error = '';
    this._generatedYaml = '';
    this._unifiedDiff = '';
    this._unchanged = false;
    this._diffSummary = '';
    this._warnings = [];

    try {
      let result;
      if (this._activeTab === 'audit') {
        result = await generatePolicyFromAudit({
          startDate: this._startDate || undefined,
          endDate: this._endDate || undefined,
        });
      } else {
        result = await generatePolicy({
          prompt: this._prompt,
          includeCurrentConfig: this._includeContext,
        });
      }

      this._generatedYaml = result.yaml;
      this._warnings = result.warnings || [];
      const current = await this._loadCurrentYaml();
      this._unchanged = yamlDocumentsEqual(current, result.yaml);
      this._unifiedDiff = this._unchanged
        ? ''
        : unifiedYamlDiff(current, result.yaml);
      this._diffSummary = this._unchanged
        ? ''
        : await this._loadDiffSummary(result.yaml);
    } catch (err: any) {
      this._error = err.message || 'Generation failed';
    } finally {
      this._loading = false;
    }
  }

  private _downloadYaml() {
    const blob = new Blob([this._generatedYaml], {
      type: 'application/x-yaml',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'generated-policy.yaml';
    a.click();
    URL.revokeObjectURL(url);
  }

  private _applyPolicy() {
    this.dispatchEvent(
      new CustomEvent('policy-apply', {
        detail: { yaml: this._generatedYaml },
        bubbles: true,
        composed: true,
      })
    );
  }

  private _discard() {
    this._generatedYaml = '';
    this._unifiedDiff = '';
    this._unchanged = false;
    this._diffSummary = '';
    this._warnings = [];
    this._error = '';
  }

  private _handleClose() {
    this.open = false;
    this._prompt = '';
    this._generatedYaml = '';
    this._unifiedDiff = '';
    this._unchanged = false;
    this._diffSummary = '';
    this._error = '';
    this._warnings = [];
    this._loading = false;
    this.dispatchEvent(new CustomEvent('closed'));
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'policy-generate-dialog': PolicyGenerateDialog;
  }
}
