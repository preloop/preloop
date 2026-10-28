import { LitElement, html, css, nothing } from 'lit';
import { live } from 'lit/directives/live.js';
import { customElement, property, state } from 'lit/decorators.js';
import {
  getFeatures,
  getUserProfile,
  hasPermission,
  onApiCachesInvalidated,
} from '../api';
import {
  getReportRecipients,
  saveReportRecipients,
  type ReportRecipients,
  type ReportRecipientsChanges,
  type ReportRole,
} from '../delivery-report-recipients-api';
import {
  clearDeliveryReportPolicy,
  getDeliveryReportPolicy,
  getDeliveryReportSubscription,
  saveDeliveryReportPolicy,
  saveDeliveryReportSubscription,
  type DeliveryReportPolicy,
  type DeliveryReportPolicyLayer,
  type DeliveryReportSubscription,
  type PolicyOverrides,
} from '../delivery-report-policy-api';
import { showToast } from './confirm-dialog';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';

const RECIPIENT_POLICY_FIELDS = [
  'recipients',
  'sponsoring_lead_user_id',
  'per_member_updates',
] as const;
const ROLE_LABELS = {
  owner: 'Owner',
  lead: 'Lead',
  member: 'Member',
  viewer: 'Viewer',
};
const DELIVERY_LABELS = {
  full_report: 'Full report',
  short_update: 'Short update',
  none: 'No email',
};

const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const WEEKDAY_LABELS: Record<string, string> = {
  mon: 'Monday',
  tue: 'Tuesday',
  wed: 'Wednesday',
  thu: 'Thursday',
  fri: 'Friday',
  sat: 'Saturday',
  sun: 'Sunday',
};

function usd(value: number): string {
  return `$${value.toFixed(2)}`;
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * The Delivery Report policy card (#2020).
 *
 * Without `project-id` it edits the account layer on Account settings:
 * whether the report goes out, cadence, assessment and its budgets, tracker
 * writes and recipients. With `project-id` it edits one repository's layer
 * on the tracker's Projects tab: on or off, the open-item windows and the
 * criteria source, with a reset back to the account policy.
 */
@customElement('delivery-report-policy-settings')
export class DeliveryReportPolicySettings extends LitElement {
  @property({ attribute: 'project-id' }) projectId = '';
  @state() private layer: DeliveryReportPolicyLayer | null = null;
  @state() private draft: DeliveryReportPolicy | null = null;
  @state() private canManage = false;
  @state() private memberUpdatesAvailable = false;
  @state() private recipientsVisible = false;
  @state() private canManageRecipients = false;
  @state() private recipients: ReportRecipients | null = null;
  @state() private recipientDraft: ReportRecipients | null = null;
  @state() private roleChanges: Record<string, ReportRole | null> = {};
  @state() private recipientError = '';
  private stopInvalidation?: () => void;
  @state() private saving = false;
  @state() private error = '';
  @state() private subscription: DeliveryReportSubscription | null = null;
  private generation = 0;

  static styles = css`
    :host {
      display: block;
      margin-bottom: 2rem;
    }
    section {
      padding: 1.25rem;
      border: 1px solid var(--console-hairline, var(--sl-color-neutral-200));
      border-radius: var(--sl-border-radius-large);
      background: var(--console-surface, transparent);
    }
    h2 {
      margin: 0 0 0.25rem;
      font-size: var(--console-text-card-title, 15px);
      font-weight: 600;
    }
    h3 {
      margin: 0 0 0.5rem;
      font-size: var(--console-text-body, 14px);
      font-weight: 600;
    }
    .group {
      padding: 0.75rem 0;
      border-top: 1px solid var(--console-hairline, var(--sl-color-neutral-200));
    }
    .fields {
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-small);
      align-items: flex-end;
    }
    .fields sl-input,
    .fields sl-select {
      width: 12rem;
      max-width: 100%;
    }
    .fields sl-select[data-field='recipients'] {
      width: 18rem;
    }
    p,
    .meta {
      margin: 0.25rem 0 0.75rem;
      font-size: var(--console-text-meta, 13px);
      color: var(--console-meta-color, var(--sl-color-neutral-600));
      line-height: 1.5;
    }
    .actions {
      display: flex;
      gap: var(--sl-spacing-small);
      padding-top: 0.75rem;
    }
    .roster {
      overflow-x: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: var(--console-text-body, 14px);
    }
    th,
    td {
      padding: 0.65rem 0.75rem 0.65rem 0;
      text-align: left;
      border-bottom: 1px solid
        var(--console-hairline, var(--sl-color-neutral-200));
    }
    th {
      font-size: var(--console-text-meta, 13px);
      font-weight: 600;
    }
    td sl-select {
      min-width: 10rem;
    }
    .identity {
      display: block;
    }
    .error {
      color: var(--sl-color-neutral-700);
      font-size: var(--console-text-meta, 13px);
    }
  `;

  disconnectedCallback(): void {
    ++this.generation;
    this.stopInvalidation?.();
    super.disconnectedCallback();
  }

  connectedCallback(): void {
    super.connectedCallback();
    this.stopInvalidation = onApiCachesInvalidated(() => {
      ++this.generation;
      this.layer = null;
      this.draft = null;
      this.recipients = null;
      this.recipientDraft = null;
      this.subscription = null;
      this.roleChanges = {};
      this.saving = false;
    });
    // A card moved in the DOM reads its layer again; the first read comes
    // from updated(), once project-id is known.
    if (this.hasUpdated) void this.load();
  }

  protected updated(changed: Map<PropertyKey, unknown>): void {
    if (changed.has('projectId')) void this.load();
  }

  private get isProject(): boolean {
    return !!this.projectId;
  }

  private async load(): Promise<void> {
    const generation = ++this.generation;
    this.error = '';
    this.recipients = null;
    this.recipientDraft = null;
    this.roleChanges = {};
    this.recipientsVisible = false;
    this.recipientError = '';
    this.subscription = null;
    this.saving = false;
    try {
      const [profile, layer, features] = await Promise.all([
        getUserProfile(),
        getDeliveryReportPolicy(this.projectId || undefined),
        getFeatures().catch(() => null),
      ]);
      if (generation !== this.generation) return;
      this.canManage =
        !!profile &&
        hasPermission(
          profile?.permissions,
          this.isProject ? 'edit_projects' : 'manage_budgets'
        );
      this.memberUpdatesAvailable =
        features?.features.delivery_report_member_updates === true &&
        !this.isProject;
      this.recipientsVisible =
        !this.isProject &&
        !!profile &&
        features?.features.delivery_report_recipients === true &&
        hasPermission(profile?.permissions, 'view_policies');
      this.canManageRecipients =
        !!profile && hasPermission(profile?.permissions, 'manage_policies');
      this.adopt(layer);
      if (this.recipientsVisible) void this.loadRecipients(generation);
      if (!this.isProject) {
        // Each person's own copy; any member may change it.
        const subscription = await getDeliveryReportSubscription().catch(
          () => null
        );
        if (generation === this.generation) this.subscription = subscription;
      }
    } catch {
      // A server without the report policy, or a member who cannot read
      // flows, gets no card.
      if (generation === this.generation) this.layer = null;
    }
  }

  private adopt(layer: DeliveryReportPolicyLayer): void {
    this.layer = layer;
    this.draft = structuredClone(layer.effective);
  }

  private set<K extends keyof DeliveryReportPolicy>(
    key: K,
    value: DeliveryReportPolicy[K]
  ): void {
    if (!this.draft) return;
    this.draft = { ...this.draft, [key]: value };
  }

  private setWrite(
    key: 'create_follow_up' | 'reopen',
    value: 'allow' | 'approval' | 'deny'
  ): void {
    if (!this.draft) return;
    this.draft = {
      ...this.draft,
      tracker_writes: { ...this.draft.tracker_writes, [key]: value },
    };
  }

  /** The overrides this layer stores: what differs from the layer below. */
  overrides(): PolicyOverrides {
    const layer = this.layer;
    const draft = this.draft;
    if (!layer || !draft) return {};
    const out: Record<string, unknown> = {};
    if (this.isProject) {
      for (const key of layer.project_override_fields) {
        const k = key as keyof DeliveryReportPolicy;
        if (key in layer.overrides || !same(draft[k], layer.effective[k])) {
          out[key] = draft[k];
        }
      }
      return out as PolicyOverrides;
    }
    const defaults = layer.defaults;
    for (const key of Object.keys(draft) as (keyof DeliveryReportPolicy)[]) {
      // Opt-outs belong to each person (the email link, the switch below).
      if (
        key === 'tracker_writes' ||
        key === 'opted_out_user_ids' ||
        RECIPIENT_POLICY_FIELDS.includes(
          key as (typeof RECIPIENT_POLICY_FIELDS)[number]
        )
      )
        continue;
      if (!same(draft[key], defaults[key])) out[key] = draft[key];
    }
    // The EE recipients API owns these controls. Preserve stored values,
    // including hidden feature settings, rather than reconstructing defaults.
    for (const key of RECIPIENT_POLICY_FIELDS) {
      if (key in layer.overrides) out[key] = layer.overrides[key];
    }
    const writes: Record<string, unknown> = {};
    for (const key of ['create_follow_up', 'reopen'] as const) {
      if (draft.tracker_writes[key] !== defaults.tracker_writes[key]) {
        writes[key] = draft.tracker_writes[key];
      }
    }
    if (Object.keys(writes).length) out.tracker_writes = writes;
    if (draft.report_mode === 'assessed') {
      // Recurring assessment is accepted only with both budgets stated.
      out.per_run_usd = draft.per_run_usd;
      out.monthly_usd = draft.monthly_usd;
    }
    return out as PolicyOverrides;
  }

  private async save(): Promise<void> {
    if (this.saving || !this.canManage) return;
    const generation = this.generation;
    this.saving = true;
    this.error = '';
    try {
      const overrides = this.overrides();
      if (!this.isProject) {
        // Recipient saves use a separate permission and endpoint. A later
        // budget save must retain the current recipient policy, not its
        // initial snapshot from when this card opened.
        const current = await getDeliveryReportPolicy();
        if (generation !== this.generation) return;
        for (const key of RECIPIENT_POLICY_FIELDS) {
          delete overrides[key];
          if (key in current.overrides)
            Object.assign(overrides, { [key]: current.overrides[key] });
        }
      }
      const layer = await saveDeliveryReportPolicy(
        overrides,
        this.projectId || undefined
      );
      if (generation !== this.generation) return;
      this.adopt(layer);
      showToast('Report policy saved.', 'success');
    } catch (error) {
      if (generation !== this.generation) return;
      this.error =
        error instanceof Error ? error.message : 'Could not save the policy.';
    } finally {
      if (generation === this.generation) this.saving = false;
    }
  }

  private async setSubscribed(subscribed: boolean): Promise<void> {
    this.error = '';
    const generation = this.generation;
    try {
      const subscription = await saveDeliveryReportSubscription(!subscribed);
      if (generation !== this.generation) return;
      this.subscription = subscription;
      showToast(
        subscribed
          ? 'You will receive the report email.'
          : 'You will no longer receive the report email.',
        'success'
      );
    } catch (error) {
      if (generation !== this.generation) return;
      this.error =
        error instanceof Error ? error.message : 'Could not save the setting.';
    }
  }

  private async reset(): Promise<void> {
    if (this.saving || !this.projectId || !this.canManage) return;
    const generation = this.generation;
    this.saving = true;
    this.error = '';
    try {
      const layer = await clearDeliveryReportPolicy(this.projectId);
      if (generation !== this.generation) return;
      this.adopt(layer);
      showToast('This repository follows the account policy again.', 'success');
    } catch (error) {
      if (generation !== this.generation) return;
      this.error =
        error instanceof Error ? error.message : 'Could not reset the policy.';
    } finally {
      if (generation === this.generation) this.saving = false;
    }
  }

  private numberInput(
    label: string,
    key:
      | 'pr_ready_days'
      | 'issue_inactive_days'
      | 'audit_ci_max_wait_minutes'
      | 'per_run_usd'
      | 'monthly_usd',
    min: number,
    step = 1
  ) {
    const draft = this.draft!;
    return html`<sl-input
      type="number"
      label=${label}
      data-field=${key}
      min=${min}
      step=${step}
      .value=${String(draft[key])}
      ?disabled=${!this.canManage}
      @sl-input=${(event: Event) => {
        const value = Number((event.target as HTMLInputElement).value);
        if (Number.isFinite(value)) this.set(key, value);
      }}
    ></sl-input>`;
  }

  private renderWindows() {
    const draft = this.draft!;
    return html`<div class="group">
      <h3>Open items and criteria</h3>
      <div class="fields">
        ${this.numberInput('Open PR after (days)', 'pr_ready_days', 1)}
        ${this.numberInput('Inactive issue after (days)', 'issue_inactive_days', 1)}
        ${this.numberInput('Merge audit CI wait (minutes)', 'audit_ci_max_wait_minutes', 1)}
        <sl-select
          label="Acceptance criteria from"
          data-field="criteria_source"
          .value=${draft.criteria_source}
          ?disabled=${!this.canManage}
          @sl-change=${(event: Event) =>
            this.set(
              'criteria_source',
              (event.target as HTMLSelectElement)
                .value as DeliveryReportPolicy['criteria_source']
            )}
        >
          <sl-option value="triage_assessment"
            >Triage assessment section</sl-option
          >
          <sl-option value="description">Issue description</sl-option>
        </sl-select>
      </div>
    </div>`;
  }

  private renderAccount() {
    const draft = this.draft!;
    const defaults = this.layer!.defaults;
    const assessed = draft.report_mode === 'assessed';
    return html`
      <div class="group">
        <h3>Schedule</h3>
        <div class="fields">
          <sl-select
            label="Cadence"
            data-field="cadence"
            .value=${draft.cadence}
            ?disabled=${!this.canManage}
            @sl-change=${(event: Event) =>
              this.set(
                'cadence',
                (event.target as HTMLSelectElement)
                  .value as DeliveryReportPolicy['cadence']
              )}
          >
            <sl-option value="weekly">Weekly</sl-option>
            <sl-option value="daily">Daily</sl-option>
            <sl-option value="manual">Manual only</sl-option>
          </sl-select>
          ${
            draft.cadence === 'weekly'
              ? html`<sl-select
                  label="Day"
                  data-field="weekday"
                  .value=${draft.weekday}
                  ?disabled=${!this.canManage}
                  @sl-change=${(event: Event) =>
                    this.set(
                      'weekday',
                      (event.target as HTMLSelectElement).value
                    )}
                >
                  ${WEEKDAYS.map(
                    (day) =>
                      html`<sl-option value=${day}
                        >${WEEKDAY_LABELS[day]}</sl-option
                      >`
                  )}
                </sl-select>`
              : nothing
          }
          ${
            draft.cadence !== 'manual'
              ? html`<sl-input
                  label="At (${draft.timezone})"
                  data-field="at"
                  .value=${draft.at}
                  pattern="([01][0-9]|2[0-3]):[0-5][0-9]"
                  ?disabled=${!this.canManage}
                  @sl-input=${(event: Event) =>
                    this.set('at', (event.target as HTMLInputElement).value)}
                ></sl-input>`
              : nothing
          }
        </div>
        <p>Each report covers the period since the previous one.</p>
      </div>
      <div class="group">
        <h3>Assessment</h3>
        <sl-switch
          data-field="onboarding_assessed_run"
          ?checked=${draft.onboarding_assessed_run}
          ?disabled=${!this.canManage}
          @sl-change=${(event: Event) =>
            this.set(
              'onboarding_assessed_run',
              (event.target as HTMLInputElement).checked
            )}
          >Assess the first report once</sl-switch
        >
        <p class="meta">
          One model pass on the first report, capped at the per-run budget
          (${usd(draft.per_run_usd)}), so you can see what an assessment adds.
        </p>
        <sl-switch
          data-field="report_mode"
          ?checked=${assessed}
          ?disabled=${!this.canManage}
          @sl-change=${(event: Event) =>
            this.set(
              'report_mode',
              (event.target as HTMLInputElement).checked
                ? 'assessed'
                : 'deterministic'
            )}
          >Assess every report</sl-switch
        >
        <p class="meta">
          ${
            assessed
              ? html`A model checks changes that have neither a merge audit nor
                a change brief, within these budgets. When the month's budget or
                the account's credit runs out, the report still goes out without
                assessments and says so.`
              : html`Off: reports use tracker and forge data only and make no
                model call. Turning this on asks for a budget; the defaults are
                ${usd(defaults.per_run_usd)} per run and
                ${usd(defaults.monthly_usd)} per month.`
          }
        </p>
        ${
          assessed
            ? html`<div class="fields">
                ${this.numberInput('Per run (USD)', 'per_run_usd', 0.01, 0.01)}
                ${this.numberInput('Per month (USD)', 'monthly_usd', 0.01, 0.01)}
              </div>`
            : nothing
        }
      </div>
      ${this.renderWindows()}
      <div class="group">
        <h3>Tracker writes</h3>
        <sl-switch
          data-field="create_follow_up"
          ?checked=${draft.tracker_writes.create_follow_up === 'allow'}
          ?disabled=${!this.canManage}
          @sl-change=${(event: Event) =>
            this.setWrite(
              'create_follow_up',
              (event.target as HTMLInputElement).checked ? 'allow' : 'deny'
            )}
          >Create follow-up issues automatically</sl-switch
        >
        <sl-switch
          data-field="reopen"
          ?checked=${draft.tracker_writes.reopen === 'allow'}
          ?disabled=${!this.canManage}
          @sl-change=${(event: Event) =>
            this.setWrite(
              'reopen',
              (event.target as HTMLInputElement).checked ? 'allow' : 'approval'
            )}
          >Reopen issues automatically</sl-switch
        >
        <p class="meta">
          Off by default: follow-ups and reopen requests are proposals in the
          report, and opening one is the approval. Turn these on once the
          proposals have proven precise on this account. The report never closes
          an issue.
        </p>
      </div>
      ${this.renderRecipients()}
      ${
        this.subscription?.is_recipient
          ? html`<div class="group">
              <h3>Your email</h3>
              <sl-switch
                data-field="subscribed"
                .checked=${live(!this.subscription.opted_out)}
                @sl-change=${(event: Event) => void this.setSubscribed((event.target as HTMLInputElement).checked)}
                >Email me this report</sl-switch
              >
              <p class="meta">
                You can also stop your copy from the link in the email.
              </p>
            </div>`
          : nothing
      }
    `;
  }

  private async loadRecipients(generation = this.generation): Promise<void> {
    this.recipientError = '';
    try {
      const data = await getReportRecipients();
      if (generation !== this.generation) return;
      this.recipients = data;
      this.recipientDraft = structuredClone(data);
      this.roleChanges = {};
    } catch (error) {
      if (generation !== this.generation) return;
      this.recipientError =
        error instanceof Error ? error.message : 'Could not load recipients.';
    }
  }

  private recipientChanges(): ReportRecipientsChanges {
    if (!this.recipients || !this.recipientDraft) return {};
    const changes: ReportRecipientsChanges = {};
    const draft = this.recipientDraft;
    for (const key of [
      'recipients',
      'sponsoring_lead_user_id',
      'opted_out_user_ids',
    ] as const) {
      if (!same(draft[key], this.recipients[key]))
        Object.assign(changes, { [key]: draft[key] });
    }
    if (
      this.memberUpdatesAvailable &&
      draft.per_member !== this.recipients.per_member
    )
      changes.per_member = draft.per_member;
    if (Object.keys(this.roleChanges).length) changes.roles = this.roleChanges;
    return changes;
  }

  private async saveRecipients(): Promise<void> {
    if (!this.canManageRecipients || this.saving || !this.recipientDraft)
      return;
    if (
      this.recipientDraft.recipients === 'sponsoring_lead' &&
      !this.recipientDraft.sponsoring_lead_user_id
    ) {
      this.recipientError =
        'Choose the sponsoring lead who receives the report.';
      return;
    }
    const changes = this.recipientChanges();
    if (!Object.keys(changes).length) return;
    const generation = this.generation;
    this.saving = true;
    this.recipientError = '';
    try {
      const data = await saveReportRecipients(changes);
      if (generation !== this.generation) return;
      this.recipients = data;
      this.recipientDraft = structuredClone(data);
      this.roleChanges = {};
      // A recipient may have opted themselves out in this save.
      const subscription = await getDeliveryReportSubscription().catch(
        () => null
      );
      if (generation !== this.generation) return;
      this.subscription = subscription;
      showToast('Report recipients saved.', 'success');
    } catch (error) {
      if (generation !== this.generation) return;
      this.recipientError =
        error instanceof Error ? error.message : 'Could not save recipients.';
    } finally {
      if (generation === this.generation) this.saving = false;
    }
  }

  private renderRecipients() {
    if (!this.recipientsVisible || this.isProject) return nothing;
    const draft = this.recipientDraft;
    const disabled = !this.canManageRecipients || this.saving;
    return html`<div class="group" data-section="recipients">
      <h3>Recipients</h3>
      <p class="meta">
        The account owner receives the full report. You can also send it to a
        sponsoring lead. When member updates are enabled, other owners and leads
        receive the full report, members receive a short update, and viewers
        receive no email. Report roles follow account roles unless overridden.
      </p>
      ${this.canManageRecipients ? nothing : html`<p class="meta">People who can manage policies can change recipients.</p>`}
      ${this.recipientError ? html`<p class="error" role="alert">${this.recipientError}</p>` : nothing}
      ${
        !draft
          ? html`<sl-button
              size="small"
              data-action="retry-recipients"
              @click=${() => void this.loadRecipients()}
              >Reload recipients</sl-button
            >`
          : html`
              ${
                this.memberUpdatesAvailable
                  ? html`<sl-switch
                        data-field="per_member_updates"
                        .checked=${live(draft.per_member)}
                        ?disabled=${disabled}
                        @sl-change=${(event: Event) => {
                          this.recipientDraft = {
                            ...draft,
                            per_member: (event.target as HTMLInputElement)
                              .checked,
                          };
                        }}
                        >Send short updates to account members</sl-switch
                      >
                      <p class="meta">
                        Off by default. Each person receives one update per
                        report period, based on the work they reviewed or
                        discussed.
                      </p>`
                  : nothing
              }
              <div class="fields">
                <sl-select
                  label="Send full report to"
                  data-field="recipients"
                  .value=${draft.recipients}
                  ?disabled=${disabled}
                  @sl-change=${(event: Event) => {
                    this.recipientDraft = {
                      ...this.recipientDraft!,
                      recipients: (event.target as HTMLSelectElement)
                        .value as ReportRecipients['recipients'],
                    };
                  }}
                  ><sl-option value="account_owner">Account owner</sl-option
                  ><sl-option value="sponsoring_lead"
                    >Owner and sponsoring lead</sl-option
                  ></sl-select
                >
                ${
                  draft.recipients === 'sponsoring_lead'
                    ? html`<sl-select
                        label="Sponsoring lead"
                        data-field="sponsoring_lead_user_id"
                        .value=${draft.sponsoring_lead_user_id ?? ''}
                        ?disabled=${disabled}
                        @sl-change=${(event: Event) => {
                          this.recipientDraft = {
                            ...this.recipientDraft!,
                            sponsoring_lead_user_id:
                              (event.target as HTMLSelectElement).value || null,
                          };
                        }}
                        >${draft.members.map((member) => html`<sl-option value=${member.user_id}>${member.name || member.email || member.user_id}</sl-option>`)}</sl-select
                      >`
                    : nothing
                }
              </div>
              <p class="meta">
                The account owner always keeps the owner report role. A saved
                opt-out stops delivery for every role, including the owner.
              </p>
              <div class="roster">
                <table aria-label="Report recipients">
                  <thead>
                    <tr>
                      <th scope="col">Person</th>
                      <th scope="col">Report role</th>
                      <th scope="col">Current delivery</th>
                      <th scope="col">Opt out</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${draft.members.map((member) => {
                      const role =
                        member.user_id in this.roleChanges
                          ? (this.roleChanges[member.user_id] ?? 'default')
                          : member.role_source === 'override'
                            ? member.report_role
                            : 'default';
                      const source =
                        member.role_source === 'rbac'
                          ? 'RBAC'
                          : member.role_source === 'owner'
                            ? 'Account owner'
                            : member.role_source === 'sponsoring_lead'
                              ? 'Sponsoring lead'
                              : 'Override';
                      return html`<tr data-recipient=${member.user_id}>
                        <td>
                          <span class="identity"
                            >${member.name || member.email}</span
                          ><span class="meta identity">${member.email}</span
                          ><span class="meta identity"
                            >${member.rbac_roles.join(', ') || 'No account role'}</span
                          >
                        </td>
                        <td>
                          <sl-select
                            aria-label=${`Report role for ${member.name || member.email}`}
                            data-role-user=${member.user_id}
                            .value=${role}
                            ?disabled=${disabled || member.role_source === 'owner'}
                            @sl-change=${(event: Event) => {
                              const value = (event.target as HTMLSelectElement)
                                .value;
                              this.roleChanges = {
                                ...this.roleChanges,
                                [member.user_id]:
                                  value === 'default'
                                    ? null
                                    : (value as ReportRole),
                              };
                            }}
                            ><sl-option value="default"
                              >Follow account role</sl-option
                            >${Object.entries(ROLE_LABELS).map(([value, label]) => html`<sl-option value=${value}>${label}</sl-option>`)}</sl-select
                          >
                          <span class="meta identity"
                            >${ROLE_LABELS[member.report_role]} ·
                            ${source}</span
                          >
                        </td>
                        <td>
                          ${DELIVERY_LABELS[member.delivery]}${member.opted_out ? html`<span class="meta identity">Opted out</span>` : nothing}
                        </td>
                        <td>
                          <sl-switch
                            aria-label=${`Opt out ${member.name || member.email}`}
                            data-opt-out-user=${member.user_id}
                            .checked=${live(draft.opted_out_user_ids.includes(member.user_id))}
                            ?disabled=${disabled}
                            @sl-change=${(event: Event) => {
                              const ids =
                                this.recipientDraft!.opted_out_user_ids.filter(
                                  (id) => id !== member.user_id
                                );
                              if ((event.target as HTMLInputElement).checked)
                                ids.push(member.user_id);
                              this.recipientDraft = {
                                ...this.recipientDraft!,
                                opted_out_user_ids: ids,
                              };
                            }}
                          ></sl-switch>
                        </td>
                      </tr>`;
                    })}
                  </tbody>
                </table>
              </div>
              <p class="meta">
                Current delivery shows the saved policy. Save recipient changes
                to update it.
              </p>
              ${this.canManageRecipients ? html`<div class="actions"><sl-button size="small" data-action="save-recipients" ?loading=${this.saving} @click=${() => void this.saveRecipients()}>Save recipients</sl-button></div>` : nothing}
            `
      }
    </div>`;
  }

  render() {
    const layer = this.layer;
    const draft = this.draft;
    if (!layer || !draft) return nothing;
    const title = this.isProject
      ? 'Delivery Report for this repository'
      : 'Delivery Report';
    const overridden = Object.keys(layer.overrides).length > 0;
    return html`<section aria-labelledby="delivery-report-policy-title">
      <h2 id="delivery-report-policy-title">${title}</h2>
      <p>
        ${
          this.isProject
            ? overridden
              ? 'This repository has its own settings. Budgets, cadence and tracker writes stay on the account.'
              : 'This repository follows the account policy.'
            : 'What landed, what the evidence supports against stated criteria, and which gaps deserve a look.'
        }
        ${
          this.canManage
            ? ''
            : this.isProject
              ? 'People who can edit projects can change these.'
              : 'People who can manage budgets can change these.'
        }
      </p>
      <div class="group">
        <sl-switch
          data-field="enabled"
          ?checked=${draft.enabled}
          ?disabled=${!this.canManage}
          @sl-change=${(event: Event) =>
            this.set('enabled', (event.target as HTMLInputElement).checked)}
          >${
            this.isProject
              ? 'Include this repository in the report'
              : 'Send the Delivery Report'
          }</sl-switch
        >
      </div>
      ${this.isProject ? this.renderWindows() : this.renderAccount()}
      ${
        this.error
          ? html`<p class="error" role="alert">${this.error}</p>`
          : nothing
      }
      ${
        this.canManage
          ? html`<div class="actions">
              <sl-button
                variant="primary"
                size="small"
                data-action="save"
                ?loading=${this.saving}
                @click=${() => void this.save()}
                >Save</sl-button
              >
              ${
                this.isProject && overridden
                  ? html`<sl-button
                      size="small"
                      data-action="reset"
                      ?disabled=${this.saving}
                      @click=${() => void this.reset()}
                      >Use the account policy</sl-button
                    >`
                  : nothing
              }
            </div>`
          : nothing
      }
    </section>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'delivery-report-policy-settings': DeliveryReportPolicySettings;
  }
}
