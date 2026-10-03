import './repository-chip';
import { getApprovalRepository } from '../utils/approval-identity';
import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  readablePayload,
  toolSummary,
  type SessionTool,
} from '../utils/session-live';

@customElement('session-tool-card')
export class SessionToolCard extends LitElement {
  @property({ attribute: false }) tool!: SessionTool;
  @state() private expanded = false;
  static styles = css`
    :host {
      font-family: var(--sl-font-sans, system-ui, sans-serif);
      color: var(--sl-color-neutral-900);
      display: block;
      margin: 0.5rem 0;
    }
    details {
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: 0.5rem;
      padding: 0.7rem;
      background: var(--sl-color-neutral-50);
    }
    summary {
      cursor: pointer;
      overflow-wrap: anywhere;
    }
    summary:focus-visible {
      outline: 2px solid var(--sl-color-primary-500);
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      max-height: 30rem;
      overflow: auto;
      font-size: 0.8rem;
    }
    .preview {
      font-size: 0.8rem;
      color: var(--sl-color-neutral-600);
      margin: 0.4rem 0;
    }
  `;
  render() {
    const t = this.tool;
    if (!t) return html``;
    return html`<details
      @toggle=${(event: Event) => (this.expanded = (event.target as HTMLDetailsElement).open)}
    >
      <summary>
        ${t.name} ·
        ${t.phase === 'requested' ? 'Tool requested' : t.phase}${t.durationMs !== undefined ? ` · ${(t.durationMs / 1000).toFixed(1)}s` : ''}
        ${getApprovalRepository(t.repositoryArgs) ? html`<repository-chip .toolArgs=${t.repositoryArgs}></repository-chip>` : ''}
        <div class="preview">${toolSummary(t)}</div>
      </summary>
      ${t.redacted ? html`<p>Content redacted by capture policy.</p>` : ''}${t.truncated ? html`<p>Captured content is truncated.</p>` : ''}
      ${
        this.expanded
          ? html`<h4>Arguments</h4>
              <pre>${readablePayload(t.arguments)}</pre>
              <h4>Result</h4>
              <pre>${readablePayload(t.result)}</pre>`
          : ''
      }
    </details>`;
  }
}
