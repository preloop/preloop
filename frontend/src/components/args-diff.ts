import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import { diffLines, toDiffLines, type DiffLine } from '../utils/line-diff';

/** One before/after pair pulled out of a tool call's arguments. */
export interface ArgsFileEdit {
  /** File the edit applies to, when the call names one. */
  path?: string;
  before: string;
  after: string;
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function pathOf(args: Record<string, unknown>): string | undefined {
  return (
    asString(args.file_path) ??
    asString(args.filePath) ??
    asString(args.path) ??
    asString(args.notebook_path)
  );
}

function pairOf(entry: Record<string, unknown>): ArgsFileEdit | null {
  const before = asString(entry.old_string) ?? asString(entry.oldText);
  const after = asString(entry.new_string) ?? asString(entry.newText);
  if (before === undefined || after === undefined) return null;
  if (before === after) return null;
  return { before, after };
}

/**
 * Read the file edits out of a tool call's arguments.
 *
 * Covers the three shapes agents actually send: a single `old_string` /
 * `new_string` replacement, a list of them under `edits`, and a whole-file
 * `content` write, which is a diff against nothing. Anything else has no
 * before and no after, so it has no diff and the caller shows the raw
 * arguments instead.
 */
export function fileEditsFromArgs(
  args: Record<string, unknown> | null | undefined
): ArgsFileEdit[] {
  if (!args || typeof args !== 'object') return [];
  const path = pathOf(args);

  const single = pairOf(args);
  if (single) return [{ ...single, path }];

  if (Array.isArray(args.edits)) {
    const edits = args.edits
      .filter(
        (entry): entry is Record<string, unknown> =>
          !!entry && typeof entry === 'object'
      )
      .map((entry) => pairOf(entry))
      .filter((edit): edit is ArgsFileEdit => edit !== null)
      .map((edit) => ({ ...edit, path }));
    if (edits.length > 0) return edits;
  }

  const content = asString(args.content);
  if (content !== undefined && path) {
    return [{ path, before: '', after: content }];
  }

  return [];
}

/**
 * The file edits in an approval, shown as a diff.
 *
 * An approval for an edit used to print the whole call as JSON, so deciding
 * whether the change was safe meant reading two long escaped strings side by
 * side and spotting the difference by eye. This shows what leaves and what
 * arrives, and keeps the exact payload one click away under Raw, because the
 * diff is a reading aid and the raw arguments are the thing being approved.
 */
@customElement('args-diff')
export class ArgsDiff extends LitElement {
  /** The tool call arguments, already stripped of approval metadata. */
  @property({ attribute: false })
  args: Record<string, unknown> | null = null;

  /** The formatted arguments to show when there is no diff, or under Raw. */
  @property({ type: String })
  raw = '';

  @state()
  private showRaw = false;

  static styles = css`
    :host {
      display: block;
    }

    .raw,
    .diff {
      background: var(--console-page);
      border-radius: 4px;
      font-family: var(--sl-font-mono, monospace);
      font-size: 0.875rem;
    }

    .raw {
      padding: 1rem;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--sl-spacing-small);
      margin-bottom: 0.5rem;
    }

    .path {
      font-family: var(--sl-font-mono, monospace);
      font-size: 0.8125rem;
      color: var(--console-meta-color);
      overflow-wrap: anywhere;
    }

    .diff {
      padding: 0.5rem 0;
      overflow: hidden;
    }

    .edit-label {
      padding: 0.25rem 1rem 0.5rem;
      font-family: var(--sl-font-sans);
      font-size: 0.8125rem;
      color: var(--console-meta-color);
    }

    .edit + .edit {
      margin-top: 0.5rem;
      border-top: 1px solid var(--console-hairline);
      padding-top: 0.5rem;
    }

    .line {
      display: flex;
      gap: 0.5rem;
      padding: 0 1rem;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .sign {
      flex: 0 0 auto;
      width: 0.75rem;
      color: var(--console-meta-color);
      user-select: none;
    }

    /* States are 16% tints with -800 ink, not solid paint (D27). */
    .line.removed {
      background: color-mix(
        in srgb,
        var(--sl-color-danger-500) 16%,
        transparent
      );
      color: var(--sl-color-danger-800);
    }

    .line.removed .sign {
      color: var(--sl-color-danger-800);
    }

    .line.added {
      background: color-mix(
        in srgb,
        var(--sl-color-success-500) 16%,
        transparent
      );
      color: var(--sl-color-success-800);
    }

    .line.added .sign {
      color: var(--sl-color-success-800);
    }

    .empty {
      padding: 0 1rem;
      font-family: var(--sl-font-sans);
      font-size: 0.8125rem;
      color: var(--console-meta-color);
    }
  `;

  /** The edits this call describes, if it describes any. */
  get edits(): ArgsFileEdit[] {
    return fileEditsFromArgs(this.args);
  }

  private toggleRaw() {
    this.showRaw = !this.showRaw;
  }

  private renderLine(line: DiffLine) {
    const sign =
      line.type === 'added' ? '+' : line.type === 'removed' ? '-' : ' ';
    return html`
      <div class="line ${line.type}">
        <span class="sign" aria-hidden="true">${sign}</span
        ><span class="text">${line.text || ' '}</span>
      </div>
    `;
  }

  private renderEdit(edit: ArgsFileEdit, index: number, total: number) {
    const lines = diffLines(
      edit.before === '' ? [] : toDiffLines(edit.before),
      edit.after === '' ? [] : toDiffLines(edit.after)
    );
    return html`
      <div class="edit">
        ${
          total > 1
            ? html`<div class="edit-label">Edit ${index + 1} of ${total}</div>`
            : nothing
        }
        ${
          lines.length === 0
            ? html`<div class="empty">No text changes.</div>`
            : lines.map((line) => this.renderLine(line))
        }
      </div>
    `;
  }

  render() {
    const edits = this.edits;
    if (edits.length === 0) {
      return html`<div class="raw" data-testid="args-raw">${this.raw}</div>`;
    }

    const path = edits.find((edit) => edit.path)?.path;
    return html`
      <div class="toolbar">
        <span class="path">${path ?? ''}</span>
        <sl-button
          size="small"
          variant="text"
          data-testid="raw-toggle"
          aria-pressed=${this.showRaw ? 'true' : 'false'}
          @click=${this.toggleRaw}
        >
          ${this.showRaw ? 'Diff' : 'Raw'}
        </sl-button>
      </div>
      ${
        this.showRaw
          ? html`<div class="raw" data-testid="args-raw">${this.raw}</div>`
          : html`<div class="diff" data-testid="args-diff">
              ${edits.map((edit, index) =>
                this.renderEdit(edit, index, edits.length)
              )}
            </div>`
      }
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'args-diff': ArgsDiff;
  }
}
