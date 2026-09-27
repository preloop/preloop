import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import {
  getSpendOutlierSettings,
  updateSpendOutlierSettings,
  type SpendOutlierSettings,
} from '../spend-outliers-api';
import { consoleDialogStyles } from '../styles/console-dialog';

/** The form, as text, so a half-typed number is not coerced mid-edit. */
interface SpendOutlierForm {
  dailyMultiple: string;
  minHistoryDays: string;
  prefixes: string;
  sharePercent: string;
  sessionThreshold: string;
}

function toForm(settings: SpendOutlierSettings): SpendOutlierForm {
  return {
    dailyMultiple: String(settings.daily_multiple),
    minHistoryDays: String(settings.min_history_days),
    prefixes: settings.top_tier_model_prefixes.join('\n'),
    sharePercent: String(Math.round(settings.top_tier_share * 100)),
    sessionThreshold:
      settings.session_cost_threshold_usd === null
        ? ''
        : String(settings.session_cost_threshold_usd),
  };
}

/**
 * Parses the form into a settings payload, or returns a message naming the
 * first field that is wrong. The server validates the same bounds; checking
 * here only saves a round trip.
 */
export function parseSpendOutlierForm(
  form: SpendOutlierForm
): SpendOutlierSettings | string {
  const dailyMultiple = Number(form.dailyMultiple);
  if (!Number.isFinite(dailyMultiple) || dailyMultiple < 1) {
    return 'The daily multiple must be a number of at least 1.';
  }
  const minHistoryDays = Number(form.minHistoryDays);
  if (
    !Number.isInteger(minHistoryDays) ||
    minHistoryDays < 1 ||
    minHistoryDays > 28
  ) {
    return 'History days must be a whole number from 1 to 28.';
  }
  const sharePercent = Number(form.sharePercent);
  if (
    !Number.isFinite(sharePercent) ||
    sharePercent <= 0 ||
    sharePercent >= 100
  ) {
    return 'The top-tier share must be between 0 and 100 percent.';
  }
  let sessionThreshold: number | null = null;
  if (form.sessionThreshold.trim() !== '') {
    sessionThreshold = Number(form.sessionThreshold);
    if (!Number.isFinite(sessionThreshold) || sessionThreshold <= 0) {
      return 'The session threshold must be a positive dollar amount, or empty.';
    }
  }
  const prefixes = form.prefixes
    .split(/[\n,]/)
    .map((prefix) => prefix.trim())
    .filter(Boolean);
  return {
    daily_multiple: dailyMultiple,
    min_history_days: minHistoryDays,
    top_tier_model_prefixes: prefixes,
    top_tier_share: sharePercent / 100,
    session_cost_threshold_usd: sessionThreshold,
  };
}

/**
 * Thresholds for the spend outlier cards (#960). Opened from the Attention
 * page; saving applies from the next evaluation, and cards already raised
 * stay until they are dismissed or age out.
 */
@customElement('spend-outlier-settings-dialog')
export class SpendOutlierSettingsDialog extends LitElement {
  @property({ type: Boolean, reflect: true }) open = false;

  @state() private form: SpendOutlierForm | null = null;
  @state() private loadError: string | null = null;
  @state() private saveError: string | null = null;
  @state() private saving = false;

  static styles = [
    consoleDialogStyles,
    css`
      .description {
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
        margin-bottom: var(--sl-spacing-medium);
      }

      form {
        display: grid;
        gap: var(--sl-spacing-medium);
      }

      .error {
        color: var(--sl-color-danger-600);
        font-size: var(--sl-font-size-small);
      }
    `,
  ];

  protected updated(changed: Map<string, unknown>): void {
    if (changed.has('open') && this.open) {
      void this.load();
    }
  }

  private async load(): Promise<void> {
    this.form = null;
    this.loadError = null;
    this.saveError = null;
    try {
      this.form = toForm(await getSpendOutlierSettings());
    } catch (error) {
      this.loadError =
        error instanceof Error ? error.message : 'Failed to load settings';
    }
  }

  private setField(field: keyof SpendOutlierForm, event: Event): void {
    if (!this.form) return;
    const value = (event.target as HTMLInputElement).value;
    this.form = { ...this.form, [field]: value };
  }

  private async save(event: Event): Promise<void> {
    event.preventDefault();
    if (!this.form) return;
    const parsed = parseSpendOutlierForm(this.form);
    if (typeof parsed === 'string') {
      this.saveError = parsed;
      return;
    }
    this.saving = true;
    this.saveError = null;
    try {
      const saved = await updateSpendOutlierSettings(parsed);
      this.dispatchEvent(
        new CustomEvent('spend-outlier-settings-changed', {
          bubbles: true,
          composed: true,
          detail: { settings: saved },
        })
      );
      this.close();
    } catch (error) {
      this.saveError =
        error instanceof Error ? error.message : 'Failed to save settings';
    } finally {
      this.saving = false;
    }
  }

  private close(): void {
    const dialog = this.shadowRoot?.querySelector('sl-dialog');
    if (dialog) {
      dialog.open = false;
    }
    this.handleHidden();
  }

  private handleHide(event: Event): void {
    if (event.target !== event.currentTarget) {
      event.stopPropagation();
      return;
    }
    this.handleHidden();
  }

  private handleHidden(): void {
    if (!this.open) return;
    this.open = false;
    this.dispatchEvent(
      new CustomEvent('spend-outlier-settings-hide', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private renderForm(form: SpendOutlierForm) {
    return html`
      <form @submit=${this.save}>
        <sl-input
          name="daily-multiple"
          type="number"
          step="0.1"
          min="1"
          label="Daily spend multiple"
          help-text="Flag a developer whose spend on a UTC day is this many times their 28-day median."
          .value=${form.dailyMultiple}
          @sl-input=${(event: Event) => this.setField('dailyMultiple', event)}
        ></sl-input>
        <sl-input
          name="min-history-days"
          type="number"
          step="1"
          min="1"
          max="28"
          label="Days of history needed"
          help-text="Days with spend in the last 28 before the daily rule applies to someone."
          .value=${form.minHistoryDays}
          @sl-input=${(event: Event) => this.setField('minHistoryDays', event)}
        ></sl-input>
        <sl-textarea
          name="prefixes"
          rows="3"
          label="Top-tier model prefixes"
          help-text="One per line. A model matches when its name, with or without a provider prefix, starts with one of these."
          .value=${form.prefixes}
          @sl-input=${(event: Event) => this.setField('prefixes', event)}
        ></sl-textarea>
        <sl-input
          name="share-percent"
          type="number"
          step="1"
          min="1"
          max="99"
          label="Top-tier share (percent)"
          help-text="Flag a developer when one top-tier model is more than this share of their spend two days running."
          .value=${form.sharePercent}
          @sl-input=${(event: Event) => this.setField('sharePercent', event)}
        ></sl-input>
        <sl-input
          name="session-threshold"
          type="number"
          step="0.01"
          min="0"
          label="Session cost threshold (USD)"
          help-text="Flag a single session that costs more than this. Leave empty to turn the rule off."
          .value=${form.sessionThreshold}
          @sl-input=${(event: Event) => this.setField('sessionThreshold', event)}
        ></sl-input>
        ${
          this.saveError
            ? html`<div class="error" role="alert">${this.saveError}</div>`
            : nothing
        }
        <sl-button type="submit" variant="primary" ?loading=${this.saving}
          >Save</sl-button
        >
      </form>
    `;
  }

  render() {
    return html`
      <sl-dialog
        label="Spend outlier alerts"
        style="--width: 560px;"
        ?open=${this.open}
        @sl-hide=${this.handleHide}
      >
        <div class="description">
          Rules are checked once a day on UTC days, and sessions every 15
          minutes. Each alert fires once; dismissing it hides it until the next
          day it still applies.
        </div>
        ${
          this.loadError
            ? html`<div class="error" role="alert">${this.loadError}</div>`
            : this.form
              ? this.renderForm(this.form)
              : this.open
                ? html`<sl-spinner></sl-spinner>`
                : nothing
        }
      </sl-dialog>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'spend-outlier-settings-dialog': SpendOutlierSettingsDialog;
  }
}
