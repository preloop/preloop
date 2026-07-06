import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';

@customElement('icon-selector')
export class IconSelector extends LitElement {
  static styles = css`
    .icon-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(48px, 1fr));
      gap: 8px;
    }
    .icon-wrapper {
      display: flex;
      justify-content: center;
      align-items: center;
      width: 48px;
      height: 48px;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      cursor: pointer;
    }
    .icon-wrapper.selected {
      border-color: var(--sl-color-primary-500);
    }
  `;

  @property()
  selectedIcon = 'gear';

  private icons = [
    'gear',
    'file-earmark-text',
    'tag',
    'bug',
    'lightbulb',
    'rocket',
  ];

  render() {
    return html`
      <div class="icon-grid">
        ${this.icons.map(
          (icon) => html`
            <div
              class="icon-wrapper ${
                this.selectedIcon === icon ? 'selected' : ''
              }"
              @click=${() => this.selectIcon(icon)}
            >
              <sl-icon name=${icon} style="font-size: 24px;"></sl-icon>
            </div>
          `
        )}
        <div class="icon-wrapper" @click=${() => this.uploadIcon()}>
          <sl-icon name="upload" style="font-size: 24px;"></sl-icon>
        </div>
      </div>
    `;
  }

  selectIcon(icon: string) {
    this.selectedIcon = icon;
    this.dispatchEvent(new CustomEvent('icon-change', { detail: { icon } }));
  }

  uploadIcon() {
    // TODO: Implement file upload
    alert('File upload not yet implemented');
  }
}
