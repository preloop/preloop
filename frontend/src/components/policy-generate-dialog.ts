import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { generatePolicy, generatePolicyFromAudit } from '../api';
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

@customElement('policy-generate-dialog')
export class PolicyGenerateDialog extends LitElement {
  @property({ type: Boolean }) open = false;

  @state() private _prompt = '';
  @state() private _includeContext = true;
  @state() private _loading = false;
  @state() private _error = '';
  @state() private _generatedYaml = '';
  @state() private _warnings: string[] = [];
  @state() private _activeTab = 'prompt';
  @state() private _startDate = '';
  @state() private _endDate = '';

  static styles = css`
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

    .yaml-preview pre {
      margin: 0;
      font-family: var(--sl-font-mono);
      font-size: 0.8125rem;
      line-height: 1.6;
      white-space: pre-wrap;
      word-break: break-word;
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
  `;

  render() {
    return html`
      <sl-dialog
        label="Generate Policy with AI"
        ?open=${this.open}
        @sl-request-close=${this._handleClose}
        @sl-after-hide=${this._handleClose}
      >
        <p class="description">
          Describe the policy you want, or generate one from your audit logs. An
          AI model configured on your account will generate valid policy YAML.
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
          this._generatedYaml
            ? html`
                <div class="yaml-header">
                  <h4>Generated Policy</h4>
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
                <div class="yaml-preview">
                  <pre>${this._generatedYaml}</pre>
                </div>
              `
            : ''
        }

        <div slot="footer" class="footer-actions">
          <sl-button variant="default" @click=${this._handleClose}
            >Cancel</sl-button
          >
          ${
            this._generatedYaml
              ? html`
                  <sl-button
                    variant="primary"
                    @click=${this._applyPolicy}
                    ?loading=${this._loading}
                  >
                    <sl-icon slot="prefix" name="check-lg"></sl-icon>
                    Apply Policy
                  </sl-button>
                `
              : html`
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

  private async _generate() {
    this._loading = true;
    this._error = '';
    this._generatedYaml = '';
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

  private _handleClose() {
    this.open = false;
    this._prompt = '';
    this._generatedYaml = '';
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
