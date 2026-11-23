import '@shoelace-style/shoelace/dist/components/details/details.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  conversationRequest,
  conversationLabel,
  type FlowConversation,
} from '../utils/flow-conversations';

@customElement('flow-conversation-list')
export class FlowConversationList extends LitElement {
  @property({ type: String }) flowId = '';
  @state() private conversations: FlowConversation[] = [];
  @state() private error = '';
  @state() private more = false;
  static styles = css`
    :host {
      display: block;
      margin-block: var(--sl-spacing-medium);
    }
    ul {
      list-style: none;
      padding: 0;
    }
    li {
      display: flex;
      gap: var(--sl-spacing-medium);
      padding-block: var(--sl-spacing-small);
      border-bottom: 1px solid var(--console-hairline);
    }
    a {
      color: var(--sl-color-primary-600);
    }
    span,
    p {
      color: var(--sl-color-neutral-600);
    }
  `;
  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }
  private async load(): Promise<void> {
    try {
      const query = new URLSearchParams({
        offset: String(this.conversations.length),
      });
      if (this.flowId) query.set('flow_id', this.flowId);
      const page = await conversationRequest<FlowConversation[]>(
        `flow-conversations?${query}`
      );
      this.conversations = [...this.conversations, ...page];
      this.more = page.length === 50;
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : 'Could not load conversations.';
    }
  }
  render() {
    return html`<sl-details summary="Flow conversations">
      ${
        this.error
          ? html`<p role="alert">${this.error}</p>`
          : html`<ul>
              ${this.conversations.map((thread) => html`<li><a href=${`/console/flow-conversations/${thread.id}/talk`}>${thread.flow_name || 'Flow conversation'} · ${thread.id.slice(0, 8)}</a><span>${conversationLabel(thread)}</span></li>`)}
            </ul>`
      }
      ${!this.conversations.length && !this.error ? html`<p>No flow conversations yet.</p>` : ''}
      ${this.more ? html`<sl-button size="small" @click=${() => void this.load()}>Load more</sl-button>` : ''}
    </sl-details>`;
  }
}
