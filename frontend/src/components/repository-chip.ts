import { LitElement, css, html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import {
  formatApprovalRepository,
  getApprovalRepository,
} from '../utils/approval-identity';

/**
 * Which repository a native tool call ran in, as a compact chip.
 *
 * The hook resolves its own cwd against git and the backend stores the
 * observation as `_preloop_repository` next to `_preloop_source`; this reads
 * that marker. It is deliberately the only source: the working directory is
 * trusted, caller-supplied tool paths are not, so a repository named in the
 * arguments is never allowed to become the chip.
 *
 * The chip names the repository (`owner/repo`, host dropped, nested groups
 * kept) and the relative path when the call ran below the work-tree root.
 * A work tree with no `origin` reads "no remote". Without a marker the chip
 * renders nothing, like the attribution line it sits beside.
 */
@customElement('repository-chip')
export class RepositoryChip extends LitElement {
  /** The approval/session `tool_args` that may carry the marker. */
  @property({ type: Object })
  toolArgs: Record<string, unknown> | null = null;

  static styles = css`
    :host {
      display: inline-flex;
      min-width: 0;
    }

    .chip {
      align-items: baseline;
      background: var(--sl-color-neutral-100);
      border-radius: 999px;
      color: var(--sl-color-neutral-700);
      display: inline-flex;
      font-size: var(--sl-font-size-x-small);
      gap: 0.25rem;
      max-width: 100%;
      min-width: 0;
      padding: 1px 8px;
    }

    .remote {
      font-weight: 600;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .relative {
      color: var(--sl-color-neutral-600);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .separator {
      color: var(--sl-color-neutral-500);
    }

    .remote.is-empty {
      font-style: italic;
      font-weight: 400;
    }
  `;

  render() {
    const repository = getApprovalRepository(this.toolArgs);
    if (!repository) return nothing;

    const name = formatApprovalRepository(repository);
    const relative = repository.relative_path;
    const title = [
      name || 'No remote configured',
      relative ? `in ${relative}` : null,
      repository.toplevel,
    ]
      .filter(Boolean)
      .join(' · ');

    return html`
      <span class="chip" data-testid="repository-chip" title=${title}>
        <span class="remote ${name ? '' : 'is-empty'}"
          >${name || 'no remote'}</span
        >
        ${
          relative
            ? html`<span class="separator" aria-hidden="true">·</span>
                <span class="relative" data-testid="repository-relative"
                  >${relative}</span
                >`
            : nothing
        }
      </span>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'repository-chip': RepositoryChip;
  }
}
