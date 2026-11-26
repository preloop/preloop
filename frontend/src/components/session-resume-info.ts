import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import type { RuntimeSessionSummary } from '../types';

/** Native conversation identity, shared by Talk and the session header. */
@customElement('session-resume-info')
export class SessionResumeInfo extends LitElement {
  @property({ attribute: false }) session: RuntimeSessionSummary | null = null;

  static styles = css`
    :host {
      display: block;
      font-size: var(--console-text-meta);
      color: var(--console-meta-color);
    }
    .identity {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
      padding: var(--sl-spacing-x-small) var(--sl-spacing-medium);
      border-bottom: 1px solid var(--console-hairline);
    }
    code {
      overflow-wrap: anywhere;
      color: var(--console-body-color, inherit);
    }
    .directory {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 100%;
    }
  `;

  render() {
    const session = this.session;
    if (!session?.harness_session_id) return nothing;
    return html`<div class="identity">
      <span>Harness session</span>
      <code title=${session.harness_session_id}
        >${session.harness_session_id.slice(0, 8)}</code
      >
      <sl-copy-button
        value=${session.harness_session_id}
        copy-label="Copy harness session ID"
      ></sl-copy-button>
      ${session.start_directory ? html`<span class="directory" title=${session.start_directory}>${session.start_directory}</span>` : nothing}
      ${
        session.resume_command
          ? html`<details>
              <summary>Resume locally</summary>
              <code>${session.resume_command}</code
              ><sl-copy-button
                value=${session.resume_command}
                copy-label="Copy resume command"
              ></sl-copy-button>
            </details>`
          : html`<span>Start directory unavailable</span>`
      }
    </div>`;
  }
}
