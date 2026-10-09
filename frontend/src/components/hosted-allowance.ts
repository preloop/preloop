import { formatUsd, formatUsdExact } from '../utils/money';
import { parseUTCDate } from '../utils/date';
import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { getFeatures, getHostedModels, type HostedModelCatalog } from '../api';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';

/** Durable spend and open reservations are distinct; unknown never means zero. */
@customElement('hosted-allowance')
export class HostedAllowance extends LitElement {
  @property({ type: Boolean, attribute: 'show-models' }) showModels = false;
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
                    ${model.tariff ? html`<p>Tariff: ${model.tariff.input_price_per_1k * 1000 === null ? 'Not verified' : html`<span title=${formatUsdExact(model.tariff.input_price_per_1k * 1000)}>${formatUsd(model.tariff.input_price_per_1k * 1000)}</span>`} / million input tokens; ${model.tariff.output_price_per_1k * 1000 === null ? 'Not verified' : html`<span title=${formatUsdExact(model.tariff.output_price_per_1k * 1000)}>${formatUsd(model.tariff.output_price_per_1k * 1000)}</span>`} / million output tokens; ${model.tariff.request_price === null ? 'Not verified' : html`<span title=${formatUsdExact(model.tariff.request_price)}>${formatUsd(model.tariff.request_price)}</span>`} / request.</p>` : html`<p>Tariff not verified; this model is unavailable.</p>`}
                    ${model.own_alias_shadowing ? html`<p>Your own model uses this alias and takes precedence. Calls to that alias use your key.</p>` : nothing}
                  </li>`
              )}
            </ul>`
          : nothing
      }
    </section>`;
  }
  static styles = css`
    :host {
      display: block;
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
