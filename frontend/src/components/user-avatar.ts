import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/avatar/avatar.js';

/**
 * Deterministic color from a string (user id or username).
 * Returns a CSS hsl() value.
 */
function deterministicColor(seed: string): string {
  let hash = 0;
  for (let i = 0; i < seed.length; i++) {
    hash = seed.charCodeAt(i) + ((hash << 5) - hash);
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 55%, 45%)`;
}

/**
 * Extract up to two initials from a display name or username.
 */
function getInitials(name: string): string {
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }
  return name.slice(0, 2).toUpperCase();
}

/**
 * <user-avatar> renders a profile image with an initials fallback.
 *
 * Uses sl-avatar internally. When `image` is set the provider/uploaded
 * picture is shown; otherwise deterministic-color initials derived from
 * `label` (full name or username) are displayed.
 */
@customElement('user-avatar')
export class UserAvatar extends LitElement {
  /** Avatar image URL or data URI. Omit for initials fallback. */
  @property({ type: String })
  image = '';

  /** Display name used for initials and the aria-label. */
  @property({ type: String })
  label = '';

  /** Stable seed for the deterministic background color (e.g. user id). */
  @property({ type: String })
  seed = '';

  /** Pixel size rendered. Maps to sl-avatar's --size custom property. */
  @property({ type: Number })
  size = 32;

  static styles = css`
    :host {
      display: inline-flex;
    }
    sl-avatar {
      --size: var(--user-avatar-size, 32px);
    }
    sl-avatar::part(base) {
      background-color: var(--avatar-bg);
    }
  `;

  render() {
    const initials = getInitials(this.label || this.seed || '?');
    const bg = deterministicColor(this.seed || this.label || 'default');
    const sizeVar = `${this.size}px`;
    return html`
      <sl-avatar
        style="--user-avatar-size: ${sizeVar}; --avatar-bg: ${bg}"
        .image=${this.image || ''}
        initials=${initials}
        label=${this.label || 'User avatar'}
      ></sl-avatar>
    `;
  }
}
