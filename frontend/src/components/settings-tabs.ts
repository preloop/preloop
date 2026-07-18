import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';

@customElement('settings-tabs')
export class SettingsTabs extends LitElement {
  @property({ type: String })
  active = '';

  @property({ type: Object })
  features: { [key: string]: boolean | string[] } = {};

  static styles = css`
    :host {
      display: block;
      margin-bottom: 2rem;
    }
    sl-tab-group {
      --indicator-color: var(--sl-color-primary-600);
    }
  `;

  private get tabs() {
    const hasUserMgmt = this.features['user_management'] === true;
    const allTabs = [
      { path: '/console/settings/profile', label: 'Profile' },
      { path: '/console/settings/security', label: 'Security' },
      { path: '/console/settings/account', label: 'Account', ee: true },
      { path: '/console/settings/users', label: 'Users', ee: true },
      { path: '/console/settings/teams', label: 'Teams', ee: true },
      { path: '/console/settings/invitations', label: 'Invitations', ee: true },
      { path: '/console/settings/api-keys', label: 'API Keys' },
      { path: '/console/ai-models', label: 'AI Models' },
      { path: '/console/settings/appearance', label: 'Appearance' },
    ];
    return allTabs.filter((t) => !t.ee || hasUserMgmt);
  }

  private onTabSelect(e: CustomEvent) {
    const panel = e.detail.name;
    if (panel) {
      Router.go(panel);
    }
  }

  render() {
    return html`
      <sl-tab-group @sl-tab-show=${this.onTabSelect}>
        ${this.tabs.map(
          (tab) => html`
            <sl-tab
              slot="nav"
              panel=${tab.path}
              ?active=${this.active === tab.path}
            >
              ${tab.label}
            </sl-tab>
          `
        )}
      </sl-tab-group>
    `;
  }
}
