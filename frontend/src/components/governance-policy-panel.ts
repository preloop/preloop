import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import {
  convertGovernanceLegacyRules,
  getAIModels,
  getGovernancePolicy,
  getGovernanceReviewSettings,
  getGovernanceThresholds,
  getUsers,
  keepGovernanceReviewOn,
  listGovernancePolicyVersions,
  restoreGovernancePolicyVersion,
  saveGovernancePolicy,
  saveGovernanceThresholds,
  updateGovernanceReviewSettings,
  type GovernancePolicyDocument,
  type GovernancePolicyVersion,
  type GovernanceReviewMode,
  type GovernanceReviewSettings,
  type GovernanceRuleClass,
  type GovernanceThresholdField,
  type GovernanceThresholds,
} from '../api';
import type { AIModel } from '../types';
import {
  getWeeklyReportPreferences,
  updateWeeklyReportPreferences,
  type WeeklyReportPreferences,
} from '../governance-api';
import { confirmDialog, showToast } from './confirm-dialog';
import { formatLocalDateTime, formatRelativeTime } from '../utils/date';
import { renderMarkdown, markdownBodyCss } from '../utils/markdown';
import consoleStyles from '../styles/console-styles.css?inline';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio-button/radio-button.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';

/** The last "on" mode chosen here, so turning the review back on restores it. */
export const LAST_ON_MODE_KEY = 'preloop.governance.last_on_mode';

/** The three choices an operator makes. `off_over_budget` is the server's. */
type ChosenMode = Exclude<GovernanceReviewMode, 'off_over_budget'>;

const MODE_LABELS: Record<ChosenMode, string> = {
  on_platform: 'On with Preloop models',
  on_own_model: 'On with my model',
  off: 'Off',
};

export const PRIVACY_SENTENCE =
  "With Preloop models, scrubbed session excerpts and summaries are sent to Preloop's model provider. Choose your own model to keep them on your provider.";

const CLASS_LABELS: Record<GovernanceRuleClass, { label: string }> = {
  must: { label: 'Must' },
  prefer: { label: 'Prefer' },
};

const SOURCE_NOTES: Record<GovernanceThresholdField['source'], string> = {
  default: '',
  settings: '',
  policy_document: 'From a hidden rule block in the policy',
};

/** `kind.key` names one measured-check input. */
type ThresholdDraft = Record<string, number | string>;

/**
 * Marks `### must.name` and `### prefer.name` headings, which policies in the
 * old format still use, with their class in the rendered preview. A plain
 * language policy has none and renders unchanged.
 */
export function markRuleHeadings(renderedHtml: string): string {
  return renderedHtml.replace(
    /<h([2-4])([^>]*)>\s*(must|prefer)\./g,
    (_match, level: string, attrs: string, cls: GovernanceRuleClass) =>
      `<h${level}${attrs}><span class="rule-class rule-class-${cls}">${CLASS_LABELS[cls].label}</span> ${cls}.`
  );
}

/** A model the account owns and the gateway can call server side. */
function isOwnGatewayModel(model: AIModel): boolean {
  return (
    !model.is_shared &&
    !model.is_built_in &&
    (model.model_kind ?? 'llm') === 'llm' &&
    model.supports_server_side_generation !== false
  );
}

/**
 * The Governance policy tab: the plain-language policy the weekly review
 * reads, the numbers its measured checks use, the analysis model, and the
 * version history.
 *
 * Rendered only where `/features` reports `governance_review`; the parent
 * view owns that gate. `canManage` false (a `view_policies` member) gives a
 * read-only editor and no write controls.
 */
@customElement('governance-policy-panel')
export class GovernancePolicyPanel extends LitElement {
  @property({ type: Boolean }) canManage = false;
  /** The account-wide email switch needs `manage_account`, not policies. */
  @property({ type: Boolean }) canManageAccount = false;

  /** Seam for tests; the console's confirm dialog by default. */
  confirm: typeof confirmDialog = confirmDialog;

  @state() private _loading = true;
  @state() private _error = '';
  @state() private _policy: GovernancePolicyDocument | null = null;
  @state() private _draft = '';
  @state() private _view: 'edit' | 'preview' = 'edit';
  @state() private _saving = false;
  @state() private _versions: GovernancePolicyVersion[] = [];
  @state() private _restoring: number | null = null;
  @state() private _settings: GovernanceReviewSettings | null = null;
  @state() private _models: AIModel[] = [];
  @state() private _modeDraft: ChosenMode = 'on_platform';
  @state() private _modelDraft = '';
  @state() private _savingSettings = false;
  @state() private _keepingOn = false;
  @state() private _userNames: Map<string, string> = new Map();
  /** Null on a server that predates the measured-check settings. */
  @state() private _thresholds: GovernanceThresholds | null = null;
  @state() private _thresholdDraft: ThresholdDraft = {};
  @state() private _savingThresholds = false;
  @state() private _converting = false;
  /** Null when the server predates weekly report preferences. */
  @state() private _email: WeeklyReportPreferences | null = null;
  @state() private _switching = '';

  static styles = [
    unsafeCSS(consoleStyles),
    unsafeCSS(markdownBodyCss),
    css`
      :host {
        display: block;
      }
      section {
        padding: var(--sl-spacing-large) 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      section:last-child {
        border-bottom: none;
      }
      h3 {
        margin: 0 0 var(--sl-spacing-x-small);
        font-size: var(--console-text-card-title);
        font-weight: 600;
      }
      .meta {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .toolbar {
        display: flex;
        align-items: center;
        gap: var(--sl-spacing-small);
        margin: var(--sl-spacing-small) 0;
      }
      .toolbar .spacer {
        flex: 1;
      }
      .policy-intro {
        max-width: 72ch;
      }
      textarea {
        box-sizing: border-box;
        width: 100%;
        min-height: 420px;
        padding: var(--sl-spacing-small);
        font-family: var(--sl-font-sans);
        font-size: var(--console-text-body);
        line-height: 1.6;
        color: inherit;
        background: var(--console-surface);
        border: 1px solid var(--console-hairline);
        border-radius: var(--sl-border-radius-medium);
        resize: vertical;
      }
      textarea[readonly] {
        color: var(--console-meta-color);
      }
      .threshold-group {
        display: grid;
        grid-template-columns: minmax(180px, 1fr) 2fr;
        gap: var(--sl-spacing-medium);
        padding: var(--sl-spacing-small) 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      .threshold-group:last-of-type {
        border-bottom: none;
      }
      .threshold-group h4 {
        margin: 0;
        font-size: var(--console-text-body);
        font-weight: 600;
      }
      .threshold-fields {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-small) var(--sl-spacing-medium);
      }
      .threshold-fields sl-input,
      .threshold-fields sl-select {
        width: 200px;
      }
      @media (max-width: 640px) {
        .threshold-group {
          grid-template-columns: 1fr;
        }
      }
      .rule-class {
        display: inline-block;
        min-width: 3.5em;
        padding: 2px 8px;
        border-radius: var(--sl-border-radius-pill);
        font-size: 12px;
        font-weight: 500;
        text-align: center;
      }
      .rule-class-must {
        color: var(--sl-color-warning-700);
        background: color-mix(
          in srgb,
          var(--sl-color-warning-600) 14%,
          transparent
        );
      }
      .rule-class-prefer {
        color: var(--sl-color-neutral-700);
        background: color-mix(
          in srgb,
          var(--sl-color-neutral-600) 12%,
          transparent
        );
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: var(--console-text-body);
      }
      th {
        text-align: left;
        font-weight: 500;
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      th,
      td {
        padding: var(--sl-spacing-x-small) var(--sl-spacing-small)
          var(--sl-spacing-x-small) 0;
        border-bottom: 1px solid var(--console-hairline);
      }
      td.actions {
        text-align: right;
      }
      .switches {
        display: grid;
        gap: var(--sl-spacing-medium);
      }
      .switch-row .meta {
        margin-top: 2px;
      }
      .settings-grid {
        display: grid;
        gap: var(--sl-spacing-medium);
        max-width: 560px;
      }
      sl-alert {
        margin-bottom: var(--sl-spacing-medium);
      }
    `,
  ];

  connectedCallback(): void {
    super.connectedCallback();
    void this._load();
  }

  private async _load(): Promise<void> {
    this._loading = true;
    this._error = '';
    try {
      const [policy, versions, settings] = await Promise.all([
        getGovernancePolicy(),
        listGovernancePolicyVersions(),
        getGovernanceReviewSettings(),
        this._loadThresholds(),
      ]);
      this._applyPolicy(policy);
      this._versions = versions;
      this._applySettings(settings);
    } catch (error) {
      this._error =
        error instanceof Error ? error.message : 'Failed to load the policy';
    } finally {
      this._loading = false;
    }
    // Names, models and email switches are conveniences: the tab works
    // without them, and an older server has no email preferences at all.
    getWeeklyReportPreferences()
      .then((prefs) => (this._email = prefs))
      .catch(() => (this._email = null));
    if (this.canManage) {
      getAIModels()
        .then((models) => (this._models = models.filter(isOwnGatewayModel)))
        .catch(() => undefined);
    }
    getUsers()
      .then((list) => {
        this._userNames = new Map(
          list.users.map((user) => [
            user.id,
            user.full_name || user.username || user.email || 'A team member',
          ])
        );
      })
      .catch(() => undefined);
  }

  /** An older server has no measured-check settings: hide the form. */
  private async _loadThresholds(): Promise<void> {
    try {
      this._applyThresholds(await getGovernanceThresholds());
    } catch {
      this._thresholds = null;
    }
  }

  private _applyThresholds(thresholds: GovernanceThresholds): void {
    this._thresholds = thresholds;
    const draft: ThresholdDraft = {};
    for (const group of thresholds.groups) {
      for (const field of group.fields) {
        draft[`${group.kind}.${field.key}`] = field.value;
      }
    }
    this._thresholdDraft = draft;
  }

  /** Preserve saved overrides and explicit edits, leaving legacy values in place. */
  private _thresholdOverrides(): Record<
    string,
    Record<string, number | string>
  > {
    const overrides: Record<string, Record<string, number | string>> = {};
    for (const group of this._thresholds?.groups ?? []) {
      for (const field of group.fields) {
        const value = this._thresholdDraft[`${group.kind}.${field.key}`];
        const keep =
          field.source === 'settings' || String(value) !== String(field.value);
        if (value === undefined || value === '' || !keep) continue;
        (overrides[group.kind] ??= {})[field.key] = field.choices.length
          ? String(value)
          : Number(value);
      }
    }
    return overrides;
  }

  private get _thresholdsDirty(): boolean {
    return (this._thresholds?.groups ?? []).some((group) =>
      group.fields.some(
        (field) =>
          String(this._thresholdDraft[`${group.kind}.${field.key}`]) !==
          String(field.value)
      )
    );
  }

  private _thresholdError(
    field: GovernanceThresholdField,
    value: number | string
  ): string {
    if (field.choices.length) {
      return field.choices.includes(String(value))
        ? ''
        : 'Choose a value from the list.';
    }
    if (String(value).trim() === '' || !Number.isFinite(Number(value))) {
      return 'Enter a number.';
    }
    const number = Number(value);
    if (field.integer && !Number.isInteger(number))
      return 'Enter a whole number.';
    if (field.minimum != null && number < field.minimum)
      return `Enter ${field.minimum} or more.`;
    if (field.maximum != null && number > field.maximum)
      return `Enter ${field.maximum} or less.`;
    return '';
  }

  private get _thresholdsValid(): boolean {
    return (this._thresholds?.groups ?? []).every((group) =>
      group.fields.every(
        (field) =>
          !this._thresholdError(
            field,
            this._thresholdDraft[`${group.kind}.${field.key}`] ?? field.value
          )
      )
    );
  }

  private async _resetThresholds(): Promise<void> {
    const confirmed = await this.confirm({
      title: 'Reset measured checks?',
      message: 'This removes all saved measured-check settings for your team.',
      detail:
        'Values still in legacy policy blocks apply first; otherwise the built-in defaults apply. This cannot be undone.',
      confirmLabel: 'Reset',
      variant: 'danger',
    });
    if (confirmed) await this._saveThresholds(true);
  }

  private async _saveThresholds(reset = false): Promise<void> {
    if (this._savingThresholds || (!reset && !this._thresholdsValid)) return;
    this._savingThresholds = true;
    try {
      this._applyThresholds(
        await saveGovernanceThresholds(
          reset ? null : this._thresholdOverrides()
        )
      );
      showToast(
        reset
          ? 'Measured checks reset to the defaults.'
          : 'Measured checks saved.',
        'success'
      );
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : 'Failed to save',
        'danger'
      );
    } finally {
      this._savingThresholds = false;
    }
  }

  private async _convertLegacy(): Promise<void> {
    this._converting = true;
    try {
      const converted = await convertGovernanceLegacyRules();
      this._applyPolicy(converted);
      await Promise.all([this._reloadVersions(), this._loadThresholds()]);
      showToast(
        `Rule blocks removed. Saved as version ${converted.version}.`,
        'success'
      );
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : 'Failed to convert',
        'danger'
      );
    } finally {
      this._converting = false;
    }
  }

  private _applyPolicy(policy: GovernancePolicyDocument): void {
    this._policy = policy;
    this._draft = policy.body_markdown;
  }

  private _applySettings(settings: GovernanceReviewSettings): void {
    this._settings = settings;
    this._modeDraft =
      settings.mode === 'off_over_budget' ? 'off' : settings.mode;
    this._modelDraft = settings.analysis_model_id ?? '';
    if (settings.mode === 'on_platform' || settings.mode === 'on_own_model')
      localStorage.setItem(LAST_ON_MODE_KEY, settings.mode);
  }

  /** On restores the last on mode; own model only when one is still set. */
  private async _toggleReview(on: boolean): Promise<void> {
    if (this._switching) return;
    this._switching = 'review';
    try {
      const last = localStorage.getItem(LAST_ON_MODE_KEY);
      const mode: ChosenMode = !on
        ? 'off'
        : last === 'on_own_model' && this._settings?.analysis_model_id
          ? 'on_own_model'
          : 'on_platform';
      const body: { mode: ChosenMode; analysis_model_id?: string | null } = {
        mode,
      };
      if (mode === 'on_own_model')
        body.analysis_model_id = this._settings?.analysis_model_id ?? null;
      this._applySettings(await updateGovernanceReviewSettings(body));
      showToast(
        on
          ? 'The AI usage policy review is on.'
          : 'The AI usage policy review is off. No sessions are read and nothing is spent.',
        'success'
      );
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : 'Could not change the review',
        'danger'
      );
    } finally {
      this._switching = '';
    }
  }

  private async _toggleEmail(
    field: keyof WeeklyReportPreferences,
    on: boolean
  ): Promise<void> {
    if (this._switching) return;
    this._switching = field;
    try {
      this._email = await updateWeeklyReportPreferences({ [field]: on });
      showToast(
        field === 'account_enabled'
          ? on
            ? 'Weekly reports for owners and admins are on.'
            : 'Weekly reports for owners and admins are off.'
          : on
            ? 'You will get the weekly report email.'
            : 'You will no longer get the weekly report email.',
        'success'
      );
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : 'Could not change the email',
        'danger'
      );
    } finally {
      this._switching = '';
    }
  }

  private _renderSwitches() {
    const settings = this._settings;
    if (!settings && !this._email) return nothing;
    const on = !!settings && !settings.mode.startsWith('off');
    return html`<section
      class="switches"
      aria-label="Review and email switches"
    >
      ${
        settings
          ? html`<div class="switch-row">
              <sl-switch
                class="review-switch"
                ?checked=${on}
                ?disabled=${!this.canManage || !!this._switching}
                @sl-change=${(event: Event) =>
                  void this._toggleReview(
                    (event.target as HTMLInputElement).checked
                  )}
                >AI usage policy review</sl-switch
              >
              <div class="meta">
                ${
                  on
                    ? 'On. Each hour new sessions are summarised and each Monday last week is checked against this policy.'
                    : settings.mode === 'off_over_budget'
                      ? 'Off: the last review used up its analysis allowance.'
                      : settings.effective_reason ===
                          'explicit_analysis_opt_in_required'
                        ? 'Off. Automatic analysis now requires an explicit opt-in. Turn this switch on to enable policy review; factual exception emails work without it.'
                        : 'Off. No sessions are read, no review runs and nothing is spent.'
                }
              </div>
            </div>`
          : nothing
      }
      ${
        this._email
          ? html`<div class="switch-row">
                <sl-switch
                  class="email-account-switch"
                  ?checked=${this._email.account_enabled}
                  ?disabled=${!this.canManageAccount || !!this._switching}
                  @sl-change=${(event: Event) =>
                    void this._toggleEmail(
                      'account_enabled',
                      (event.target as HTMLInputElement).checked
                    )}
                  >Weekly report for owners and admins</sl-switch
                >
                <div class="meta">
                  ${this._email.account_enabled ? 'Owners and admins receive factual exceptions on Mondays when actionable observations change. Quiet weeks send no email.' : 'Owners and admins do not receive the weekly report.'}
                  Manage personal reports for members in
                  <a href="/console/settings/account">Account settings</a>.
                  ${this.canManageAccount ? '' : ' Owners and admins can change this.'}
                </div>
              </div>
              <div class="switch-row">
                <sl-switch
                  class="email-user-switch"
                  ?checked=${this._email.user_enabled}
                  ?disabled=${!!this._switching}
                  @sl-change=${(event: Event) =>
                    void this._toggleEmail(
                      'user_enabled',
                      (event.target as HTMLInputElement).checked
                    )}
                  >Send it to me</sl-switch
                >
              </div>`
          : nothing
      }
    </section>`;
  }

  private async _reloadVersions(): Promise<void> {
    this._versions = await listGovernancePolicyVersions();
  }

  private get _dirty(): boolean {
    return !!this._policy && this._draft !== this._policy.body_markdown;
  }

  private async _save(): Promise<void> {
    if (!this._dirty || this._saving) return;
    this._saving = true;
    try {
      const saved = await saveGovernancePolicy(this._draft);
      this._applyPolicy(saved);
      await this._reloadVersions();
      showToast(`Saved as version ${saved.version}.`, 'success');
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : 'Failed to save the policy',
        'danger'
      );
    } finally {
      this._saving = false;
    }
  }

  private async _restore(version: GovernancePolicyVersion): Promise<void> {
    const confirmed = await this.confirm({
      title: `Restore version ${version.version}?`,
      message: `This copies version ${version.version} into a new version and makes it active.`,
      detail: this._dirty
        ? 'Your unsaved edits will be discarded. Nothing in the history is deleted.'
        : 'Nothing in the history is deleted.',
      confirmLabel: 'Restore',
      variant: 'primary',
    });
    if (!confirmed) return;
    this._restoring = version.version;
    try {
      const restored = await restoreGovernancePolicyVersion(version.version);
      this._applyPolicy(restored);
      await this._reloadVersions();
      showToast(
        `Version ${version.version} restored as version ${restored.version}.`,
        'success'
      );
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : 'Failed to restore',
        'danger'
      );
    } finally {
      this._restoring = null;
    }
  }

  private async _saveSettings(): Promise<void> {
    this._savingSettings = true;
    try {
      const body: { mode: ChosenMode; analysis_model_id?: string | null } = {
        mode: this._modeDraft,
      };
      if (this._modeDraft === 'on_own_model') {
        body.analysis_model_id = this._modelDraft || null;
      }
      this._applySettings(await updateGovernanceReviewSettings(body));
      showToast('Analysis model saved.', 'success');
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : 'Failed to save settings',
        'danger'
      );
    } finally {
      this._savingSettings = false;
    }
  }

  private async _keepOn(): Promise<void> {
    this._keepingOn = true;
    try {
      this._applySettings(
        await keepGovernanceReviewOn(this._modelDraft || null)
      );
      showToast('The review is on with your model.', 'success');
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : 'Failed to keep the review on',
        'danger'
      );
    } finally {
      this._keepingOn = false;
    }
  }

  /**
   * The picker needs its options: a select holding a value it cannot list
   * stalls the page, and a read-only member has nothing to pick anyway.
   */
  private get _modelPickable(): boolean {
    return this.canManage && this._models.length > 0;
  }

  private _who(version: GovernancePolicyVersion): string {
    if (!version.created_by_id) {
      return version.source === 'default_template'
        ? 'Default template'
        : 'Preloop';
    }
    return this._userNames.get(version.created_by_id) ?? 'A team member';
  }

  private _renderBanner() {
    if (this._settings?.mode !== 'off_over_budget') return nothing;
    return html`
      <sl-alert variant="warning" open class="over-budget-banner">
        <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
        <strong>The governance review is off.</strong>
        The last review went over the free Preloop-funded analysis allowance.
        Keep it on with your own model.
        ${
          this.canManage
            ? html`<div class="toolbar">
                <sl-button
                  size="small"
                  variant="primary"
                  class="keep-on"
                  ?loading=${this._keepingOn}
                  @click=${this._keepOn}
                >
                  Keep on with my own model
                </sl-button>
              </div>`
            : html`<div class="meta">
                Ask someone who manages policies to turn it back on.
              </div>`
        }
      </sl-alert>
    `;
  }

  private _renderLegacyNotice() {
    const blocks =
      this._policy?.watchlist.legacy_rule_blocks ??
      this._thresholds?.legacy_rule_blocks ??
      0;
    if (!blocks) return nothing;
    return html`<sl-alert variant="neutral" open class="legacy-blocks">
      <sl-icon slot="icon" name="info-circle"></sl-icon>
      This policy still has ${blocks} hidden rule
      block${blocks === 1 ? '' : 's'} from the old format. You no longer need
      them: the review reads the text, and the numbers now live in What the
      review measures below.
      ${
        this.canManage
          ? html`<div class="toolbar">
              <sl-button
                size="small"
                class="convert-legacy"
                ?loading=${this._converting}
                @click=${this._convertLegacy}
                >Move the numbers and remove the blocks</sl-button
              >
            </div>`
          : nothing
      }
    </sl-alert>`;
  }

  private _renderEditor() {
    const policy = this._policy;
    if (!policy) return nothing;
    return html`
      <section>
        <h3>AI usage policy</h3>
        <div class="meta policy-intro">
          Write your policy in plain language, or paste the one you already
          have. Each week the review reads all of it and reports what your
          team's agent sessions did against it. It never blocks work. Rules you
          call a requirement are reported as violations, preferences as
          inefficiencies.
        </div>
        ${this._renderLegacyNotice()}
        <div class="toolbar">
          <sl-radio-group
            size="small"
            aria-label="Show the text or the preview"
            value=${this._view}
            @sl-change=${(event: Event) =>
              (this._view = (event.target as HTMLInputElement).value as
                'edit' | 'preview')}
          >
            <sl-radio-button value="edit"
              >${this.canManage ? 'Edit' : 'Text'}</sl-radio-button
            >
            <sl-radio-button value="preview">Preview</sl-radio-button>
          </sl-radio-group>
          <span class="meta"
            >Version ${policy.version}, saved
            ${formatRelativeTime(policy.created_at)}</span
          >
          <span class="spacer"></span>
          ${
            this.canManage
              ? html`
                  <sl-button
                    size="small"
                    ?disabled=${!this._dirty}
                    @click=${() => (this._draft = policy.body_markdown)}
                    >Discard</sl-button
                  >
                  <sl-button
                    size="small"
                    variant="primary"
                    class="save-policy"
                    ?disabled=${!this._dirty}
                    ?loading=${this._saving}
                    @click=${this._save}
                    >Save as new version</sl-button
                  >
                `
              : html`<span class="meta read-only-note"
                  >Read only. Editing needs the manage policies
                  permission.</span
                >`
          }
        </div>
        ${
          this._view === 'edit'
            ? html`<textarea
                class="policy-source"
                aria-label="AI usage policy"
                ?readonly=${!this.canManage}
                .value=${this._draft}
                @input=${(event: Event) =>
                  (this._draft = (event.target as HTMLTextAreaElement).value)}
              ></textarea>`
            : html`<div class="markdown-body policy-preview">
                ${unsafeHTML(markRuleHeadings(renderMarkdown(this._draft)))}
              </div>`
        }
      </section>
    `;
  }

  private _renderThresholdField(kind: string, field: GovernanceThresholdField) {
    const name = `${kind}.${field.key}`;
    const value = this._thresholdDraft[name] ?? field.value;
    const error = this._thresholdError(field, value);
    const note = error || SOURCE_NOTES[field.source];
    const onChange = (event: Event) => {
      this._thresholdDraft = {
        ...this._thresholdDraft,
        [name]: (event.target as HTMLInputElement).value,
      };
    };
    if (field.choices.length) {
      return html`<sl-select
        size="small"
        data-field=${name}
        label=${field.label}
        help-text=${note}
        aria-invalid=${String(Boolean(error))}
        value=${String(value)}
        ?disabled=${!this.canManage}
        @sl-change=${onChange}
      >
        ${field.choices.map(
          (choice) =>
            html`<sl-option value=${choice}
              >${choice === 'any' ? 'Any model' : `A ${choice} model`}</sl-option
            >`
        )}
      </sl-select>`;
    }
    return html`<sl-input
      size="small"
      type="number"
      data-field=${name}
      label=${field.label}
      help-text=${note}
      aria-invalid=${String(Boolean(error))}
      value=${String(value)}
      min=${field.minimum ?? nothing}
      max=${field.maximum ?? nothing}
      step=${field.integer ? '1' : 'any'}
      ?disabled=${!this.canManage}
      @sl-input=${onChange}
    >
      ${
        field.unit
          ? html`<span slot="suffix" class="meta">${field.unit}</span>`
          : nothing
      }
    </sl-input>`;
  }

  private _renderThresholds() {
    const thresholds = this._thresholds;
    if (!thresholds) return nothing;
    return html`
      <section>
        <h3>What the review measures</h3>
        <div class="meta policy-intro">
          Some checks are counted, not read: repeated errors, error rate,
          caching, unused tools, model size per task, tool loops and context
          size. These are the numbers they use. Most teams keep the defaults.
        </div>
        <form
          class="thresholds"
          @submit=${(event: Event) => {
            event.preventDefault();
            void this._saveThresholds();
          }}
        >
          ${thresholds.groups.map(
            (group) =>
              html`<div class="threshold-group" data-kind=${group.kind}>
                <div>
                  <h4>${group.label}</h4>
                  <div class="meta">${group.help}</div>
                </div>
                <div class="threshold-fields">
                  ${group.fields.map((field) =>
                    this._renderThresholdField(group.kind, field)
                  )}
                </div>
              </div>`
          )}
          ${
            this.canManage
              ? html`<div class="toolbar">
                  <sl-button
                    size="small"
                    variant="primary"
                    class="save-thresholds"
                    ?disabled=${!this._thresholdsDirty || !this._thresholdsValid}
                    ?loading=${this._savingThresholds}
                    @click=${() => this._saveThresholds()}
                    >Save measured checks</sl-button
                  >
                  <sl-button
                    size="small"
                    class="reset-thresholds"
                    ?disabled=${this._savingThresholds}
                    @click=${this._resetThresholds}
                    >Reset to defaults</sl-button
                  >
                </div>`
              : nothing
          }
        </form>
      </section>
    `;
  }

  private _renderHistory() {
    return html`
      <section>
        <h3>Version history</h3>
        <table class="versions">
          <thead>
            <tr>
              <th>Version</th>
              <th>Saved by</th>
              <th>When</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${this._versions.map(
              (version) =>
                html`<tr data-version=${version.version}>
                  <td>
                    v${version.version}
                    ${
                      version.is_active
                        ? html`<sl-badge pill variant="success"
                            >Active</sl-badge
                          >`
                        : nothing
                    }
                  </td>
                  <td>${this._who(version)}</td>
                  <td title=${formatLocalDateTime(version.created_at)}>
                    ${formatRelativeTime(version.created_at, new Date(), {
                      maxRelativeDays: 3650,
                    })}
                  </td>
                  <td class="actions">
                    ${
                      this.canManage && !version.is_active
                        ? html`<sl-button
                            size="small"
                            class="restore"
                            ?loading=${this._restoring === version.version}
                            @click=${() => this._restore(version)}
                            >Restore</sl-button
                          >`
                        : nothing
                    }
                  </td>
                </tr>`
            )}
          </tbody>
        </table>
      </section>
    `;
  }

  private _renderSettings() {
    const settings = this._settings;
    if (!settings) return nothing;
    const ownModel = this._modeDraft === 'on_own_model';
    return html`
      <section>
        <h3>Analysis model</h3>
        <div class="settings-grid">
          <sl-radio-group
            label="Run the review"
            class="mode"
            value=${this._modeDraft}
            ?disabled=${!this.canManage}
            @sl-change=${(event: Event) =>
              (this._modeDraft = (event.target as HTMLInputElement)
                .value as ChosenMode)}
          >
            ${(Object.keys(MODE_LABELS) as ChosenMode[]).map(
              (mode) =>
                html`<sl-radio value=${mode} ?disabled=${!this.canManage}
                  >${MODE_LABELS[mode]}</sl-radio
                >`
            )}
          </sl-radio-group>
          ${
            ownModel && !this._modelPickable
              ? html`<div class="analysis-model">
                  Analysis model: ${settings.analysis_model_alias || 'not set'}
                </div>`
              : nothing
          }
          ${
            ownModel && this._modelPickable
              ? html`<sl-select
                  label="Analysis model"
                  class="model"
                  placeholder="Choose a gateway model"
                  value=${this._modelDraft}
                  ?disabled=${!this.canManage}
                  @sl-change=${(event: Event) =>
                    (this._modelDraft = String(
                      (event.target as HTMLInputElement).value
                    ))}
                >
                  ${this._models.map(
                    (model) =>
                      html`<sl-option value=${model.id}
                        >${model.name}</sl-option
                      >`
                  )}
                </sl-select>`
              : nothing
          }
          <div class="meta privacy">${PRIVACY_SENTENCE}</div>
          ${
            this.canManage
              ? html`<div>
                  <sl-button
                    size="small"
                    variant="primary"
                    class="save-settings"
                    ?disabled=${ownModel && !this._modelDraft}
                    ?loading=${this._savingSettings}
                    @click=${this._saveSettings}
                    >Save settings</sl-button
                  >
                </div>`
              : nothing
          }
        </div>
      </section>
    `;
  }

  render() {
    if (this._loading) {
      return html`<sl-spinner></sl-spinner>`;
    }
    if (this._error) {
      return html`<sl-alert variant="danger" open>
        <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
        ${this._error}
      </sl-alert>`;
    }
    return html`
      ${this._renderBanner()} ${this._renderSwitches()} ${this._renderEditor()}
      ${this._renderThresholds()} ${this._renderSettings()}
      ${this._renderHistory()}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'governance-policy-panel': GovernancePolicyPanel;
  }
}
