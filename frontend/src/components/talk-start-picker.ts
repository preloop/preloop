import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { TalkStartOptions } from '../api';

@customElement('talk-start-picker')
export class TalkStartPicker extends LitElement {
  @property({ attribute: false }) options: TalkStartOptions | null = null;
  @property() directory = '';
  @property() runtime = 'claude_code';

  static styles = css`
    :host {
      display: block;
      min-width: 0;
      margin-bottom: 0.75rem;
    }
    fieldset {
      min-width: 0;
      margin: 0;
      padding: 0;
      border: 0;
    }
    legend {
      font-weight: 600;
      margin-bottom: 0.5rem;
    }
    button {
      display: block;
      width: 100%;
      padding: 0.65rem;
      text-align: left;
      background: transparent;
      color: inherit;
      border: 1px solid var(--sl-color-neutral-300);
      border-radius: 0.4rem;
      margin-bottom: 0.4rem;
      cursor: pointer;
      overflow-wrap: anywhere;
    }
    .choices {
      max-height: min(32dvh, 18rem);
      overflow-y: auto;
    }
    small {
      display: block;
    }
    input {
      display: block;
      box-sizing: border-box;
      width: 100%;
      min-width: 0;
      padding: 0.65rem;
      font: inherit;
    }
    label,
    p {
      display: block;
      font-size: 0.875rem;
      margin: 0.5rem 0;
    }
    code {
      overflow-wrap: anywhere;
    }
  `;

  private select(directory: string): void {
    this.dispatchEvent(
      new CustomEvent('directory-selected', {
        detail: directory,
        bubbles: true,
        composed: true,
      })
    );
  }

  render() {
    const missing =
      this.options &&
      (!this.options.capabilities.start_directory ||
        !this.options.capabilities.command_session);
    return html` <fieldset>
      <legend>Start in</legend>
      ${missing ? html`<p role="alert">Update the connected sidecar (${this.options?.version || 'version not reported'}) to support working directories and session binding. Run <code>preloop update</code> and <code>npm install -g @preloop-ai/${this.runtime === 'codex' ? 'codex-plugin@0.1.1' : 'claude-plugin@0.1.2'}</code>, then restart the sidecar.</p>` : nothing}
      <div class="choices">
        ${
          this.options?.projects.length
            ? html`<p>Projects on this host</p>
                ${this.options.projects.map((project) => html`<button type="button" @click=${() => this.select(project.path)}>${project.name} <small>${project.path}</small></button>`)}`
            : nothing
        }
        ${
          this.options?.recent_directories.length
            ? html`<p>Recent directories</p>
                ${this.options.recent_directories.map((directory) => html`<button type="button" @click=${() => this.select(directory)}>${directory}</button>`)}`
            : nothing
        }
      </div>
      <label
        >Working directory<input
          aria-label="Working directory"
          .value=${this.directory}
          placeholder="~/projects/example"
          @input=${(event: Event) => this.select((event.target as HTMLInputElement).value)}
      /></label>
    </fieldset>`;
  }
}
