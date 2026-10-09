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
  private money(value: number | null): string {
    return value === null
      ? 'Not verified'
      : new Intl.NumberFormat('en-US', {
          style: 'currency',
          currency: 'USD',
          maximumFractionDigits: 4,
        }).format(value);
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
          <dd>${this.money(allowance.included_usd)}</dd>
        </div>
        <div>
          <dt>Spent</dt>
          <dd>${this.money(allowance.spent_usd)}</dd>
        </div>
        <div>
          <dt>Held (open reservations)</dt>
          <dd>${this.money(allowance.held_usd)}</dd>
        </div>
        <div>
          <dt>Remaining</dt>
          <dd>${this.money(allowance.remaining_usd)}</dd>
        </div>
      </dl>
      <p>
        ${allowance.reset_at ? `Resets ${new Date(allowance.reset_at).toLocaleDateString()}.` : 'One-time credit does not reset.'}
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
                    ${model.tariff ? html`<p>Tariff: ${this.money(model.tariff.input_price_per_1k * 1000)} / million input tokens; ${this.money(model.tariff.output_price_per_1k * 1000)} / million output tokens; ${this.money(model.tariff.request_price)} / request.</p>` : html`<p>Tariff not verified; this model is unavailable.</p>`}
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
