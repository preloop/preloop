import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import { NO_CAPABILITIES, type CapabilitySet } from '../../../capabilities';
import './resource-access-panel';

/**
 * Runner pools are runner labels, so they have no page of their own. This
 * picks one of the account's pools and shows its sharing and tags.
 * Context: `pools`, the pool names joined by commas.
 */
@customElement('runner-pool-access')
export class RunnerPoolAccess extends LitElement {
  static styles = css`
    :host {
      display: block;
      margin-top: var(--sl-spacing-large);
    }
    h3 {
      font-size: var(--sl-font-size-medium);
      margin: 0 0 var(--sl-spacing-x-small);
    }
  `;

  @property({ attribute: false }) context: Record<string, unknown> = {};
  @property({ attribute: false }) capabilities: CapabilitySet = NO_CAPABILITIES;
  @state() private pool = '';

  private pools(): string[] {
    return String(this.context.pools ?? '')
      .split(',')
      .map((p) => p.trim())
      .filter(Boolean);
  }

  render() {
    const pools = this.pools();
    if (pools.length === 0) return nothing;
    return html`<h3>Runner pool access</h3>
      <sl-select
        size="small"
        label="Pool"
        data-testid="pool-select"
        .value=${this.pool}
        @sl-change=${(e: Event) =>
          (this.pool = (e.target as HTMLSelectElement).value)}
      >
        ${pools.map((p) => html`<sl-option value=${p}>${p}</sl-option>`)}
      </sl-select>
      ${
        this.pool
          ? html`<resource-access-panel
              .capabilities=${this.capabilities}
              .context=${{ kind: 'runner_pool', resourceId: this.pool }}
            ></resource-access-panel>`
          : nothing
      }`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'runner-pool-access': RunnerPoolAccess;
  }
}
