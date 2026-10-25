import { formatUsd, formatUsdExact } from '../utils/money';
import { parseUTCDate } from '../utils/date';
import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { getFeatures, getHostedModels, type HostedModelCatalog } from '../api';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/progress-bar/progress-bar.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

/** Durable spend and open reservations are distinct; unknown never means zero. */
@customElement('hosted-allowance')
export class HostedAllowance extends LitElement {
  @property({ type: Boolean, attribute: 'show-models' }) showModels = false;
  /**
   * A small side card (Cost page): allowance, used, remaining as a bar and
   * the reset date. Unverified figures become one subtle badge with the
   * explanation in a tooltip, so the card never grows past a few lines.
   */
  @property({ type: Boolean, reflect: true }) compact = false;
  @state() private catalog: HostedModelCatalog | null = null;
  @state() private enabled = false;
  @state() private error = '';
  async connectedCallback(): Promise<void> {
    super.connectedCallback();
    try {
      this.enabled = (await getFeatures()).features.hosted_models === true;
      if (this.enabled) this.catalog = await getHostedModels();
    } catch {
      this.error = 'Built-in model allowance could not be loaded.';
    }
  }

  render() {
    if (!this.enabled) return nothing;
    if (this.error)
      return html`<sl-alert variant="warning" open>${this.error}</sl-alert>`;
    if (!this.catalog)
      return html`<p role="status">Loading built-in model allowance…</p>`;
    if (this.compact) return this.renderCompact();
    const { allowance, models } = this.catalog;
    return html`<section aria-label="Built-in hosted models">
      <h2>Built-in (Preloop hosted)</h2>
      <p>
        Operated by Preloop, metered against your allowance. Your own provider
        keys are billed by your provider.
      </p>
      <dl aria-label="Hosted allowance">
        <div>
          <dt>
            ${allowance.kind === 'one_time' ? 'One-time credit' : 'Included allowance'}
          </dt>
          <dd>
            ${allowance.included_usd === null ? 'Not verified' : html`<span title=${formatUsdExact(allowance.included_usd)}>${formatUsd(allowance.included_usd)}</span>`}
          </dd>
        </div>
        <div>
          <dt>Spent</dt>
          <dd>
            ${allowance.spent_usd === null ? 'Not verified' : html`<span title=${formatUsdExact(allowance.spent_usd)}>${formatUsd(allowance.spent_usd)}</span>`}
          </dd>
        </div>
        <div>
          <dt>Held (open reservations)</dt>
          <dd>
            ${allowance.held_usd === null ? 'Not verified' : html`<span title=${formatUsdExact(allowance.held_usd)}>${formatUsd(allowance.held_usd)}</span>`}
          </dd>
        </div>
        <div>
          <dt>Remaining</dt>
          <dd>
            ${allowance.remaining_usd === null ? 'Not verified' : html`<span title=${formatUsdExact(allowance.remaining_usd)}>${formatUsd(allowance.remaining_usd)}</span>`}
          </dd>
        </div>
      </dl>
      <p>
        ${allowance.kind === 'one_time' ? 'One-time credit does not reset.' : allowance.reset_at ? `Resets ${parseUTCDate(allowance.reset_at).toLocaleDateString()}.` : 'Monthly reset date is not yet verified.'}
      </p>
      ${allowance.coverage !== 'known' ? html`<p>Some balances are not yet verified. Unverified figures are not zero usage.</p>` : nothing}
      ${
        this.showModels
          ? html`<ul>
              ${models.map(
                (model) =>
                  html`<li>
                    <strong>${model.name}</strong> · ${model.provider_name} ·
                    <code>${model.alias}</code>
                    ${model.tariff ? html`<p>Tariff: ${model.tariff.input_price_per_1k == null ? 'Not verified' : html`<span title=${formatUsdExact(model.tariff.input_price_per_1k * 1000)}>${formatUsd(model.tariff.input_price_per_1k * 1000)}</span>`} / million input tokens; ${model.tariff.output_price_per_1k == null ? 'Not verified' : html`<span title=${formatUsdExact(model.tariff.output_price_per_1k * 1000)}>${formatUsd(model.tariff.output_price_per_1k * 1000)}</span>`} / million output tokens; ${model.tariff.request_price == null ? 'Not verified' : html`<span title=${formatUsdExact(model.tariff.request_price)}>${formatUsd(model.tariff.request_price)}</span>`} / request.</p>` : html`<p>Tariff not verified; this model is unavailable.</p>`}
                    ${model.own_alias_shadowing ? html`<p>Your own model uses this alias and takes precedence. Calls to that alias use your key.</p>` : nothing}
                  </li>`
              )}
            </ul>`
          : nothing
      }
    </section>`;
  }
  private renderCompact() {
    const { allowance } = this.catalog!;
    const included = allowance.included_usd;
    const used =
      allowance.spent_usd === null
        ? null
        : allowance.spent_usd + (allowance.held_usd ?? 0);
    const remaining = allowance.remaining_usd;
    const known = included !== null && used !== null && included > 0;
    const percent = known ? Math.min(100, (used / included) * 100) : 0;
    const unverified =
      allowance.coverage !== 'known' || included === null || used === null;
    const reset =
      allowance.kind === 'one_time'
        ? 'One-time credit, no reset'
        : allowance.reset_at
          ? `Resets ${parseUTCDate(allowance.reset_at).toLocaleDateString()}`
          : 'Reset date not verified';
    const money = (value: number | null) =>
      value === null
        ? '?'
        : html`<span title=${formatUsdExact(value)}>${formatUsd(value)}</span>`;
    return html`<section
      class="compact"
      aria-label="Built-in hosted models allowance"
    >
      <div class="compact-head">
        <span class="compact-title"
          >${allowance.kind === 'one_time' ? 'Built-in credit' : 'Built-in allowance'}</span
        >
        ${
          unverified
            ? html`<sl-tooltip
                content="Some balances are not yet verified. Unverified figures are not zero usage."
              >
                <sl-badge variant="neutral" pill tabindex="0"
                  >Not verified</sl-badge
                >
              </sl-tooltip>`
            : nothing
        }
      </div>
      <div class="compact-figures">
        <span><strong>${money(used)}</strong> used of ${money(included)}</span>
        <span>${money(remaining)} left</span>
      </div>
      <sl-progress-bar
        value=${percent}
        label="Built-in allowance used"
        class=${percent >= 90 ? 'high' : ''}
      ></sl-progress-bar>
      <div class="compact-meta">
        ${reset}${
          allowance.held_usd
            ? html` · ${money(allowance.held_usd)} held by open requests`
            : nothing
        }
      </div>
    </section>`;
  }

  static styles = css`
    :host {
      display: block;
    }
    h2 {
      font-size: var(--console-text-card-title, 0.9375rem);
      font-weight: 600;
    }
    section.compact {
      padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      margin: 0;
      border: 1px solid var(--console-hairline, var(--sl-color-neutral-200));
      border-radius: var(--sl-border-radius-large);
      background: var(--console-surface, var(--sl-color-neutral-0));
      display: grid;
      gap: var(--sl-spacing-2x-small);
      font-size: var(--console-text-body, var(--sl-font-size-small));
    }
    .compact-head,
    .compact-figures {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--sl-spacing-x-small);
    }
    .compact-title {
      font-weight: 600;
    }
    .compact-meta {
      font-size: var(--console-text-caption, var(--sl-font-size-x-small));
      color: var(--console-meta-color, var(--sl-color-neutral-600));
    }
    sl-progress-bar {
      --height: 6px;
    }
    sl-progress-bar.high {
      --indicator-color: var(--sl-color-warning-600);
    }
    section {
      padding: 1.5rem;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: 12px;
      margin: 1rem 0;
    }
    dl {
      display: flex;
      gap: 2rem;
      flex-wrap: wrap;
    }
    dt {
      font-size: 0.9rem;
    }
    dd {
      margin: 0.5rem 0;
      font-weight: 600;
    }
    li {
      margin: 1rem 0;
    }
    code {
      overflow-wrap: anywhere;
    }
  `;
}
