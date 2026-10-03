import { LitElement, css, html, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import {
  ARTIFACTS_DOCS_HREF,
  ARTIFACT_KIND_GROUPS,
  ARTIFACT_KIND_ICONS,
  ARTIFACT_KIND_LABELS,
  TOOLS_PAGE_HREF,
  type ArtifactKindGroup,
} from '../utils/session-artifacts';

/**
 * Session header summary of artifacts: "Artifacts N" with one icon per kind
 * group present. Clicking a kind icon emits `artifact-kind-filter`
 * ({ kind }); the active filter shows as a removable chip that emits the
 * same event with `kind: null`. With no artifacts, the button opens a short
 * explanation of how agents save artifacts, with links to the docs and the
 * Tools page.
 */
@customElement('session-artifact-summary')
export class SessionArtifactSummary extends LitElement {
  @property({ attribute: false })
  counts: Partial<Record<ArtifactKindGroup, number>> = {};
  /** The count is a lower bound (the list was cut at its page cap). */
  @property({ type: Boolean }) truncated = false;
  @property({ type: String }) activeKind: ArtifactKindGroup | null = null;

  @state() private helpOpen = false;

  static styles = css`
    :host {
      display: block;
    }
    .bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.85rem;
      position: relative;
    }
    .title {
      font-weight: 600;
      color: var(--sl-color-neutral-700, #374151);
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
    }
    button {
      font: inherit;
      cursor: pointer;
    }
    .kind {
      display: inline-flex;
      align-items: center;
      gap: 0.2rem;
      padding: 0.15rem 0.45rem;
      border-radius: 999px;
      border: 1px solid var(--sl-color-neutral-300, #d1d5db);
      background: var(--sl-color-neutral-0, #fff);
      color: var(--sl-color-neutral-700, #374151);
      font-size: 0.8rem;
    }
    .kind[aria-pressed='true'] {
      border-color: var(--sl-color-primary-500, #3b82f6);
      background: var(--sl-color-primary-50, #eff6ff);
      color: var(--sl-color-primary-800, #1e40af);
    }
    .empty-trigger {
      border: 1px dashed var(--sl-color-neutral-300, #d1d5db);
      background: transparent;
      border-radius: 999px;
      padding: 0.15rem 0.55rem;
      color: var(--sl-color-neutral-600, #4b5563);
    }
    .popover {
      position: absolute;
      top: calc(100% + 0.35rem);
      left: 0;
      z-index: 20;
      max-width: 22rem;
      padding: 0.75rem;
      border-radius: 8px;
      border: 1px solid var(--sl-color-neutral-200, #e5e7eb);
      background: var(--sl-color-neutral-0, #fff);
      box-shadow: 0 6px 24px rgb(0 0 0 / 0.12);
      color: var(--sl-color-neutral-800, #1f2937);
      font-size: 0.85rem;
      line-height: 1.4;
    }
    .popover p {
      margin: 0 0 0.5rem;
    }
    .popover .links {
      display: flex;
      gap: 1rem;
    }
    a {
      color: var(--sl-color-primary-600, #2563eb);
    }
  `;

  private get total(): number {
    return Object.values(this.counts).reduce((sum, n) => sum + (n || 0), 0);
  }

  private choose(kind: ArtifactKindGroup | null): void {
    this.dispatchEvent(
      new CustomEvent('artifact-kind-filter', {
        detail: { kind },
        bubbles: true,
        composed: true,
      })
    );
  }

  private onKeydown = (event: KeyboardEvent): void => {
    if (event.key === 'Escape' && this.helpOpen) {
      this.helpOpen = false;
      this.renderRoot
        .querySelector<HTMLButtonElement>('.empty-trigger')
        ?.focus();
    }
  };

  render() {
    const total = this.total;
    if (total === 0) {
      return html`<div class="bar" @keydown=${this.onKeydown}>
        <button
          class="empty-trigger"
          data-testid="artifact-summary-empty"
          aria-expanded=${this.helpOpen ? 'true' : 'false'}
          aria-controls="artifact-help"
          @click=${() => (this.helpOpen = !this.helpOpen)}
        >
          <sl-icon name="paperclip"></sl-icon> Artifacts 0
        </button>
        ${
          this.helpOpen
            ? html`<div
                id="artifact-help"
                class="popover"
                role="dialog"
                aria-label="About artifacts"
                data-testid="artifact-empty-state"
              >
                <p>
                  No artifacts yet. Agents can save transcripts, screenshots and
                  files with the deposit_artifact tool or the API.
                </p>
                <div class="links">
                  <a
                    href=${ARTIFACTS_DOCS_HREF}
                    target="_blank"
                    rel="noopener"
                    data-testid="artifact-docs-link"
                    >Artifacts docs</a
                  >
                  <a href=${TOOLS_PAGE_HREF} data-testid="artifact-tools-link"
                    >Tools page</a
                  >
                </div>
              </div>`
            : nothing
        }
      </div>`;
    }
    return html`<div class="bar" data-testid="artifact-summary">
      <span class="title">
        <sl-icon name="paperclip"></sl-icon>
        Artifacts ${total}${this.truncated ? '+' : ''}
      </span>
      ${ARTIFACT_KIND_GROUPS.filter((kind) => (this.counts[kind] || 0) > 0).map(
        (kind) =>
          html`<button
            class="kind"
            data-kind=${kind}
            aria-pressed=${this.activeKind === kind ? 'true' : 'false'}
            title=${`Show only ${ARTIFACT_KIND_LABELS[kind].toLowerCase()}`}
            aria-label=${`${ARTIFACT_KIND_LABELS[kind]}: ${this.counts[kind]}`}
            @click=${() => this.choose(this.activeKind === kind ? null : kind)}
          >
            <sl-icon name=${ARTIFACT_KIND_ICONS[kind]}></sl-icon>
            ${this.counts[kind]}
          </button>`
      )}
      ${
        this.activeKind
          ? html`<sl-tag
              size="small"
              variant="primary"
              removable
              pill
              data-testid="artifact-filter-chip"
              @sl-remove=${() => this.choose(null)}
              >${ARTIFACT_KIND_LABELS[this.activeKind]} only</sl-tag
            >`
          : nothing
      }
    </div>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'session-artifact-summary': SessionArtifactSummary;
  }
}
