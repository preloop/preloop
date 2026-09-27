import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/tag/tag.js';

interface Tracker {
  id: string;
  name: string;
  type: string;
}

@customElement('tracker-pill')
export class TrackerPill extends LitElement {
  @property({ type: Object })
  tracker!: Tracker;

  static styles = css`
    sl-tag {
      display: inline-flex;
      align-items: center;
    }

    sl-icon {
      font-size: var(--sl-font-size-medium);
      margin-right: var(--sl-spacing-x-small);
    }
  `;

  private getTrackerIcon(tracker: Tracker): string {
    const type = tracker.type?.toLowerCase() || '';
    const name = tracker.name?.toLowerCase() || '';

    if (type.includes('jira') || name.includes('jira')) {
      return 'jira';
    }
    if (type.includes('github') || name.includes('github')) {
      return 'github';
    }
    if (type.includes('gitlab') || name.includes('gitlab')) {
      return 'gitlab';
    }
    if (type.includes('bitbucket')) {
      return 'bucket';
    }
    return 'box-seam';
  }

  render() {
    if (!this.tracker) {
      return html``;
    }

    const iconName = this.getTrackerIcon(this.tracker);

    return html`
      <sl-tag size="medium" pill>
        <sl-icon name="${iconName}"></sl-icon>
        ${this.tracker.name}
      </sl-tag>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'tracker-pill': TrackerPill;
  }
}
