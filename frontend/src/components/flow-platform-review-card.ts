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
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import consoleStyles from '../styles/console-styles.css?inline';
import {
  FlowRevisionConflictError,
  getFlow,
  getTrackers,
  updateFlow,
} from '../api';
import {
  COMMIT_STATES,
  DEFAULT_STATUS_CONTEXT,
  STATUS_CONTEXT_PATTERN,
  findingFieldAppearances,
  platformReviewMode,
  platformReviewPrerequisites,
  prerequisitesMet,
  suggestedState,
  verdictEnum,
  type CommitState,
  type PlatformReviewConfig,
  type PlatformReviewMode,
  type PrerequisiteCheck,
} from '../utils/platform-review';

const MODE_LABELS: Record<PlatformReviewMode, string> = {
  off: 'Off',
  automatic: 'Automatic',
  manual: 'Manual',
};

const MODE_HELP: Record<PlatformReviewMode, string> = {
  off: 'Nothing is posted.',
  automatic: 'Posted when each run finishes.',
  manual:
    "Posted only when someone, or the run's own CI identity, calls the publish endpoint for a finished run.",
};

const EVENT_REVIEW_TYPES = new Set([
  'pull_request_opened',
  'pull_request_updated',
  'pull_request_reopened',
  'pull_request_ready_for_review',
]);

const EVIDENCE_HELP: Record<'any' | 'captured' | 'complete', string> = {
  any: "Post whether or not the run's tool evidence is complete.",
  captured:
    'Post only when every model request has an evidence record, and the only incomplete records are masked credentials.',
  complete:
    'Post only when every model request has an evidence record and no record is incomplete, including masked credentials.',
};

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
      .project-list {
        display: grid;
        gap: var(--sl-spacing-2x-small);
        max-height: 300px;
        overflow-y: auto;
        margin: var(--sl-spacing-x-small) 0 var(--sl-spacing-small);
      }
      .project-list code {
        font-family: var(--sl-font-mono);
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
  @state() private projectFilter = '';
  private draftFlowId: string | null = null;

  private get eventReviewTrigger(): boolean {
    const types = this.flow?.trigger_event_types;
    return (
      Array.isArray(types) &&
      types.some((type: string) => EVENT_REVIEW_TYPES.has(type))
    );
  }

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
        this.draft = this.initialDraft();
        this.projectFilter = '';
      }
    }
  }

  private storedBlock(): PlatformReviewConfig {
    const block = this.flow?.git_clone_config?.platform_review;
    if (!block || typeof block !== 'object') return { enabled: false };
    return JSON.parse(JSON.stringify(block));
  }

  /** The flow's projects, in order, with the name the card shows. */
  private get admittedProjects(): { id: string; name: string }[] {
    const config = this.flow?.git_clone_config || {};
    const repositories: any[] = Array.isArray(config.repositories)
      ? config.repositories
      : [];
    const ids: string[] = Array.isArray(this.flow?.trigger_project_ids)
      ? this.flow.trigger_project_ids.map((id: unknown) => String(id))
      : repositories
          .map((repo) => String(repo?.project_id ?? ''))
          .filter((id) => id);
    return [...new Set(ids)].map((id) => {
      const name = repositories.find(
        (repo) => String(repo?.project_id) === id
      )?.identifier;
      return { id, name: typeof name === 'string' && name ? name : '' };
    });
  }

  /** Stored posting ids that are no longer one of the flow's projects. */
  private get staleProjectIds(): string[] {
    const listed = this.storedBlock().project_ids;
    if (!Array.isArray(listed)) return [];
    const admitted = new Set(this.admittedProjects.map((p) => p.id));
    return listed.filter((id) => !admitted.has(String(id)));
  }

  /** The stored block, without posting ids that are not on the flow. */
  private initialDraft(): PlatformReviewConfig {
    const block = this.storedBlock();
    const stale = this.staleProjectIds;
    if (stale.length && Array.isArray(block.project_ids)) {
      block.project_ids = block.project_ids.filter((id) => !stale.includes(id));
    }
    return block;
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

  private renderFindingFields() {
    const plan = findingFieldAppearances(this.flow?.result_schema);
    if (!plan.declared) {
      return html`<p class="meta" data-finding-fields>
        The result schema does not declare finding fields. A finding in the
        result is still posted from the fields it carries.
      </p>`;
    }
    return html`
      <ul class="checks" data-finding-fields>
        ${
          plan.fields.length
            ? plan.fields.map(
                (field) =>
                  html`<li data-finding-field=${field.field}>
                    <span></span>
                    <span><code>${field.field}</code> ${field.appearsAs}</span>
                  </li>`
              )
            : html`<li>
                <span></span><span>No finding fields declared.</span>
              </li>`
        }
      </ul>
      ${
        plan.hasText
          ? nothing
          : html`<sl-alert variant="warning" open data-finding-text>
              <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
              The schema declares no text field for a finding, so the review has
              no problem statement. You can still save.
            </sl-alert>`
      }
    `;
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
    const listed = this.draft.project_ids;
    if (Array.isArray(listed)) {
      const admitted = new Set(this.admittedProjects.map((p) => p.id));
      const outside = listed.find((id) => !admitted.has(id));
      if (outside) {
        return `${outside} is not one of this flow's projects, so Preloop cannot post for it.`;
      }
      const duplicate = listed.find((id, i) => listed.indexOf(id) !== i);
      if (duplicate) return `${duplicate} is listed twice.`;
    }
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

  private setMode(mode: PlatformReviewMode): void {
    this.saved = false;
    const { mode: _mode, ...rest } = this.draft;
    this.draft =
      mode === 'off'
        ? { ...rest, enabled: false }
        : mode === 'manual'
          ? { ...rest, enabled: true, mode: 'manual' }
          : { ...rest, enabled: true };
  }

  private setProjectScope(scope: 'all' | 'selected'): void {
    this.saved = false;
    const { project_ids: _ids, ...rest } = this.draft;
    // Starting from every project changes nothing until one is unticked.
    this.draft =
      scope === 'all'
        ? { ...rest }
        : {
            ...rest,
            project_ids: Array.isArray(_ids)
              ? _ids
              : this.admittedProjects.map((p) => p.id),
          };
  }

  private setProjectSelected(id: string, selected: boolean): void {
    this.saved = false;
    const current = this.draft.project_ids || [];
    const chosen = new Set(selected ? [...current, id] : current);
    if (!selected) chosen.delete(id);
    // Keep the flow's order so the stored list does not churn.
    const ids = this.admittedProjects
      .map((p) => p.id)
      .filter((pid) => chosen.has(pid));
    this.draft = { ...this.draft, project_ids: ids };
  }

  private setEvidence(value: 'any' | 'captured' | 'complete'): void {
    this.saved = false;
    const { evidence: _evidence, ...rest } = this.draft;
    this.draft = value === 'any' ? { ...rest } : { ...rest, evidence: value };
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
      // git_clone_config is replaced whole, so the write is conditional
      // on the revision this card was rendered from.
      const updated = await updateFlow(
        this.flow.id,
        { git_clone_config: config },
        { revision: this.flow.revision }
      );
      this.flow = updated;
      this.draft = this.initialDraft();
      this.saved = true;
      this.dispatchEvent(
        new CustomEvent('flow-updated', {
          detail: { flow: updated },
          bubbles: true,
          composed: true,
        })
      );
    } catch (error) {
      if (error instanceof FlowRevisionConflictError && this.flow?.id) {
        await this.adoptLatestAfterConflict(this.flow.id);
        return;
      }
      this.error =
        error instanceof Error ? error.message : 'Failed to save the flow';
    } finally {
      this.saving = false;
    }
  }

  /**
   * After a 412, load the flow as it is now and keep the draft, so Save
   * applies the same change on top of the latest version instead of
   * failing again on the stale revision.
   */
  private async adoptLatestAfterConflict(flowId: string): Promise<void> {
    try {
      const latest = await getFlow(flowId);
      this.flow = latest;
      this.dispatchEvent(
        new CustomEvent('flow-updated', {
          detail: { flow: latest },
          bubbles: true,
          composed: true,
        })
      );
      this.error =
        'This flow was changed after this card loaded, so nothing was ' +
        'saved. The card now uses the latest saved flow and keeps your ' +
        'changes here: check them and select Save again.';
    } catch {
      this.error =
        'This flow was changed after this card loaded, so nothing was ' +
        'saved. Reload the page to see the latest version.';
    }
  }

  private reset(): void {
    this.draft = this.initialDraft();
    this.projectFilter = '';
    this.error = '';
    this.saved = false;
  }

  /** "Posts for N of M repositories", following the draft. */
  private renderPostingProjects() {
    const listed = this.draft.project_ids;
    if (!this.draft.enabled || !Array.isArray(listed)) return nothing;
    const projects = this.admittedProjects;
    const chosen = projects.filter((p) => listed.includes(p.id));
    const noun = projects.length === 1 ? 'repository' : 'repositories';
    const names = chosen.map((p) => p.name).filter((name) => name);
    const which =
      names.length && names.length <= 5 ? `: ${names.join(', ')}` : '';
    return html`<p class="meta" data-posting-projects>
      ${`Posts for ${chosen.length} of ${projects.length} ${noun}${which}.`}
    </p>`;
  }

  /** Every repository of the flow, or only the ticked ones. */
  private renderPostingProjectsEditor() {
    const projects = this.admittedProjects;
    const stale = this.staleProjectIds;
    const staleNote = stale.length
      ? html`<sl-alert variant="warning" open data-stale-projects>
          <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
          ${`Not on this flow any more, so not selected and removed from the list on the next save: ${stale.join(', ')}.`}
        </sl-alert>`
      : nothing;
    if (!this.draft.enabled || projects.length < 2) {
      return this.draft.enabled ? staleNote : nothing;
    }
    const listed = this.draft.project_ids;
    const selected = Array.isArray(listed);
    const filter = this.projectFilter.trim().toLowerCase();
    const shown = filter
      ? projects.filter((p) =>
          `${p.name} ${p.id}`.toLowerCase().includes(filter)
        )
      : projects;
    const help = [
      'Preloop posts only for the selected repositories; the others keep running and keep their results.',
      this.eventReviewTrigger
        ? 'Pull request events start reviews only for the selected repositories.'
        : '',
      'A repository added to the flow later is not selected.',
    ]
      .filter((sentence) => sentence)
      .join(' ');
    return html`
      <sl-radio-group
        label="Repositories to post for"
        data-field="project_scope"
        .value=${selected ? 'selected' : 'all'}
        @sl-change=${(e: Event) =>
          this.setProjectScope(
            (e.target as HTMLInputElement).value as 'all' | 'selected'
          )}
      >
        <sl-radio value="all" ?disabled=${!this.editable}
          >Every repository of the flow</sl-radio
        >
        <sl-radio value="selected" ?disabled=${!this.editable}
          >Only selected repositories</sl-radio
        >
      </sl-radio-group>
      ${
        selected
          ? html`
              <p class="meta" data-posting-projects-help>${help}</p>
              ${
                projects.length > 10
                  ? html`<sl-input
                      size="small"
                      type="search"
                      clearable
                      placeholder="Filter repositories"
                      aria-label="Filter repositories"
                      data-field="project_filter"
                      .value=${this.projectFilter}
                      @sl-input=${(e: Event) => {
                        this.projectFilter = (
                          e.target as HTMLInputElement
                        ).value;
                      }}
                      @sl-clear=${() => {
                        this.projectFilter = '';
                      }}
                    ></sl-input>`
                  : nothing
              }
              <div
                class="project-list"
                role="group"
                aria-label="Selected repositories"
                data-field="project_ids"
              >
                ${shown.map(
                  (p) =>
                    html`<sl-checkbox
                      data-project=${p.id}
                      .checked=${listed!.includes(p.id)}
                      ?disabled=${!this.editable}
                      @sl-change=${(e: Event) =>
                        this.setProjectSelected(
                          p.id,
                          (e.target as HTMLInputElement).checked
                        )}
                      >${p.name || html`<code>${p.id}</code>`}</sl-checkbox
                    >`
                )}
                ${
                  shown.length
                    ? nothing
                    : html`<p class="meta">No repository matches.</p>`
                }
              </div>
              ${
                listed!.length === 0
                  ? html`<sl-alert variant="warning" open data-posting-none>
                      <sl-icon
                        slot="icon"
                        name="exclamation-triangle"
                      ></sl-icon>
                      No repository is selected. Nothing will be posted, and no
                      pull request event will start a review.
                    </sl-alert>`
                  : nothing
              }
            `
          : nothing
      }
      ${staleNote}
    `;
  }

  /** Before saving off: the server clears a stored posting list. */
  private renderPostingProjectsCleared() {
    const stored = this.storedBlock();
    if (
      this.draft.enabled ||
      !stored.enabled ||
      !Array.isArray(stored.project_ids)
    ) {
      return nothing;
    }
    return html`<p class="meta" data-posting-projects-cleared>
      Turning posting off clears the list of repositories Preloop posts for.
      Turned on again, it posts for every repository until you select them
      again.
    </p>`;
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
    const mode = platformReviewMode(this.draft);
    const storedMode = platformReviewMode(this.storedBlock());
    const storedOn = storedMode !== 'off';
    return html`
      <sl-card data-platform-review>
        <div slot="header" class="card-header">
          <span>Post review on the pull request</span>
          ${
            storedOn
              ? html`<sl-badge pill variant="success" data-mode-badge
                  >${MODE_LABELS[storedMode]}</sl-badge
                >`
              : html`<sl-badge pill variant="neutral" data-mode-badge
                  >Off</sl-badge
                >`
          }
        </div>
        <p>
          Preloop posts one pull request review and, if you set one, a commit
          status on the reviewed commit, from a finished run whose result passes
          the result schema. The run itself never holds a token that can write
          to GitHub. Turning this on needs no new CI identities.
        </p>
        <div class="section-title">What the review shows</div>
        ${this.renderFindingFields()}
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
            ? html`<sl-radio-group
                label="When to post"
                data-field="mode"
                .value=${mode}
                help-text=${MODE_HELP[mode]}
                @sl-change=${(e: Event) =>
                  this.setMode(
                    (e.target as HTMLInputElement).value as PlatformReviewMode
                  )}
              >
                <sl-radio value="off">Off</sl-radio>
                <sl-radio value="automatic">When the run finishes</sl-radio>
                <sl-radio value="manual">Only when requested</sl-radio>
              </sl-radio-group>`
            : html`<p class="meta" data-mode>${MODE_HELP[storedMode]}</p>`
        }
        ${this.renderPostingProjects()} ${this.renderPostingProjectsEditor()}
        ${this.renderPostingProjectsCleared()}
        ${
          this.eventReviewTrigger
            ? html`<p data-event-review>
                A pull request event on this flow's GitHub tracker starts the
                run. No CI identity is required.
              </p>`
            : nothing
        }
        ${
          enabled
            ? html`<sl-select
                label="Tool evidence"
                data-field="evidence"
                help-text=${EVIDENCE_HELP[this.draft.evidence || 'any']}
                .value=${this.draft.evidence || 'any'}
                ?disabled=${!this.editable}
                @sl-change=${(event: Event) =>
                  this.setEvidence(
                    (event.target as HTMLInputElement).value as
                      'any' | 'captured' | 'complete'
                  )}
              >
                <sl-option value="any">Any</sl-option>
                <sl-option value="captured">Captured</sl-option>
                <sl-option value="complete">Complete</sl-option>
              </sl-select>`
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
  // Automatic is what a block without a mode means; only manual is stored.
  if (block.enabled && block.mode === 'manual') out.mode = 'manual';
  if (block.evidence === 'captured' || block.evidence === 'complete') {
    out.evidence = block.evidence;
  }
  if (block.action && block.action !== 'comment') out.action = block.action;
  // The posting list is only valid while on; the server clears it on off.
  if (block.enabled && Array.isArray(block.project_ids)) {
    out.project_ids = [...block.project_ids];
  }
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
