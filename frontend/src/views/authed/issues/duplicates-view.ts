import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { getDuplicateIssues } from '../../../api';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/menu/menu.js';
import '@shoelace-style/shoelace/dist/components/menu-item/menu-item.js';

@customElement('duplicates-view')
export class DuplicatesView extends LitElement {
  static styles = css`
    :host {
      display: block;
    }

    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: var(--sl-spacing-large);
    }

    .container {
      max-width: var(--console-container-max-width);
      padding: var(--sl-spacing-x-large);
    }
  `;

  @state()
  private issues: any[] = [];

  async connectedCallback() {
    super.connectedCallback();
    this.issues = await getDuplicateIssues();
  }

  render() {
    return html`
      <div class="container">
        <div class="header">
          <h1 class="title">Issues Dashboard</h1>
        </div>
        <div class="container">
          ${
            this.issues.length > 0
              ? html`
                  <sl-menu>
                    ${this.issues.map(
                      (issue) =>
                        html`<sl-menu-item>${issue.title}</sl-menu-item>`
                    )}
                  </sl-menu>
                `
              : html`
                  <sl-alert variant="primary" open>
                    <sl-icon slot="icon" name="info-circle"></sl-icon>
                    No duplicate issues found.
                  </sl-alert>
                `
          }
        </div>
      </div>
    `;
  }
}
