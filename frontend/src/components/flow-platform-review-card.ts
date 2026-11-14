import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import consoleStyles from '../styles/console-styles.css?inline';
import { getTrackers, updateFlow } from '../api';
import {
  COMMIT_STATES,
  DEFAULT_STATUS_CONTEXT,
  STATUS_CONTEXT_PATTERN,
  platformReviewPrerequisites,
  prerequisitesMet,
  suggestedState,
  verdictEnum,
  type CommitState,
  type PlatformReviewConfig,
  type PrerequisiteCheck,
} from '../utils/platform-review';

const STATE_LABELS: Record<CommitState, string> = {
  success: 'Success (passes)',
  failure: 'Failure (blocks)',
  error: 'Error',
};

/**
 * "Post review on the pull request" for a read-only review flow.
 *
 * Turns `git_clone_config.platform_review` on and off, sets the commit status
 * each verdict produces (`status.verdict_states`), and lists what the posting
 * needs as checks with the reason when one is missing. Saves the one block
 * through `PUT /flows/{id}`; every other key of the flow is left as it is.
 * Enabling needs no new CI identities.
 *
 * Fires `flow-updated` with the saved flow in `detail.flow`.
 */
@customElement('flow-platform-review-card')
export class FlowPlatformReviewCard extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      .card-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sl-spacing-small);
      }
      p {
        margin: 0 0 var(--sl-spacing-small);
        font-size: var(--console-text-body, 14px);
      }
      .checks {
        list-style: none;
        margin: 0 0 var(--sl-spacing-medium);
        padding: 0;
      }
      .checks li {
        display: grid;
        grid-template-columns: 1.25rem 1fr;
        gap: var(--sl-spacing-x-small);
        padding: var(--sl-spacing-x-small) 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      .checks li:last-child {
        border-bottom: none;
      }
      .checks sl-icon {
        margin-top: 2px;
      }
      .check-ok {
        color: var(--sl-color-success-600);
      }
      .check-missing {
        color: var(--sl-color-danger-600);
      }
      .check-unknown {
        color: var(--console-meta-color);
      }
      .check-reason {
        display: block;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta, 13px);
      }
      .section-title {
        font-size: var(--console-text-card-title, 15px);
        font-weight: 600;
        margin: var(--sl-spacing-medium) 0 var(--sl-spacing-x-small);
      }
      .mapping {
        width: 100%;
        border-collapse: collapse;
        margin-bottom: var(--sl-spacing-small);
      }
      .mapping th,
      .mapping td {
        text-align: left;
        padding: var(--sl-spacing-x-small) var(--sl-spacing-small)
          var(--sl-spacing-x-small) 0;
        border-bottom: 1px solid var(--console-hairline);
        font-size: var(--console-text-body, 14px);
      }
      .mapping th {
        font-weight: 500;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta, 13px);
      }
      .mapping code {
        font-family: var(--sl-font-mono);
      }
      .fields {
        display: grid;
        gap: var(--sl-spacing-small);
        max-width: 28rem;
      }
      .actions {
        display: flex;
        gap: var(--sl-spacing-small);
        margin-top: var(--sl-spacing-medium);
      }
      .loading {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-x-small);
        color: var(--console-meta-color);
      }
    `,
  ];

  /** The flow as returned by `GET /flows/{id}`. */
  @property({ attribute: false }) flow: any = null;
  /** Whether the viewer may edit flows; otherwise the card is read-only. */
  @property({ type: Boolean }) editable = false;

  @state() private trackers: any[] | null = null;
  @state() private loadingTrackers = true;
  @state() private draft: PlatformReviewConfig = { enabled: false };
  @state() private saving = false;
  @state() private error = '';
  @state() private saved = false;
  private draftFlowId: string | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    void this.loadTrackers();
  }

  private async loadTrackers(): Promise<void> {
    this.loadingTrackers = true;
    try {
      const trackers = await getTrackers();
      this.trackers = Array.isArray(trackers) ? trackers : [];
    } catch {
      this.trackers = null;
    } finally {
      this.loadingTrackers = false;
    }
  }

  willUpdate(changed: Map<string, unknown>): void {
    if (changed.has('flow') && this.flow) {
      const id = String(this.flow.id ?? '');
      // Reset the draft when another flow is shown or after a save.
      if (id !== this.draftFlowId || !this.dirty) {
        this.draftFlowId = id;
        this.draft = this.storedBlock();
      }
    }
  }

  private storedBlock(): PlatformReviewConfig {
    const block = this.flow?.git_clone_config?.platform_review;
    if (!block || typeof block !== 'object') return { enabled: false };
    return JSON.parse(JSON.stringify(block));
  }

  private get dirty(): boolean {
    return (
      JSON.stringify(normalise(this.draft)) !==
      JSON.stringify(normalise(this.storedBlock()))
    );
  }

  private get verdicts(): string[] | null {
    return verdictEnum(this.flow?.result_schema);
  }

  private get checks(): PrerequisiteCheck[] {
    return platformReviewPrerequisites(
      this.flow || {},
      this.loadingTrackers ? [] : this.trackers
    );
  }

  /** A save the server would refuse, as a sentence; empty when it would pass. */
  private get blocker(): string {
    if (!this.draft.enabled) return '';
    const missing = this.checks.find((check) => check.ok === false);
    if (missing) return missing.reason;
    const status = this.draft.status;
    if (status) {
      const context = status.context ?? DEFAULT_STATUS_CONTEXT;
      if (!STATUS_CONTEXT_PATTERN.test(context)) {
        return 'The status name must start with a letter or digit and use only letters, digits, spaces and . _ / : - (up to 100 characters).';
      }
      const unmapped = (this.verdicts || []).find(
        (verdict) => !status.verdict_states?.[verdict]
      );
      if (unmapped) return `Choose a status for the verdict "${unmapped}".`;
    }
    return '';
  }

  private setEnabled(enabled: boolean): void {
    this.saved = false;
    this.draft = { ...this.draft, enabled };
  }

  private setStatusOn(on: boolean): void {
    this.saved = false;
    if (!on) {
      const { status: _status, ...rest } = this.draft;
      this.draft = { ...rest };
      return;
    }
    const verdict_states: Record<string, CommitState> = {};
    for (const verdict of this.verdicts || []) {
      verdict_states[verdict] = suggestedState(verdict);
    }
    this.draft = {
      ...this.draft,
      status: { context: DEFAULT_STATUS_CONTEXT, verdict_states },
    };
  }

  private setVerdictState(verdict: string, value: CommitState): void {
    this.saved = false;
    const status = this.draft.status!;
    this.draft = {
      ...this.draft,
      status: {
        ...status,
        verdict_states: { ...status.verdict_states, [verdict]: value },
      },
    };
  }

  private setStatusField(
    key: 'context' | 'incomplete_state',
    value: string
  ): void {
    this.saved = false;
    const status = { ...this.draft.status! } as Record<string, unknown>;
    if (key === 'incomplete_state' && !value) delete status.incomplete_state;
    else status[key] = value;
    this.draft = {
      ...this.draft,
      status: status as unknown as PlatformReviewConfig['status'],
    };
  }

  private async save(): Promise<void> {
    if (!this.flow?.id || this.blocker) return;
    this.saving = true;
    this.error = '';
    this.saved = false;
    try {
      const block = normalise(this.draft);
      const config = { ...(this.flow.git_clone_config || {}) };
      if (block.enabled || this.flow.git_clone_config?.platform_review) {
        config.platform_review = block;
      }
      const updated = await updateFlow(this.flow.id, {
        git_clone_config: config,
      });
      this.flow = updated;
      this.draft = this.storedBlock();
      this.saved = true;
      this.dispatchEvent(
        new CustomEvent('flow-updated', {
          detail: { flow: updated },
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Failed to save the flow';
    } finally {
      this.saving = false;
    }
  }

  private reset(): void {
    this.draft = this.storedBlock();
    this.error = '';
    this.saved = false;
  }

  private renderCheck(check: PrerequisiteCheck) {
    const icon =
      check.ok === true
        ? html`<sl-icon
            class="check-ok"
            name="check-circle"
            label="Met"
          ></sl-icon>`
        : check.ok === false
          ? html`<sl-icon
              class="check-missing"
              name="x-circle"
              label="Missing"
            ></sl-icon>`
          : html`<sl-icon
              class="check-unknown"
              name="info-circle"
              label="Confirm yourself"
            ></sl-icon>`;
    return html`<li data-check=${check.id} data-ok=${String(check.ok)}>
      ${icon}
      <span
        >${check.label}
        ${
          check.reason
            ? html`<span class="check-reason">${check.reason}</span>`
            : nothing
        }</span
      >
    </li>`;
  }

  private renderMapping() {
    const status = this.draft.status;
    const verdicts = this.verdicts;
    if (!status) return nothing;
    if (!verdicts) {
      return html`<p class="meta" data-empty="verdicts">
        The result schema declares no verdicts yet, so there is nothing to map.
      </p>`;
    }
    return html`
      <table class="mapping" data-mapping>
        <thead>
          <tr>
            <th scope="col">Verdict</th>
            <th scope="col">Commit status</th>
          </tr>
        </thead>
        <tbody>
          ${verdicts.map(
            (verdict) =>
              html`<tr>
                <td><code>${verdict}</code></td>
                <td>
                  ${
                    this.editable
                      ? html`<sl-select
                          size="small"
                          data-verdict=${verdict}
                          aria-label=${`Commit status for ${verdict}`}
                          placeholder="Choose"
                          .value=${status.verdict_states?.[verdict] ?? ''}
                          @sl-change=${(e: Event) =>
                            this.setVerdictState(
                              verdict,
                              (e.target as HTMLSelectElement)
                                .value as CommitState
                            )}
                        >
                          ${COMMIT_STATES.map(
                            (value) =>
                              html`<sl-option value=${value}
                                >${STATE_LABELS[value]}</sl-option
                              >`
                          )}
                        </sl-select>`
                      : (status.verdict_states?.[verdict] ?? 'not set')
                  }
                </td>
              </tr>`
          )}
        </tbody>
      </table>
      <div class="fields">
        <sl-input
          size="small"
          label="Status name"
          help-text="Keep it stable so a branch protection rule can require it."
          data-field="status_context"
          .value=${status.context ?? DEFAULT_STATUS_CONTEXT}
          ?disabled=${!this.editable}
          @sl-input=${(e: Event) =>
            this.setStatusField(
              'context',
              (e.target as HTMLInputElement).value
            )}
        ></sl-input>
        <sl-select
          size="small"
          label="When the run stops, fails or has no valid result"
          data-field="incomplete_state"
          .value=${status.incomplete_state ?? ''}
          ?disabled=${!this.editable}
          @sl-change=${(e: Event) =>
            this.setStatusField(
              'incomplete_state',
              (e.target as HTMLSelectElement).value
            )}
        >
          <sl-option value="">Post nothing</sl-option>
          <sl-option value="error">Error</sl-option>
          <sl-option value="failure">Failure</sl-option>
        </sl-select>
      </div>
    `;
  }

  render() {
    if (!this.flow) return nothing;
    const checks = this.checks;
    const enabled = this.draft.enabled;
    const storedOn = this.storedBlock().enabled;
    return html`
      <sl-card data-platform-review>
        <div slot="header" class="card-header">
          <span>Post review on the pull request</span>
          ${
            storedOn
              ? html`<sl-badge pill variant="success">On</sl-badge>`
              : html`<sl-badge pill variant="neutral">Off</sl-badge>`
          }
        </div>
        <p>
          After a run finishes and its result passes the result schema, Preloop
          posts one pull request review and, if you set one, a commit status on
          the reviewed commit. The run itself never holds a token that can write
          to GitHub. Turning this on needs no new CI identities.
        </p>
        <div class="section-title">What it needs</div>
        ${
          this.loadingTrackers
            ? html`<div class="loading" data-loading>
                <sl-spinner></sl-spinner> Checking connections…
              </div>`
            : html`<ul class="checks" data-checks>
                ${checks.map((check) => this.renderCheck(check))}
              </ul>`
        }
        ${
          this.editable
            ? html`<sl-switch
                data-field="enabled"
                .checked=${enabled}
                @sl-change=${(e: Event) =>
                  this.setEnabled((e.target as HTMLInputElement).checked)}
                >Post the review on the pull request</sl-switch
              >`
            : nothing
        }
        ${
          enabled
            ? html`
                <div class="section-title">Commit status</div>
                ${
                  this.editable
                    ? html`<sl-checkbox
                        data-field="status_on"
                        .checked=${Boolean(this.draft.status)}
                        ?disabled=${!this.verdicts}
                        @sl-change=${(e: Event) =>
                          this.setStatusOn(
                            (e.target as HTMLInputElement).checked
                          )}
                        >Set a commit status for each verdict</sl-checkbox
                      >`
                    : this.draft.status
                      ? nothing
                      : html`<p class="meta">No commit status is set.</p>`
                }
                ${this.renderMapping()}
              `
            : nothing
        }
        ${
          this.editable && this.dirty && this.blocker
            ? html`<sl-alert variant="warning" open data-blocker>
                <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
                ${this.blocker}
              </sl-alert>`
            : nothing
        }
        ${
          this.error
            ? html`<sl-alert variant="danger" open role="alert" data-error>
                <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                ${this.error}
              </sl-alert>`
            : nothing
        }
        ${
          this.saved
            ? html`<p class="meta" role="status" data-saved>Saved.</p>`
            : nothing
        }
        ${
          this.editable
            ? html`<div class="actions">
                <sl-button
                  variant="primary"
                  size="small"
                  data-action="save"
                  ?disabled=${!this.dirty || Boolean(this.blocker)}
                  ?loading=${this.saving}
                  @click=${() => this.save()}
                  >Save</sl-button
                >
                <sl-button
                  size="small"
                  data-action="reset"
                  ?disabled=${!this.dirty || this.saving}
                  @click=${() => this.reset()}
                  >Discard changes</sl-button
                >
              </div>`
            : nothing
        }
        ${
          !this.editable && !prerequisitesMet(checks) && storedOn
            ? html`<p class="meta">
                Ask someone who can edit flows to resolve the missing items.
              </p>`
            : nothing
        }
      </sl-card>
    `;
  }
}

/** The block as the server stores it: no empty optional keys. */
function normalise(block: PlatformReviewConfig): PlatformReviewConfig {
  const out: PlatformReviewConfig = { enabled: Boolean(block.enabled) };
  if (block.action && block.action !== 'comment') out.action = block.action;
  if (block.verdict_actions && Object.keys(block.verdict_actions).length) {
    out.verdict_actions = block.verdict_actions;
  }
  if (block.status) {
    const status: Record<string, unknown> = {
      context: block.status.context ?? DEFAULT_STATUS_CONTEXT,
      verdict_states: block.status.verdict_states || {},
    };
    if (block.status.incomplete_state) {
      status.incomplete_state = block.status.incomplete_state;
    }
    out.status = status as unknown as PlatformReviewConfig['status'];
  }
  return out;
}

declare global {
  interface HTMLElementTagNameMap {
    'flow-platform-review-card': FlowPlatformReviewCard;
  }
}
