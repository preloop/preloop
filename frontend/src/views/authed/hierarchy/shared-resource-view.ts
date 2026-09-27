import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '../../../components/view-header.ts';
import consoleStyles from '../../../styles/console-styles.css?inline';
import {
  currentAccountId,
  getSharedResource,
  type SharedResource,
} from '../../../hierarchy-api';
import { isCapabilityOff } from '../../../capabilities';
import { sharedFromBadge } from './shared-badge';

const KIND_LABELS: Record<string, string> = {
  ai_model: 'AI model',
  mcp_server: 'MCP server',
  managed_agent: 'Managed agent',
  flow: 'Flow',
  runner_pool: 'Runner pool',
  policy: 'Policy baseline',
};

/**
 * The only fields a subaccount sees of a shared resource. Rendering goes
 * through this list and nothing else, so a credential the server might send
 * by mistake still never reaches the page.
 */
const VISIBLE_FIELDS: ReadonlyArray<[keyof SharedResource, string]> = [
  ['name', 'Name'],
  ['provider', 'Provider'],
  ['identifier', 'Identifier'],
  ['price', 'Price'],
  ['description', 'Description'],
];

/**
 * Read-only view of a resource a parent account shared with this subaccount
 * (capability `account_hierarchy`). It can be used here, never edited,
 * reshared or inspected for credentials.
 */
@customElement('shared-resource-view')
export class SharedResourceView extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    css`
      dl {
        display: grid;
        grid-template-columns: max-content 1fr;
        gap: var(--sl-spacing-x-small) var(--sl-spacing-large);
        margin: 0;
      }
      dt {
        color: var(--sl-color-neutral-600);
      }
      dd {
        margin: 0;
      }
    `,
  ];

  kind = '';
  resourceId = '';
  @state() private resource: SharedResource | null = null;
  @state() private status: 'loading' | 'ready' | 'not-found' | 'off' =
    'loading';

  onBeforeEnter(location: { params: { kind?: string; resourceId?: string } }) {
    this.kind = location.params.kind ?? '';
    this.resourceId = location.params.resourceId ?? '';
    void this.load();
  }

  async load() {
    this.status = 'loading';
    this.resource = null;
    try {
      const accountId = await currentAccountId();
      const resource = await getSharedResource(
        accountId,
        this.kind,
        this.resourceId
      );
      // The server answers for the id it was asked about, or not at all.
      if (resource?.id !== this.resourceId) {
        this.status = 'not-found';
        return;
      }
      this.resource = resource;
      this.status = 'ready';
    } catch (error) {
      // Any other failure also reads as not found: this view must never
      // guess at a resource it could not load.
      this.status = isCapabilityOff(error) ? 'off' : 'not-found';
    }
  }

  render() {
    if (this.status === 'off') return nothing;
    if (this.status === 'loading') return html`<sl-spinner></sl-spinner>`;
    if (this.status === 'not-found' || !this.resource) {
      return html`<view-header headerText="Not found"></view-header>
        <p class="empty-state" data-testid="shared-not-found">
          This resource does not exist or is not shared with this account.
        </p>`;
    }
    const resource = this.resource;
    return html`
      <view-header
        headerText=${resource.name}
        description=${`${KIND_LABELS[resource.kind] ?? resource.kind}, read only`}
      ></view-header>
      <sl-card>
        <div slot="header">${sharedFromBadge(resource)}</div>
        <dl data-testid="shared-fields">
          ${VISIBLE_FIELDS.map(([key, label]) => {
            const value = resource[key];
            return typeof value === 'string' && value
              ? html`<dt>${label}</dt>
                  <dd>${value}</dd>`
              : nothing;
          })}
        </dl>
        <p class="muted">
          Shared by ${resource.shared_from.account_name}. You can use it here,
          and add your own limits, but only the owning account can change it.
        </p>
      </sl-card>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'shared-resource-view': SharedResourceView;
  }
}
