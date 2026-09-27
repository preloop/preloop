import { LitElement, html, nothing, type PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  loadCapabilities,
  type Capability,
  type CapabilitySet,
} from '../capabilities';

/** What a mounted extension element receives. */
export interface ExtensionElement extends HTMLElement {
  context?: Record<string, unknown>;
  capabilities?: CapabilitySet;
}

interface ExtensionEntry {
  /** Mounted when any of these is reported. */
  capabilities: readonly Capability[];
  tag: string;
  load: () => Promise<unknown>;
}

/**
 * Pieces of existing pages that only a capability turns on. The modules are
 * fetched the first time a page asks for one and the capability is present;
 * without it the page gets an empty element and nothing is downloaded.
 */
export const CAPABILITY_EXTENSIONS: Record<string, ExtensionEntry> = {
  'account-switcher': {
    capabilities: ['multi_account'],
    tag: 'account-switcher',
    load: () => import('../views/authed/hierarchy/account-switcher'),
  },
  'resource-access': {
    capabilities: ['account_hierarchy', 'abac_rules'],
    tag: 'resource-access-panel',
    load: () => import('../views/authed/hierarchy/resource-access-panel'),
  },
  'runner-pools': {
    capabilities: ['account_hierarchy', 'abac_rules'],
    tag: 'runner-pool-access',
    load: () => import('../views/authed/hierarchy/runner-pool-access'),
  },
  'usage-rollup': {
    capabilities: ['account_hierarchy'],
    tag: 'usage-rollup-panel',
    load: () => import('../views/authed/hierarchy/usage-rollup-panel'),
  },
  'access-rules': {
    capabilities: ['abac_rules'],
    tag: 'access-rules-panel',
    load: () => import('../views/authed/hierarchy/access-rules-panel'),
  },
};

export function contextChanged(
  next: Record<string, unknown> | undefined,
  previous: Record<string, unknown> | undefined
): boolean {
  if (next === previous) return false;
  if (!next || !previous) return true;
  const keys = new Set([...Object.keys(next), ...Object.keys(previous)]);
  for (const key of keys) {
    if (next[key] !== previous[key]) return true;
  }
  return false;
}

/**
 * `<capability-extension name="resource-access" .context=${...}>` mounts the
 * named extension when `/features` reports one of its capabilities, and
 * renders nothing otherwise. The extension hides itself (event
 * `capability-off`) when its endpoint answers 404, so a missing plugin never
 * surfaces as an error.
 */
@customElement('capability-extension')
export class CapabilityExtension extends LitElement {
  @property() name = '';
  /**
   * Handed to the extension. Pages pass a fresh object literal on every
   * render, so only a change in one of its values counts as a change.
   */
  @property({ attribute: false, hasChanged: contextChanged })
  context: Record<string, unknown> = {};

  /** Tests pass a capability set instead of reading `/features`. */
  @property({ attribute: false }) capabilities: CapabilitySet | null = null;

  @state() private _mounted: ExtensionElement | null = null;
  @state() private _off = false;

  private _generation = 0;

  protected createRenderRoot() {
    // Light DOM: the extension belongs to the page's layout and styles.
    return this;
  }

  protected updated(changed: PropertyValues) {
    if (changed.has('name') || changed.has('capabilities')) {
      void this.mount();
    } else if (changed.has('context') && this._mounted) {
      this._mounted.context = this.context;
    }
  }

  private async mount() {
    const generation = ++this._generation;
    const entry = CAPABILITY_EXTENSIONS[this.name];
    if (!entry) return;
    const capabilities = this.capabilities ?? (await loadCapabilities());
    if (generation !== this._generation) return;
    if (!entry.capabilities.some((name) => capabilities.has(name))) {
      this._mounted = null;
      return;
    }
    await entry.load();
    if (generation !== this._generation) return;
    const element = document.createElement(entry.tag) as ExtensionElement;
    element.context = this.context;
    element.capabilities = capabilities;
    element.addEventListener('capability-off', () => {
      this._off = true;
    });
    this._off = false;
    this._mounted = element;
  }

  render() {
    return this._mounted && !this._off ? html`${this._mounted}` : nothing;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'capability-extension': CapabilityExtension;
  }
}
