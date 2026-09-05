import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/divider/divider.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import {
  AUTO_RUNNER_POOL,
  buildRunnerPoolGroups,
  describeNextRunnerPool,
  type RunnerPoolSource,
} from '../utils/runner-pool';
import consoleStyles from '../styles/console-styles.css?inline';

export type RunnerPoolSelectContext = 'flow' | 'account';

@customElement('preloop-runner-pool-select')
export class PreloopRunnerPoolSelect extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      :host {
        display: block;
      }

      sl-details {
        margin-top: var(--sl-spacing-2x-small);
      }

      sl-details::part(base) {
        border: none;
        background: transparent;
      }

      sl-details::part(header) {
        padding: var(--sl-spacing-2x-small) 0;
        font-size: var(--console-text-meta, 0.8125rem);
        color: var(--sl-color-neutral-600);
      }

      .group-label {
        display: block;
        padding: var(--sl-spacing-2x-small) var(--sl-spacing-x-small) 0;
        color: var(--sl-color-neutral-600);
        font-size: var(--console-text-meta, 0.8125rem);
      }

      sl-divider {
        margin: var(--sl-spacing-2x-small) 0;
        --color: var(--console-hairline, var(--sl-color-neutral-200));
      }

      .runner-pool-hint {
        margin: var(--sl-spacing-2x-small) 0 0;
        color: var(--sl-color-neutral-600);
        font-size: var(--console-text-meta, 0.8125rem);
        line-height: 1.45;
      }
    `,
  ];

  @property({ type: String })
  value: string | null = null;

  @property({ type: String })
  context: RunnerPoolSelectContext = 'flow';

  @property({ type: Array })
  runners: RunnerPoolSource[] = [];

  @property({ type: String })
  accountPool: string | null = null;

  @property({ type: Number })
  hostedMinutesLeft: number | null = null;

  @property({ type: String })
  label = 'Runner pool';

  @property({ type: String })
  helpText = '';

  @property({ type: Boolean })
  disabled = false;

  @state()
  private localValue: string | null = null;

  @state()
  private customDraft = '';

  protected willUpdate(
    changedProperties: Map<string | number | symbol, unknown>
  ): void {
    if (changedProperties.has('value')) {
      this.localValue = this.value;
      const current = (this.value || '').trim();
      const known = new Set(
        this.groupsFor(current).flatMap((group) =>
          group.options
            .filter((option) => !option.label.endsWith('(not registered)'))
            .map((option) => option.value)
        )
      );
      if (!current || known.has(current)) {
        this.customDraft = '';
      } else if (!this.customDraft) {
        this.customDraft = current;
      }
    }
  }

  private effectiveValue(): string | null {
    return this.localValue !== null ? this.localValue : this.value;
  }

  private groupsFor(current: string | null) {
    return buildRunnerPoolGroups({
      runners: this.runners,
      context: this.context,
      accountPool: this.accountPool,
      current,
      hostedMinutesLeft: this.hostedMinutesLeft,
    });
  }

  private selectValue(): string {
    const current = (this.effectiveValue() || '').trim();
    if (this.context === 'account') {
      return current || AUTO_RUNNER_POOL;
    }
    return current;
  }

  private emitValue(value: string | null): void {
    this.localValue = value;
    this.dispatchEvent(
      new CustomEvent('pool-change', {
        detail: { value },
        bubbles: true,
        composed: true,
      })
    );
  }

  private handleSelect(event: Event): void {
    const target = event.target as HTMLSelectElement;
    const value = (target.value || '').trim();
    this.customDraft = '';
    this.emitValue(value || null);
  }

  private handleCustom(event: Event): void {
    const target = event.target as HTMLInputElement;
    this.customDraft = target.value;
    const value = (target.value || '').trim();
    this.emitValue(value || null);
  }

  private hintText(): string {
    if (this.context === 'account') {
      const pool = (this.effectiveValue() || '').trim() || AUTO_RUNNER_POOL;
      return describeNextRunnerPool({
        flowPool: pool,
        accountPool: pool,
        runners: this.runners,
        hostedMinutesLeft: this.hostedMinutesLeft,
      });
    }
    return describeNextRunnerPool({
      flowPool: this.effectiveValue(),
      accountPool: this.accountPool,
      runners: this.runners,
      hostedMinutesLeft: this.hostedMinutesLeft,
    });
  }

  render() {
    const current = this.effectiveValue();
    const groups = this.groupsFor(current);
    return html`
      <sl-select
        label=${this.label}
        help-text=${this.helpText}
        .value=${this.selectValue()}
        ?disabled=${this.disabled}
        @sl-change=${this.handleSelect}
      >
        ${groups.map(
          (group) => html`
            ${
              group.label
                ? html`<small class="group-label">${group.label}</small>
                    <sl-divider></sl-divider>`
                : nothing
            }
            ${group.options.map(
              (option) => html`
                <sl-option
                  value=${option.value}
                  ?disabled=${option.disabled === true}
                >
                  ${option.label}
                </sl-option>
              `
            )}
          `
        )}
      </sl-select>
      <sl-details summary="Type a label or runner id">
        <sl-input
          placeholder="gpu"
          .value=${this.customDraft}
          ?disabled=${this.disabled}
          @sl-input=${this.handleCustom}
        ></sl-input>
      </sl-details>
      <p class="runner-pool-hint">${this.hintText()}</p>
    `;
  }
}
