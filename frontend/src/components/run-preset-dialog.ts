import { LitElement, css, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';

import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import { consoleDialogStyles } from '../styles/console-dialog';
import {
  runPresetOnTarget,
  RunPresetError,
  type RunPresetResponse,
  type RunPresetSlug,
  type RunPresetTarget,
} from '../api';

export interface RunPresetDialogOptions {
  presetSlug: RunPresetSlug;
  target?: RunPresetTarget;
  targets?: RunPresetTarget[];
  issueKey: string;
  role?: 'implementer' | 'reviewer' | 'triage';
}

/**
 * Resolve-or-create dialog for ad hoc preset runs.
 *
 * Probes with confirm_create false, then shows either the first-time create
 * copy or "Run {flow} on {key}?". Confirm sends confirm_create true.
 */
@customElement('run-preset-dialog')
export class RunPresetDialog extends LitElement {
  @state() private isOpen = false;
  @state() private loading = false;
  @state() private submitting = false;
  @state() private mode: 'create' | 'run' | 'disabled' | 'error' = 'run';
  @state() private flowName = '';
  @state() private flowId = '';
  @state() private issueKey = '';
  @state() private role: 'implementer' | 'reviewer' | 'triage' = 'implementer';
  @state() private modelAlert = false;
  @state() private errorMessage = '';

  private options: RunPresetDialogOptions | null = null;

  static styles = [
    consoleDialogStyles,
    css`
      :host {
        display: contents;
      }
      .body {
        color: var(--sl-color-neutral-800);
        white-space: pre-line;
      }
      .model-alert {
        margin-top: var(--sl-spacing-medium);
      }
      .disabled-link {
        margin-top: var(--sl-spacing-small);
      }
      .loading-row {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
      }
    `,
  ];

  async start(options: RunPresetDialogOptions): Promise<void> {
    this.options = options;
    this.issueKey = options.issueKey;
    this.role = options.role || 'implementer';
    this.modelAlert = false;
    this.errorMessage = '';
    this.flowName = '';
    this.flowId = '';
    this.mode = 'run';
    this.isOpen = true;
    this.loading = true;
    try {
      const result = await runPresetOnTarget({
        preset_slug: options.presetSlug,
        target: options.target,
        targets: options.targets,
        confirm_create: false,
      });
      this.flowName = result.flow_name;
      this.flowId = result.flow_id;
      this.mode = 'run';
    } catch (error) {
      this.applyError(error);
    } finally {
      this.loading = false;
    }
  }

  private applyError(error: unknown): void {
    if (error instanceof RunPresetError) {
      if (error.code === 'flow_missing') {
        this.flowName = error.flowName || this.presetDisplayName();
        this.mode = 'create';
        return;
      }
      if (error.code === 'flow_disabled') {
        this.flowId = error.flowId || '';
        this.flowName = error.flowName || this.presetDisplayName();
        this.mode = 'disabled';
        return;
      }
      if (error.status === 422) {
        this.modelAlert = true;
        this.mode = this.mode === 'create' ? 'create' : 'run';
        return;
      }
      this.errorMessage = error.message;
      this.mode = 'error';
      return;
    }
    this.errorMessage =
      error instanceof Error ? error.message : 'Could not start the run.';
    this.mode = 'error';
  }

  private presetDisplayName(): string {
    if (this.options?.presetSlug === 'pull-request-reviewer') {
      return 'Pull Request Reviewer';
    }
    if (this.options?.presetSlug === 'issue-triage-assistant') {
      return 'Issue Triage Assistant';
    }
    return 'Automated Issue Implementation';
  }

  private titleText(): string {
    if (this.mode === 'create') {
      if (this.role === 'reviewer') {
        return 'Create the reviewer flow?';
      }
      if (this.role === 'triage') {
        return 'Create the triage flow?';
      }
      return 'Create the implementer flow?';
    }
    if (this.mode === 'disabled') {
      return `${this.flowName} is disabled`;
    }
    if (this.mode === 'error') {
      return 'Could not start the run';
    }
    return `Run ${this.flowName || this.presetDisplayName()} on ${this.issueKey}?`;
  }

  private bodyText(): string {
    if (this.mode === 'create') {
      return (
        `This account has no ${this.flowName || this.presetDisplayName()} ` +
        `flow yet. Preloop will create one from the preset with your default ` +
        `AI model and runner pool, then run it on ${this.issueKey}.`
      );
    }
    if (this.mode === 'disabled') {
      return `The ${this.flowName} flow is disabled. Enable it to run.`;
    }
    if (this.mode === 'error') {
      return this.errorMessage;
    }
    return '';
  }

  private confirmLabel(): string {
    return this.mode === 'create' ? 'Create and run' : 'Run';
  }

  private showConfirm(): boolean {
    return this.mode === 'create' || this.mode === 'run';
  }

  private close(): void {
    this.isOpen = false;
    this.submitting = false;
  }

  private async confirm(): Promise<void> {
    if (!this.options || this.submitting) return;
    this.submitting = true;
    this.modelAlert = false;
    try {
      const result = await runPresetOnTarget({
        preset_slug: this.options.presetSlug,
        target: this.options.target,
        targets: this.options.targets,
        confirm_create: true,
      });
      this.close();
      this.showRunResultToast(result);
    } catch (error) {
      this.submitting = false;
      if (error instanceof RunPresetError && error.status === 422) {
        this.modelAlert = true;
        return;
      }
      this.applyError(error);
    }
  }

  private showRunResultToast(result: RunPresetResponse): void {
    const items = result.results;
    const failures = items?.filter((item) => item.error).length || 0;
    const created = items?.filter((item) => item.execution_id).length || 0;
    const alert = Object.assign(document.createElement('sl-alert'), {
      variant: failures ? 'warning' : 'success',
      duration: Infinity,
      closable: true,
    });
    const icon = document.createElement('sl-icon');
    icon.slot = 'icon';
    icon.setAttribute(
      'name',
      failures ? 'exclamation-triangle' : 'check2-circle'
    );
    alert.append(icon);
    const summary = items
      ? (created
          ? `${created} ${created === 1 ? 'run' : 'runs'} created.`
          : 'No runs were created.') +
        (failures
          ? ` ${failures} ${failures === 1 ? 'issue needs' : 'issues need'} attention.`
          : '')
      : 'Run started';
    alert.append(document.createTextNode(summary));
    const addRunLink = (parent: HTMLElement, url: string): void => {
      const view = document.createElement('sl-button');
      view.setAttribute('size', 'small');
      view.setAttribute('variant', 'text');
      view.textContent = 'View run';
      view.addEventListener('click', () => {
        Router.go(url);
        void (alert as unknown as { hide: () => Promise<void> }).hide();
      });
      parent.append(view);
    };
    if (items) {
      for (const [index, item] of items.entries()) {
        const line = document.createElement('div');
        line.append(
          document.createTextNode(
            `Issue ${index + 1}: ${item.error || 'Run created.'} `
          )
        );
        if (item.execution_url) addRunLink(line, item.execution_url);
        alert.append(line);
      }
    } else if (result.execution_url) {
      addRunLink(alert, result.execution_url);
    }
    const edit = document.createElement('sl-button');
    edit.setAttribute('size', 'small');
    edit.setAttribute('variant', 'text');
    edit.textContent = 'Edit flow';
    edit.addEventListener('click', () => {
      Router.go(`/console/flows/${result.flow_id}`);
      void (alert as unknown as { hide: () => Promise<void> }).hide();
    });
    alert.append(edit);
    document.body.append(alert);
    void (alert as unknown as { toast: () => Promise<void> }).toast();
  }

  render() {
    return html`
      <sl-dialog
        label=${this.titleText()}
        ?open=${this.isOpen}
        @sl-after-hide=${(event: Event) => {
          if (event.target !== event.currentTarget) return;
          this.isOpen = false;
        }}
      >
        ${
          this.loading
            ? html`<div class="loading-row">
                <sl-spinner></sl-spinner>
                Checking for an existing flow
              </div>`
            : html`
                ${this.bodyText() ? html`<div class="body">${this.bodyText()}</div>` : nothing}
                ${
                  this.mode === 'disabled' && this.flowId
                    ? html`<div class="disabled-link">
                        <sl-button
                          variant="text"
                          size="small"
                          href="/console/flows/${this.flowId}"
                          @click=${() => this.close()}
                          >Edit flow</sl-button
                        >
                      </div>`
                    : nothing
                }
                ${
                  this.modelAlert
                    ? html`<sl-alert class="model-alert" variant="warning" open>
                        No usable AI model for this preset. Add one under
                        Models, then try again.
                        <sl-button
                          variant="text"
                          size="small"
                          href="/console/ai-models"
                          @click=${() => this.close()}
                          >Models</sl-button
                        >
                      </sl-alert>`
                    : nothing
                }
              `
        }
        <sl-button slot="footer" @click=${() => this.close()}>Cancel</sl-button>
        ${
          this.showConfirm() && !this.loading
            ? html`<sl-button
                slot="footer"
                variant="primary"
                ?loading=${this.submitting}
                data-testid="run-preset-confirm"
                @click=${() => this.confirm()}
              >
                ${this.confirmLabel()}
              </sl-button>`
            : nothing
        }
      </sl-dialog>
    `;
  }
}

let singleton: RunPresetDialog | null = null;

function getRunPresetDialog(): RunPresetDialog {
  if (singleton?.isConnected) return singleton;
  singleton = document.createElement('run-preset-dialog') as RunPresetDialog;
  document.body.append(singleton);
  return singleton;
}

export function openRunPresetDialog(
  options: RunPresetDialogOptions
): Promise<void> {
  return getRunPresetDialog().start(options);
}

export function resetRunPresetDialogForTests(): void {
  singleton?.remove();
  singleton = null;
}

declare global {
  interface HTMLElementTagNameMap {
    'run-preset-dialog': RunPresetDialog;
  }
}
