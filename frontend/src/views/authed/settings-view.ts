import { LitElement, html, css } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { Router } from '@vaadin/router';
import { router } from '../../router';
import '@shoelace-style/shoelace/dist/components/tab-group/tab-group.js';
import '@shoelace-style/shoelace/dist/components/tab/tab.js';

@customElement('settings-view')
export class SettingsView extends LitElement {
  static styles = css`
    :host {
      display: block;
      padding: var(--lumo-space-l);
    }
    sl-tab-group {
      margin-bottom: var(--lumo-space-l);
    }
  `;

  @state()
  private location = router.location;

  private tabs = [
    { path: '/console/settings/profile', label: 'Profile' },
    { path: '/console/settings/security', label: 'Security' },
    { path: '/console/settings/subscription', label: 'Subscription' },
    { path: '/console/settings/api-keys', label: 'API Keys' },
    { path: '/console/settings/runners', label: 'Runners' },
    { path: '/console/ai-models', label: 'AI Models' },
    { path: '/console/settings/appearance', label: 'Appearance' },
  ];

  private onTabSelected(e: CustomEvent) {
    const tab = e.detail.tab;
    Router.go(tab.panel);
  }

  render() {
    return html`
      <sl-tab-group @sl-tab-select=${this.onTabSelected}>
        ${this.tabs.map(
          (tab) => html`<sl-tab panel=${tab.path}>${tab.label}</sl-tab>`
        )}
      </sl-tab-group>
      <slot></slot>
    `;
  }
}
