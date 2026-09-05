import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import './ide-setup-tabs';
import { consoleDialogStyles } from '../styles/console-dialog';

@customElement('mcp-setup-dialog')
export class MCPSetupDialog extends LitElement {
  static styles = [
    consoleDialogStyles,
    css`
      sl-dialog::part(panel) {
        background: transparent;
        box-shadow: none;
      }

      sl-dialog::part(body) {
        padding: 0;
      }

      sl-dialog::part(overlay) {
        backdrop-filter: blur(4px);
      }
    `,
  ];

  @property({ type: Boolean })
  open = false;

  render() {
    return html`
      <sl-dialog
        ?open=${this.open}
        @sl-hide=${() => this._handleClose()}
        style="--width: 65rem;"
      >
        <ide-setup-tabs
          .configs=${[
            {
              ide: 'cli',
              ide_name: 'Preloop CLI',
              logo_path: '/assets/preloop-badge.png',
              logo_width: '32',
              prerequisites: [],
              setup_instructions:
                'Install the CLI to onboard existing agents or connect them manually.',
              code:
                window.location.hostname === 'preloop.ai'
                  ? 'curl -fsSL https://preloop.ai/install/cli | sh\n\npreloop login\n\npreloop agents discover'
                  : `curl -fsSL https://preloop.ai/install/cli | sh\n\nexport PRELOOP_URL=${window.location.origin}\npreloop login\n\npreloop agents discover`,
            },
          ]}
          defaultTab="cli"
          helpText="The Preloop CLI manages your local settings and agent discovery."
        ></ide-setup-tabs>
      </sl-dialog>
    `;
  }

  private _handleClose() {
    this.dispatchEvent(new CustomEvent('close'));
  }
}
