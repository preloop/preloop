import { LitElement, html, css, unsafeCSS, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { previewFlowSchedule, type SchedulePreview } from '../api';
import { formatLocalDateTime } from '../utils/date';
import consoleStyles from '../styles/console-styles.css?inline';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';

export type ScheduleUnit = 'minutes' | 'hours' | 'days';
export type Weekday = 'mon' | 'tue' | 'wed' | 'thu' | 'fri' | 'sat' | 'sun';

export type ScheduleConfig =
  | { type: 'interval'; every: number; unit: ScheduleUnit; timezone: string }
  | { type: 'daily'; at: string; timezone: string }
  | { type: 'weekly'; days: Weekday[]; at: string; timezone: string }
  | { type: 'cron'; expr: string; timezone: string };

const WEEKDAYS: { value: Weekday; label: string }[] = [
  { value: 'mon', label: 'Mon' },
  { value: 'tue', label: 'Tue' },
  { value: 'wed', label: 'Wed' },
  { value: 'thu', label: 'Thu' },
  { value: 'fri', label: 'Fri' },
  { value: 'sat', label: 'Sat' },
  { value: 'sun', label: 'Sun' },
];

/** The browser's IANA timezone, falling back to UTC. */
export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

function supportedTimezones(): string[] {
  const zones: string[] =
    typeof (Intl as any).supportedValuesOf === 'function'
      ? (Intl as any).supportedValuesOf('timeZone')
      : [];
  const set = new Set(zones);
  set.add('UTC');
  set.add(browserTimezone());
  return [...set].sort();
}

/** Sensible default when the user first picks the Schedule trigger. */
export function defaultScheduleConfig(): ScheduleConfig {
  return { type: 'daily', at: '09:00', timezone: browserTimezone() };
}

/**
 * Editor for a flow schedule trigger configuration.
 *
 * Friendly forms first (interval / daily / weekly); raw cron is behind
 * an "Advanced" toggle. Emits `schedule-change` with `{ value }` on
 * every edit and shows a live preview of the next run times computed by
 * the backend preview endpoint (min-interval and cron validation errors
 * are surfaced inline from the same call).
 */
@customElement('schedule-config-editor')
export class ScheduleConfigEditor extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }
      .schedule-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--sl-spacing-medium);
      }
      @media (max-width: 768px) {
        .schedule-grid {
          grid-template-columns: 1fr;
        }
      }
      .weekday-row {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sl-spacing-medium);
      }
      .field-label {
        display: block;
        margin-bottom: 0.5rem;
        font-weight: 500;
      }
      .advanced-toggle {
        margin: var(--sl-spacing-medium) 0;
      }
      .preview {
        margin-top: var(--sl-spacing-medium);
        padding: var(--sl-spacing-medium);
        background: var(--sl-color-neutral-50);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        font-size: var(--sl-font-size-small);
      }
      .preview-description {
        font-weight: 600;
        margin-bottom: var(--sl-spacing-x-small);
      }
      .preview-runs {
        margin: 0;
        padding-left: 1.25rem;
        color: var(--sl-color-neutral-700);
      }
      .preview-error {
        color: var(--sl-color-danger-600);
      }
      .preview-hint {
        color: var(--sl-color-neutral-500);
        margin-top: var(--sl-spacing-x-small);
      }
    `,
  ];

  /** The current schedule config value (discriminated on `type`). */
  @property({ type: Object })
  value: ScheduleConfig | undefined;

  @state()
  private previewResult: SchedulePreview | null = null;

  @state()
  private previewError: string | null = null;

  @state()
  private previewLoading = false;

  private previewDebounce?: number;
  private previewSeq = 0;

  /** Friendly mode to restore when the Advanced cron switch is turned off. */
  private lastFriendlyType: Exclude<ScheduleConfig['type'], 'cron'> = 'daily';
  private needsDefaultEmit = false;

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this.previewDebounce) clearTimeout(this.previewDebounce);
  }

  willUpdate(changed: Map<string | number | symbol, unknown>) {
    // Initialize here (not connectedCallback) so a `.value` binding
    // applied during upgrade is never clobbered by the default.
    if (!this.value) {
      this.value = defaultScheduleConfig();
      this.needsDefaultEmit = true;
    }
    if (!this.hasUpdated || changed.has('value')) {
      this.schedulePreview();
    }
  }

  firstUpdated() {
    if (this.needsDefaultEmit) {
      this.needsDefaultEmit = false;
      this.emitChange();
    }
  }

  private get config(): ScheduleConfig {
    return this.value || defaultScheduleConfig();
  }

  private patchConfig(patch: Partial<ScheduleConfig>) {
    this.value = { ...this.config, ...patch } as ScheduleConfig;
    this.emitChange();
    this.schedulePreview();
  }

  private switchType(type: ScheduleConfig['type']) {
    const timezone = this.config.timezone || browserTimezone();
    const prev = this.config as any;
    if (type === 'cron' && this.config.type !== 'cron') {
      this.lastFriendlyType = this.config.type;
    }
    let next: ScheduleConfig;
    switch (type) {
      case 'interval':
        next = { type, every: 1, unit: 'hours', timezone };
        break;
      case 'daily':
        next = { type, at: prev.at || '09:00', timezone };
        break;
      case 'weekly':
        next = { type, days: ['mon'], at: prev.at || '09:00', timezone };
        break;
      case 'cron':
      default:
        next = { type: 'cron', expr: '0 9 * * *', timezone };
        break;
    }
    this.value = next;
    this.emitChange();
    this.schedulePreview();
  }

  private emitChange() {
    this.dispatchEvent(
      new CustomEvent('schedule-change', {
        bubbles: true,
        composed: true,
        detail: { value: this.value },
      })
    );
  }

  /** Client-side guard for shapes the backend would reject as incomplete. */
  private localValidationError(): string | null {
    const config = this.config;
    if (config.type === 'weekly' && config.days.length === 0) {
      return 'Select at least one day of the week.';
    }
    if (config.type === 'cron' && !config.expr.trim()) {
      return 'Enter a cron expression (minute hour day month day-of-week).';
    }
    if (
      (config.type === 'daily' || config.type === 'weekly') &&
      !/^([01]\d|2[0-3]):([0-5]\d)$/.test(config.at)
    ) {
      return 'Enter a time of day in 24h HH:MM format.';
    }
    if (config.type === 'interval' && (!config.every || config.every < 1)) {
      return 'Enter how often the flow should run (at least 1).';
    }
    return null;
  }

  private schedulePreview() {
    // Invalidate any in-flight preview so a stale response can't overwrite
    // newer state (e.g. a local validation error shown after an edit).
    this.previewSeq++;
    if (this.previewDebounce) clearTimeout(this.previewDebounce);
    const localError = this.localValidationError();
    if (localError) {
      this.previewResult = null;
      this.previewError = localError;
      this.previewLoading = false;
      return;
    }
    this.previewLoading = true;
    this.previewDebounce = window.setTimeout(() => {
      void this.fetchPreview();
    }, 350);
  }

  private async fetchPreview() {
    const seq = this.previewSeq;
    try {
      const preview = await previewFlowSchedule(this.config);
      if (seq !== this.previewSeq) return;
      this.previewResult = preview;
      this.previewError = null;
    } catch (error) {
      if (seq !== this.previewSeq) return;
      this.previewResult = null;
      this.previewError =
        error instanceof Error ? error.message : 'Invalid schedule.';
    } finally {
      if (seq === this.previewSeq) {
        this.previewLoading = false;
      }
    }
  }

  private toggleWeekday(day: Weekday, checked: boolean) {
    const config = this.config;
    if (config.type !== 'weekly') return;
    const days = checked
      ? [...config.days, day]
      : config.days.filter((d) => d !== day);
    const ordered = WEEKDAYS.map((d) => d.value).filter((d) =>
      days.includes(d)
    );
    this.patchConfig({ days: ordered } as Partial<ScheduleConfig>);
  }

  private renderIntervalFields() {
    const config = this.config;
    if (config.type !== 'interval') return nothing;
    return html`
      <div class="schedule-grid">
        <sl-input
          type="number"
          label="Every"
          min="1"
          .value=${String(config.every)}
          @sl-input=${(e: any) =>
            this.patchConfig({
              every: Number(e.target.value) || 0,
            } as Partial<ScheduleConfig>)}
        ></sl-input>
        <sl-select
          label="Unit"
          .value=${config.unit}
          @sl-change=${(e: any) =>
            this.patchConfig({
              unit: e.target.value,
            } as Partial<ScheduleConfig>)}
        >
          <sl-option value="minutes">Minutes</sl-option>
          <sl-option value="hours">Hours</sl-option>
          <sl-option value="days">Days</sl-option>
        </sl-select>
      </div>
    `;
  }

  private renderDailyFields() {
    const config = this.config;
    if (config.type !== 'daily') return nothing;
    return html`
      <sl-input
        type="time"
        label="At time"
        .value=${config.at}
        @sl-input=${(e: any) =>
          this.patchConfig({ at: e.target.value } as Partial<ScheduleConfig>)}
      ></sl-input>
    `;
  }

  private renderWeeklyFields() {
    const config = this.config;
    if (config.type !== 'weekly') return nothing;
    return html`
      <div style="margin-bottom: var(--sl-spacing-medium);">
        <label class="field-label">On days</label>
        <div class="weekday-row">
          ${WEEKDAYS.map(
            (day) => html`
              <sl-checkbox
                .checked=${config.days.includes(day.value)}
                @sl-change=${(e: any) =>
                  this.toggleWeekday(day.value, e.target.checked)}
              >
                ${day.label}
              </sl-checkbox>
            `
          )}
        </div>
      </div>
      <sl-input
        type="time"
        label="At time"
        .value=${config.at}
        @sl-input=${(e: any) =>
          this.patchConfig({ at: e.target.value } as Partial<ScheduleConfig>)}
      ></sl-input>
    `;
  }

  private renderCronFields() {
    const config = this.config;
    if (config.type !== 'cron') return nothing;
    return html`
      <sl-input
        label="Cron expression"
        help-text="5-field crontab: minute hour day-of-month month day-of-week (e.g. '0 9 * * mon-fri')"
        .value=${config.expr}
        @sl-input=${(e: any) =>
          this.patchConfig({ expr: e.target.value } as Partial<ScheduleConfig>)}
      ></sl-input>
    `;
  }

  private renderPreview() {
    if (this.previewError) {
      return html`
        <div class="preview">
          <div class="preview-error" data-testid="schedule-preview-error">
            <sl-icon
              name="exclamation-triangle"
              style="vertical-align: -0.125em;"
            ></sl-icon>
            ${this.previewError}
          </div>
        </div>
      `;
    }
    if (this.previewLoading && !this.previewResult) {
      return html`
        <div class="preview">
          <sl-spinner style="font-size: 1rem;"></sl-spinner>
          Computing next run times...
        </div>
      `;
    }
    if (!this.previewResult) return nothing;
    return html`
      <div class="preview" data-testid="schedule-preview">
        <div class="preview-description">
          <sl-icon
            name="calendar-check"
            style="vertical-align: -0.125em;"
          ></sl-icon>
          ${this.previewResult.description}
        </div>
        ${
          this.previewResult.next_run_times.length > 0
            ? html`
                <div>Next runs (your local time):</div>
                <ol class="preview-runs">
                  ${this.previewResult.next_run_times
                    .slice(0, 3)
                    .map((t) => html`<li>${formatLocalDateTime(t)}</li>`)}
                </ol>
              `
            : html`<div>This schedule never fires.</div>`
        }
        <div class="preview-hint">
          Evaluated in ${this.previewResult.timezone}. Runs are skipped while a
          previous run is still in progress; paused flows do not fire.
        </div>
      </div>
    `;
  }

  render() {
    const config = this.config;
    const isCron = config.type === 'cron';
    return html`
      <div>
        ${
          !isCron
            ? html`
                <div style="margin-bottom: var(--sl-spacing-medium);">
                  <label class="field-label">Repeats</label>
                  <sl-radio-group
                    value=${config.type}
                    @sl-change=${(e: any) => this.switchType(e.target.value)}
                    style="display: flex; gap: var(--sl-spacing-large);"
                  >
                    <sl-radio value="interval">Interval</sl-radio>
                    <sl-radio value="daily">Daily</sl-radio>
                    <sl-radio value="weekly">Weekly</sl-radio>
                  </sl-radio-group>
                </div>
              `
            : nothing
        }
        ${this.renderIntervalFields()} ${this.renderDailyFields()}
        ${this.renderWeeklyFields()} ${this.renderCronFields()}

        <sl-select
          label="Timezone"
          .value=${config.timezone}
          @sl-change=${(e: any) =>
            this.patchConfig({
              timezone: e.target.value,
            } as Partial<ScheduleConfig>)}
          style="margin-top: var(--sl-spacing-medium);"
        >
          ${supportedTimezones().map(
            (tz) => html`<sl-option .value=${tz}>${tz}</sl-option>`
          )}
        </sl-select>

        <div class="advanced-toggle">
          <sl-switch
            .checked=${isCron}
            @sl-change=${(e: any) =>
              this.switchType(
                e.target.checked ? 'cron' : this.lastFriendlyType
              )}
          >
            Advanced: cron expression
          </sl-switch>
        </div>

        ${this.renderPreview()}
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'schedule-config-editor': ScheduleConfigEditor;
  }
}
