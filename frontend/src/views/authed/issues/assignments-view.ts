import { ConsoleStatus } from '../../../controllers/console-status';
import { LitElement, html, css } from 'lit';
import { customElement } from 'lit/decorators.js';

@customElement('assignments-view')
export class AssignmentsView extends LitElement {
  private readonly accessibilityStatus = new ConsoleStatus(this);
  static styles = css`
    :host {
      display: flex;
      justify-content: center;
      align-items: center;
      height: 100%;
      color: var(--lumo-contrast-60pct);
    }
    h1 {
      font-size: var(--lumo-font-size-xxl);
    }
  `;

  render() {
    return html` <h1>Coming Soon</h1> `;
  }
}
