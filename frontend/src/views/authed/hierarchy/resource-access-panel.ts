import { LitElement, html, css, nothing, type PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/card/card.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/radio/radio.js';
import '@shoelace-style/shoelace/dist/components/radio-group/radio-group.js';
import '@shoelace-style/shoelace/dist/components/switch/switch.js';
import '@shoelace-style/shoelace/dist/components/tag/tag.js';
import {
  NO_CAPABILITIES,
  isCapabilityOff,
  type CapabilitySet,
} from '../../../capabilities';
import {
  createShare,
  currentAccountId,
  deleteShare,
  getTags,
  isConflict,
  listShares,
  listSubaccounts,
  setTags,
  type Share,
  type ShareTarget,
  type ShareableKind,
  type Subaccount,
  type Tags,
} from '../../../hierarchy-api';
import { parseTags } from './tags';
import { confirmDialog } from '../../../components/confirm-dialog';

const SHAREABLE: readonly ShareableKind[] = [
  'ai_model',
  'mcp_server',
  'managed_agent',
  'flow',
  'runner_pool',
  'policy',
];

type Section = 'loading' | 'on' | 'off';

/**
 * Whether two share targets reach the same subaccounts. Selected targets
 * compare as id sets, since names need not be unique and order is not
 * meaningful.
 */
function sameTarget(a: ShareTarget, b: ShareTarget): boolean {
  if (a.type === 'all' || b.type === 'all') return a.type === b.type;
  if (a.type === 'tag' || b.type === 'tag') {
    return (
      a.type === 'tag' &&
      b.type === 'tag' &&
      a.key === b.key &&
      a.value === b.value
    );
  }
  const x = new Set(a.subaccount_ids);
  const y = new Set(b.subaccount_ids);
  return x.size === y.size && [...x].every((id) => y.has(id));
}

/**
 * Sharing and tags for one resource, mounted on its detail page through
 * `<capability-extension name="resource-access">`.
 *
 * Context: `kind` (resource type), `resourceId`, and `sharedFrom` when the
 * resource belongs to a parent (then nothing here is editable and sharing is
 * not offered). Each section hides on its own when its endpoint is missing,
 * and the panel reports `capability-off` when both are.
 */
@customElement('resource-access-panel')
export class ResourceAccessPanel extends LitElement {
  static styles = css`
    :host {
      display: block;
      margin-top: var(--sl-spacing-medium);
    }
    section + section {
      margin-top: var(--sl-spacing-medium);
      border-top: 1px solid var(--sl-color-neutral-200);
      padding-top: var(--sl-spacing-medium);
    }
    h3 {
      font-size: var(--sl-font-size-medium);
      margin: 0 0 var(--sl-spacing-x-small);
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: var(--sl-spacing-x-small);
      align-items: center;
      margin: var(--sl-spacing-x-small) 0;
    }
    .muted {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
    }
    .error {
      color: var(--sl-color-danger-700);
    }
  `;

  @property({ attribute: false }) context: Record<string, unknown> = {};
  @property({ attribute: false }) capabilities: CapabilitySet = NO_CAPABILITIES;

  @state() private shareState: Section = 'off';
  @state() private tagState: Section = 'off';
  @state() private shares: Share[] = [];
  @state() private subaccounts: Subaccount[] = [];
  @state() private tags: Tags = {};
  @state() private governed: string[] = [];
  private tagsVersion: string | null = null;
  @state() private draftShared = false;
  @state() private draftTarget: ShareTarget['type'] = 'all';
  @state() private draftSelected = new Set<string>();
  @state() private draftTag = '';
  @state() private error = '';
  @state() private saving = false;

  private accountId = '';
  private generation = 0;

  private get kind(): string {
    return String(this.context.kind ?? '');
  }
  private get resourceId(): string {
    return String(this.context.resourceId ?? '');
  }
  private get readOnly(): boolean {
    return Boolean(this.context.sharedFrom);
  }

  protected willUpdate(changed: PropertyValues) {
    if (changed.has('context') || changed.has('capabilities')) void this.load();
  }

  private async load() {
    const generation = ++this.generation;
    const id = this.resourceId;
    if (!id || !this.kind) return;
    const wantShares =
      this.capabilities.has('account_hierarchy') &&
      !this.readOnly &&
      SHAREABLE.includes(this.kind as ShareableKind);
    const wantTags = this.capabilities.has('abac_rules');
    this.shareState = wantShares ? 'loading' : 'off';
    this.tagState = wantTags ? 'loading' : 'off';
    this.error = '';
    try {
      this.accountId = await currentAccountId();
    } catch {
      this.shareState = this.tagState = 'off';
      return;
    }
    await Promise.all([
      wantShares ? this.loadShares(generation, id) : null,
      wantTags ? this.loadTags(generation, id) : null,
    ]);
    if (
      generation === this.generation &&
      this.shareState === 'off' &&
      this.tagState === 'off'
    ) {
      this.dispatchEvent(new CustomEvent('capability-off', { bubbles: true }));
    }
  }

  private async loadShares(generation: number, id: string) {
    try {
      const [shares, subaccounts] = await Promise.all([
        listShares(this.accountId, this.kind as ShareableKind, id),
        listSubaccounts(this.accountId),
      ]);
      if (generation !== this.generation) return;
      // Only shares of this resource, whatever the server sends.
      this.shares = shares.filter(
        (s) => s.resource_id === id && s.resource_type === this.kind
      );
      this.subaccounts = subaccounts;
      this.resetShareDraft();
      this.shareState = 'on';
    } catch {
      if (generation === this.generation) this.shareState = 'off';
    }
  }

  private async loadTags(generation: number, id: string) {
    try {
      const result = await getTags(this.kind, id);
      if (generation !== this.generation) return;
      this.tags = result.tags;
      this.governed = result.governed_keys ?? [];
      this.tagsVersion = result.version ?? null;
      this.tagState = 'on';
    } catch {
      // Missing endpoint, or an id this account cannot see: show nothing.
      if (generation === this.generation) this.tagState = 'off';
    }
  }

  private resetShareDraft() {
    this.draftShared = this.shares.length > 0;
    this.draftTarget = 'all';
    this.draftSelected = new Set();
    this.draftTag = '';
  }

  private buildTarget(): ShareTarget | string {
    if (this.draftTarget === 'all') return { type: 'all' };
    if (this.draftTarget === 'selected') {
      if (this.draftSelected.size === 0)
        return 'Choose at least one subaccount.';
      return { type: 'selected', subaccount_ids: [...this.draftSelected] };
    }
    const { tags, errors } = parseTags(this.draftTag);
    const entries = Object.entries(tags);
    if (errors.length || entries.length !== 1) {
      return 'Enter one tag as key=value.';
    }
    const [key, value] = entries[0];
    return { type: 'tag', key, value };
  }

  /**
   * Runs one share change, then reads the shares back from the server
   * whether it worked or not, so the list never shows a share that is gone
   * or hides one that exists.
   */
  private async changeShares(change: () => Promise<void>) {
    this.error = '';
    this.saving = true;
    try {
      await change();
    } catch (error) {
      if (isCapabilityOff(error)) this.shareState = 'off';
      else this.error = error instanceof Error ? error.message : 'Failed';
    } finally {
      this.saving = false;
    }
    if (this.shareState !== 'off') {
      const error = this.error;
      await this.loadShares(this.generation, this.resourceId);
      this.error = error;
    }
  }

  /**
   * Adds a share. Existing shares are left alone, and a target that is
   * already shared is refused rather than posted twice.
   */
  private addShare = async () => {
    const target = this.buildTarget();
    if (typeof target === 'string') {
      this.error = target;
      return;
    }
    if (this.shares.some((share) => sameTarget(share.target, target))) {
      this.error = `Already shared with ${this.targetLabel(target)}.`;
      return;
    }
    await this.changeShares(async () => {
      await createShare(this.accountId, {
        resource_type: this.kind as ShareableKind,
        resource_id: this.resourceId,
        target,
      });
    });
  };

  private async removeShare(share: Share) {
    const ok = await confirmDialog({
      title: 'Stop sharing?',
      message: `Stop sharing this resource with ${this.targetLabel(share.target)}?`,
      detail: 'Those subaccounts lose access to it right away.',
      confirmLabel: 'Stop sharing',
      variant: 'danger',
    });
    if (!ok) return;
    return this.changeShares(() => deleteShare(this.accountId, share.id));
  }

  /** Stops every share listed, and only those. */
  private stopSharing = async () => {
    const listed = [...this.shares];
    const ok = await confirmDialog({
      title: 'Stop sharing?',
      message: `Stop all ${listed.length} share${listed.length === 1 ? '' : 's'} of this resource?`,
      detail: listed.map((share) => this.targetLabel(share.target)).join('\n'),
      confirmLabel: 'Stop sharing',
      variant: 'danger',
    });
    if (!ok) return;
    await this.changeShares(async () => {
      for (const share of listed) await deleteShare(this.accountId, share.id);
    });
  };

  private targetLabel(target: ShareTarget): string {
    if (target.type === 'all') return 'All subaccounts';
    if (target.type === 'tag')
      return `Subaccounts tagged ${target.key}=${target.value}`;
    const names = target.subaccount_ids.map(
      (id) => this.subaccounts.find((sub) => sub.id === id)?.name ?? id
    );
    return names.join(', ') || 'No subaccounts';
  }

  private async writeTags(next: Tags) {
    this.error = '';
    try {
      const result = await setTags(
        this.kind,
        this.resourceId,
        next,
        this.tagsVersion
      );
      this.tags = result.tags;
      this.governed = result.governed_keys ?? this.governed;
      this.tagsVersion = result.version ?? null;
    } catch (error) {
      if (isCapabilityOff(error)) this.tagState = 'off';
      else if (isConflict(error)) {
        // Someone else changed the tags first: show theirs, keep nothing.
        await this.loadTags(this.generation, this.resourceId);
        this.error =
          'The tags changed while you were editing. They have been reloaded; make your change again.';
      } else this.error = error instanceof Error ? error.message : 'Failed';
    }
  }

  private addTag = () => {
    const input = this.renderRoot.querySelector<HTMLInputElement>('#new-tag');
    const { tags, errors } = parseTags(input?.value ?? '');
    if (errors.length) {
      this.error = errors.join('. ');
      return;
    }
    const blocked = Object.keys(tags).filter((k) => this.governed.includes(k));
    if (blocked.length) {
      this.error = `Only the parent account sets ${blocked.join(', ')}.`;
      return;
    }
    if (input) input.value = '';
    void this.writeTags({ ...this.tags, ...tags });
  };

  private removeTag(key: string) {
    const next = { ...this.tags };
    delete next[key];
    void this.writeTags(next);
  }

  private renderShare() {
    const pickTarget = (e: Event) =>
      (this.draftTarget = (e.target as HTMLInputElement)
        .value as ShareTarget['type']);
    return html`<section data-testid="share-section">
      <h3>Sharing</h3>
      ${
        this.shares.length
          ? html`<ul data-testid="share-list">
              ${this.shares.map(
                (share) =>
                  html`<li data-share=${share.id}>
                    ${this.targetLabel(share.target)}
                    <sl-button
                      size="small"
                      variant="text"
                      ?disabled=${this.saving}
                      @click=${() => this.removeShare(share)}
                      >Stop</sl-button
                    >
                  </li>`
              )}
            </ul>`
          : nothing
      }
      <sl-switch
        data-testid="share-toggle"
        ?checked=${this.draftShared}
        @sl-change=${(e: Event) =>
          (this.draftShared = (e.target as HTMLInputElement).checked)}
        >Share with subaccounts</sl-switch
      >
      ${
        this.draftShared
          ? html`<sl-radio-group
                size="small"
                label=${this.shares.length ? 'Also share with' : 'With'}
                data-testid="share-target"
                .value=${this.draftTarget}
                @sl-change=${pickTarget}
              >
                <sl-radio value="all">All subaccounts</sl-radio>
                <sl-radio value="selected">Selected subaccounts</sl-radio>
                <sl-radio value="tag">Subaccounts with a tag</sl-radio>
              </sl-radio-group>
              ${
                this.draftTarget === 'selected'
                  ? html`<div class="row">
                      ${this.subaccounts.map(
                        (sub) =>
                          html`<sl-checkbox
                            size="small"
                            data-subaccount=${sub.id}
                            ?checked=${this.draftSelected.has(sub.id)}
                            @sl-change=${(e: Event) => {
                              const next = new Set(this.draftSelected);
                              if ((e.target as HTMLInputElement).checked)
                                next.add(sub.id);
                              else next.delete(sub.id);
                              this.draftSelected = next;
                            }}
                            >${sub.name}</sl-checkbox
                          >`
                      )}
                    </div>`
                  : nothing
              }
              ${
                this.draftTarget === 'tag'
                  ? html`<sl-input
                      size="small"
                      data-testid="share-tag"
                      placeholder="customer=acme"
                      .value=${this.draftTag}
                      @sl-input=${(e: Event) =>
                        (this.draftTag = (e.target as HTMLInputElement).value)}
                    ></sl-input>`
                  : nothing
              }
              <div class="row">
                <sl-button
                  size="small"
                  variant="primary"
                  data-testid="share-save"
                  ?loading=${this.saving}
                  @click=${this.addShare}
                  >${this.shares.length ? 'Add share' : 'Share'}</sl-button
                >
              </div>`
          : this.shares.length
            ? html`<div class="row">
                <sl-button
                  size="small"
                  variant="danger"
                  outline
                  data-testid="share-stop"
                  ?loading=${this.saving}
                  @click=${this.stopSharing}
                  >Stop all ${this.shares.length} shown</sl-button
                >
              </div>`
            : nothing
      }
    </section>`;
  }

  private renderTags() {
    const entries = Object.entries(this.tags);
    return html`<section data-testid="tag-section">
      <h3>Tags</h3>
      <div class="row">
        ${
          entries.length === 0
            ? html`<span class="muted">No tags.</span>`
            : entries.map(([key, value]) => {
                const locked = this.readOnly || this.governed.includes(key);
                return html`<sl-tag
                  size="small"
                  data-key=${key}
                  ?removable=${!locked}
                  @sl-remove=${() => this.removeTag(key)}
                  >${key}=${value}${
                    locked && !this.readOnly ? ' (set by parent)' : ''
                  }</sl-tag
                >`;
              })
        }
      </div>
      ${
        this.readOnly
          ? nothing
          : html`<div class="row">
              <sl-input
                id="new-tag"
                size="small"
                placeholder="key=value"
              ></sl-input>
              <sl-button
                size="small"
                data-testid="tag-add"
                @click=${this.addTag}
                >Add tag</sl-button
              >
            </div>`
      }
    </section>`;
  }

  render() {
    const share = this.shareState === 'on';
    const tags = this.tagState === 'on';
    if (!share && !tags) return nothing;
    return html`<sl-card>
      ${share ? this.renderShare() : nothing}
      ${tags ? this.renderTags() : nothing}
      ${
        this.error
          ? html`<p class="error" role="alert">${this.error}</p>`
          : nothing
      }
    </sl-card>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'resource-access-panel': ResourceAccessPanel;
  }
}
