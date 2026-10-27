import { LitElement, html, css, nothing } from 'lit';
import { live } from 'lit/directives/live.js';
import { customElement, property, state } from 'lit/decorators.js';
import { getFeatures, getUserProfile, hasPermission } from '../api';
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
    .error {
      color: var(--sl-color-neutral-700);
      font-size: var(--console-text-meta, 13px);
    }
  `;

  disconnectedCallback(): void {
    ++this.generation;
    super.disconnectedCallback();
  }

  connectedCallback(): void {
    super.connectedCallback();
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
    try {
      const [profile, layer, features] = await Promise.all([
        getUserProfile(),
        getDeliveryReportPolicy(this.projectId || undefined),
        getFeatures().catch(() => null),
      ]);
      if (generation !== this.generation) return;
      this.canManage = hasPermission(
        profile?.permissions,
        this.isProject ? 'edit_projects' : 'manage_budgets'
      );
      this.memberUpdatesAvailable =
        features?.features.delivery_report_member_updates === true &&
        !this.isProject;
      this.adopt(layer);
      if (!this.isProject) {
        // Each person's own copy; any member may change it.
        this.subscription = await getDeliveryReportSubscription().catch(
          () => null
        );
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
      if (key === 'tracker_writes' || key === 'opted_out_user_ids') continue;
      if (!same(draft[key], defaults[key])) out[key] = draft[key];
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
    if (this.saving) return;
    this.saving = true;
    this.error = '';
    try {
      const layer = await saveDeliveryReportPolicy(
        this.overrides(),
        this.projectId || undefined
      );
      this.adopt(layer);
      showToast('Report policy saved.', 'success');
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not save the policy.';
    } finally {
      this.saving = false;
    }
  }

  private async setSubscribed(subscribed: boolean): Promise<void> {
    this.error = '';
    try {
      this.subscription = await saveDeliveryReportSubscription(!subscribed);
      showToast(
        subscribed
          ? 'You will receive the report email.'
          : 'You will no longer receive the report email.',
        'success'
      );
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not save the setting.';
    }
  }

  private async reset(): Promise<void> {
    if (this.saving || !this.projectId) return;
    this.saving = true;
    this.error = '';
    try {
      this.adopt(await clearDeliveryReportPolicy(this.projectId));
      showToast('This repository follows the account policy again.', 'success');
    } catch (error) {
      this.error =
        error instanceof Error ? error.message : 'Could not reset the policy.';
    } finally {
      this.saving = false;
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
      <div class="group">
        <h3>Recipients</h3>
        ${
          this.memberUpdatesAvailable
            ? html`<sl-switch
                  data-field="per_member_updates"
                  .checked=${live(draft.per_member_updates)}
                  ?disabled=${!this.canManage}
                  @sl-change=${(event: Event) =>
                    this.set(
                      'per_member_updates',
                      (event.target as HTMLInputElement).checked
                    )}
                  >Send short updates to account members</sl-switch
                >
                <p class="meta">
                  Off by default. Each person receives one update per report
                  period, based on the work they reviewed or discussed.
                </p>`
            : nothing
        }
        <div class="fields">
          <sl-select
            label="Send to"
            data-field="recipients"
            .value=${draft.recipients}
            ?disabled=${!this.canManage}
            @sl-change=${(event: Event) =>
              this.set(
                'recipients',
                (event.target as HTMLSelectElement)
                  .value as DeliveryReportPolicy['recipients']
              )}
          >
            <sl-option value="account_owner">Account owner</sl-option>
            <sl-option value="sponsoring_lead">Sponsoring lead</sl-option>
          </sl-select>
          ${
            draft.recipients === 'sponsoring_lead'
              ? html`<sl-input
                  label="Lead user id"
                  data-field="sponsoring_lead_user_id"
                  .value=${draft.sponsoring_lead_user_id ?? ''}
                  ?disabled=${!this.canManage}
                  @sl-input=${(event: Event) =>
                    this.set(
                      'sponsoring_lead_user_id',
                      (event.target as HTMLInputElement).value || null
                    )}
                ></sl-input>`
              : nothing
          }
        </div>
        ${
          this.subscription?.is_recipient
            ? html`<sl-switch
                data-field="subscribed"
                .checked=${live(!this.subscription.opted_out)}
                @sl-change=${(event: Event) =>
                  void this.setSubscribed(
                    (event.target as HTMLInputElement).checked
                  )}
                >Email me this report</sl-switch
              >`
            : nothing
        }
        <p class="meta">
          Anyone can stop their own copy from the link in the email.
        </p>
      </div>
    `;
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
