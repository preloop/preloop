import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import type { PropertyValues } from 'lit';
import {
  previewFlowContinuation,
  adoptFlowContinuation,
  FlowContinuationError,
  type FlowContinuationPreview,
  type ContinuationRecoveryMode,
} from '../api';
import { consoleDialogStyles } from '../styles/console-dialog';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

interface PublishedExecution {
  id: string;
  flow_id: string;
  status: string;
  result?: { pr_url?: string; continuation?: { thread_id?: string } };
  trigger_event_details?: { _thread_id?: string };
}

@customElement('preloop-execution-continuation')
export class PreloopExecutionContinuation extends LitElement {
  static styles = [
    consoleDialogStyles,
    css`
      :host {
        display: block;
      }
      .action {
        margin: var(--sl-spacing-medium) 0;
      }
      sl-alert,
      sl-checkbox {
        margin-top: var(--sl-spacing-medium);
      }
      p {
        line-height: 1.5;
      }
      a {
        color: var(--sl-color-primary-600);
        overflow-wrap: anywhere;
      }
      code {
        overflow-wrap: anywhere;
      }
    `,
  ];

  @property({ attribute: false }) execution?: PublishedExecution;
  @state() private open = false;
  @state() private preview: FlowContinuationPreview | null = null;
  @state() private loading = false;
  @state() private saving = false;
  @state() private acknowledged = false;
  @state() private error = '';
  @state() private adopted = false;
  private generation = 0;

  protected willUpdate(changes: PropertyValues) {
    const previous = changes.get('execution') as PublishedExecution | undefined;
    if (changes.has('execution') && previous?.id !== this.execution?.id) {
      this.generation++;
      this.open = false;
      this.preview = null;
      this.acknowledged = false;
      this.error = '';
      this.loading = false;
      this.saving = false;
      this.adopted = false;
    }
  }

  private eligible() {
    return (
      this.execution &&
      ['SUCCEEDED', 'COMPLETED'].includes(this.execution.status) &&
      Boolean(this.execution.result?.pr_url) &&
      !this.execution.result?.continuation?.thread_id &&
      !this.execution.trigger_event_details?._thread_id
    );
  }

  private mode(): ContinuationRecoveryMode | null {
    const preview = this.preview;
    if (
      !preview ||
      !preview.feedback_enabled ||
      !preview.feedback_readable ||
      !preview.artifact_upload_enabled ||
      preview.existing_thread_id
    )
      return null;
    if (
      preview.native_resume_available &&
      preview.allowed_recovery_modes.includes('native_resume')
    )
      return 'native_resume';
    return preview.allowed_recovery_modes.includes('published_branch_handoff')
      ? 'published_branch_handoff'
      : null;
  }

  private async loadPreview() {
    if (!this.execution || this.loading || this.saving) return;
    const executionId = this.execution.id;
    const generation = ++this.generation;
    this.open = true;
    this.loading = true;
    this.preview = null;
    this.acknowledged = false;
    this.error = '';
    try {
      const preview = await previewFlowContinuation(executionId);
      if (generation !== this.generation) return;
      if (
        preview.execution_id !== executionId ||
        preview.flow_id !== this.execution?.flow_id
      ) {
        throw new Error(
          'The preview does not match this execution. Reload and try again.'
        );
      }
      this.preview = preview;
    } catch (error) {
      if (generation === this.generation)
        this.error =
          error instanceof Error
            ? error.message
            : 'Unable to preview follow-up.';
    } finally {
      if (generation === this.generation) this.loading = false;
    }
  }

  private async adopt() {
    const mode = this.mode();
    if (
      !mode ||
      !this.preview?.head_sha ||
      this.loading ||
      this.saving ||
      (mode === 'published_branch_handoff' && !this.acknowledged)
    )
      return;
    const executionId = this.execution!.id;
    const generation = this.generation;
    this.saving = true;
    this.error = '';
    try {
      await adoptFlowContinuation(executionId, {
        recovery_mode: mode,
        expected_head_sha: this.preview.head_sha,
        acknowledge_fresh_conversation:
          mode === 'published_branch_handoff' && this.acknowledged,
      });
      if (generation !== this.generation) return;
      this.adopted = true;
      this.open = false;
    } catch (error) {
      if (generation !== this.generation) return;
      if (error instanceof FlowContinuationError && error.status === 409) {
        this.saving = false;
        await this.loadPreview();
        if (this.execution?.id === executionId && this.open) {
          this.error =
            'The PR or follow-up state changed. Review the refreshed preview and confirm again.';
        }
      } else if (
        !(error instanceof FlowContinuationError) ||
        error.status >= 500
      ) {
        this.saving = false;
        await this.loadPreview();
        if (
          this.execution?.id === executionId &&
          this.open &&
          !this.preview?.existing_thread_id
        ) {
          this.error =
            'Could not confirm whether follow-up was enabled. Check the refreshed preview before trying again.';
        }
      } else {
        this.error =
          error instanceof Error ? error.message : 'Unable to start follow-up.';
      }
    } finally {
      if (this.execution?.id === executionId) this.saving = false;
    }
  }

  private close() {
    if (this.saving) return;
    this.generation++;
    this.open = false;
    this.loading = false;
    this.acknowledged = false;
  }

  render() {
    if (!this.eligible()) return nothing;
    if (this.adopted)
      return html`<sl-alert open variant="success"
        >PR follow-up is configured. Repairs will use this PR and the flow's
        limits. Merge remains manual.</sl-alert
      >`;
    const preview = this.preview;
    const mode = this.mode();
    const prUrl =
      preview?.pr_url && /^https?:\/\//.test(preview.pr_url)
        ? preview.pr_url
        : '';
    return html`
      <div class="action">
        <sl-button data-continuation-preview @click=${this.loadPreview}
          >Set up PR follow-up</sl-button
        >
      </div>
      <sl-dialog
        label="Set up PR follow-up"
        .open=${this.open}
        @sl-request-close=${(event: Event) => {
          event.preventDefault();
          this.close();
        }}
      >
        ${
          this.loading
            ? html`<sl-spinner></sl-spinner>
                <p>Checking this PR and its saved execution state...</p>`
            : nothing
        }
        ${
          preview
            ? html`
                <p>
                  Enable review and CI repairs for
                  <a href=${prUrl} target="_blank" rel="noopener noreferrer"
                    >${preview.pr_url}</a
                  >.
                </p>
                <p>
                  Branch: <code>${preview.branch}</code><br />Current commit:
                  <code>${preview.head_sha}</code>
                </p>
                ${preview.existing_thread_id ? html`<sl-alert open>This PR already has follow-up configured (${preview.existing_thread_state || 'registered'}).</sl-alert>` : nothing}
                ${!preview.feedback_enabled ? html`<sl-alert open>Enable PR review and CI follow-up in <a href=${`/console/flows/${encodeURIComponent(preview.flow_id)}?edit=true`}>this flow's settings</a>, then reload this preview.</sl-alert>` : nothing}
                ${!preview.artifact_upload_enabled ? html`<sl-alert open>Saved execution state uploads must be enabled by your deployment administrator before follow-up can start.</sl-alert>` : nothing}
                ${!preview.feedback_readable ? html`<sl-alert open variant="warning">Review and CI access must be available before follow-up can start. ${preview.feedback_blocked_reason || ''}</sl-alert>` : nothing}
                ${preview.warnings.map((warning) => html`<sl-alert open variant="warning">${warning}</sl-alert>`)}
                ${mode === 'native_resume' ? html`<p>The next repair will continue the previous agent conversation using its saved checkpoint.</p>` : nothing}
                ${
                  mode === 'published_branch_handoff'
                    ? html` <p>
                          The previous conversation cannot be restored. The next
                          repair starts a fresh conversation from this published
                          branch and the current issue and PR context.
                        </p>
                        <sl-checkbox
                          data-continuation-ack
                          .checked=${this.acknowledged}
                          ?disabled=${this.saving}
                          @sl-change=${(event: Event) => {
                            this.acknowledged = (
                              event.target as HTMLInputElement
                            ).checked;
                          }}
                        >
                          I understand that follow-up starts a fresh
                          conversation and unpublished workspace changes will
                          not be recovered.
                        </sl-checkbox>`
                    : nothing
                }
                ${!mode && !preview.existing_thread_id ? html`<p>Follow-up is unavailable until the requirements above are met.</p>` : nothing}
                <p>
                  Future review findings or failed CI can start repairs within
                  the flow's limits. Merge remains manual.
                </p>
              `
            : nothing
        }
        ${this.error ? html`<sl-alert data-continuation-error variant="danger" open>${this.error}</sl-alert>` : nothing}
        <sl-button slot="footer" @click=${this.close} ?disabled=${this.saving}
          >Cancel</sl-button
        >
        <sl-button
          slot="footer"
          data-continuation-reload
          @click=${this.loadPreview}
          ?disabled=${this.loading || this.saving}
          >Reload preview</sl-button
        >
        <sl-button
          slot="footer"
          data-continuation-confirm
          variant="primary"
          @click=${this.adopt}
          ?loading=${this.saving}
          ?disabled=${this.loading || this.saving || !mode || !preview?.head_sha || (mode === 'published_branch_handoff' && !this.acknowledged)}
          >Enable follow-up for this PR</sl-button
        >
      </sl-dialog>
    `;
  }
}
