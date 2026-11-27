import { LitElement, css, html, nothing, unsafeCSS } from 'lit';
import type { PropertyValues, TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import {
  getExtensionContent,
  type ExtensionContent,
  type ExtensionContentFile,
  type ExtensionContentFileStatus,
  type ExtensionContentHook,
} from '../extension-content-api';
import type { ExtensionKey } from '../extension-inventory-api';
import { diffLines, toDiffLines, type DiffLine } from '../utils/line-diff';
import {
  markdownBodyCss,
  renderUntrustedMarkdownChecked,
} from '../utils/markdown';

/** Why a listed file has no text. Shown beside the file, never hidden. */
export const WITHHELD_FILE_TEXT: Record<
  Exclude<ExtensionContentFileStatus, 'stored'>,
  string
> = {
  binary: 'Binary file. Binaries are never uploaded.',
  too_large: 'Over 256 KiB. Not uploaded.',
  symlink: 'Symlink. Links are not followed, so its target was not read.',
  private_key: 'Contains a private key block. Such a file is never uploaded.',
  over_budget: 'Not uploaded: this extension reached its 1 MiB text cap.',
  not_selected:
    'Listed only. For a plugin, text is collected for its manifest, skills, commands and hook definitions.',
  unreadable: 'Changed or could not be read while the device collected it.',
};

const shortHash = (value: string): string => `${value.slice(0, 19)}…`;
const isMarkdown = (path: string): boolean => /\.(md|markdown)$/i.test(path);
const total = (counts: Record<string, number>): number =>
  Object.values(counts).reduce((sum, count) => sum + count, 0);

/** Split leading YAML front matter from a Markdown body. */
export function splitFrontMatter(text: string): {
  front: string;
  body: string;
} {
  const normalized = text.replace(/\r\n/g, '\n');
  if (!normalized.startsWith('---\n')) return { front: '', body: text };
  const end = normalized.indexOf('\n---', 4);
  if (end < 0) return { front: '', body: text };
  const rest = normalized.slice(end + 4);
  return {
    front: normalized.slice(4, end),
    body: rest.startsWith('\n') ? rest.slice(1) : rest,
  };
}

type HunkLine = DiffLine | { type: 'gap'; text: string };

/** Keep changed lines and a few around each; collapse the rest to a count. */
export function diffHunks(lines: DiffLine[], context = 3): HunkLine[] {
  const keep = new Array<boolean>(lines.length).fill(false);
  lines.forEach((line, index) => {
    if (line.type === 'context') return;
    const last = Math.min(lines.length - 1, index + context);
    for (let k = Math.max(0, index - context); k <= last; k++) keep[k] = true;
  });
  const result: HunkLine[] = [];
  let skipped = 0;
  lines.forEach((line, index) => {
    if (!keep[index]) {
      skipped++;
      return;
    }
    if (skipped)
      result.push({ type: 'gap', text: `${skipped} unchanged lines` });
    skipped = 0;
    result.push(line);
  });
  if (skipped && result.length)
    result.push({ type: 'gap', text: `${skipped} unchanged lines` });
  return result;
}

interface TreeNode {
  name: string;
  file?: ExtensionContentFile;
  children: Map<string, TreeNode>;
}

function buildTree(files: ExtensionContentFile[]): TreeNode {
  const root: TreeNode = { name: '', children: new Map() };
  for (const file of files) {
    let node = root;
    const parts = file.path.split('/');
    parts.forEach((part, index) => {
      let child = node.children.get(part);
      if (!child) {
        child = { name: part, children: new Map() };
        node.children.set(part, child);
      }
      if (index === parts.length - 1) child.file = file;
      node = child;
    });
  }
  return root;
}

/**
 * Reads the stored content behind one extension content hash.
 *
 * Every load is an audited read on the server, so the element loads only the
 * hash it is given and only when it is given one: the parent sets
 * `contentHash` after an explicit click. Text shown here was written by an
 * extension author and redacted on the reporting device. It is rendered as
 * text, or as Markdown through the strict sanitiser, never as raw HTML.
 *
 * Fires `approve-content` with `{ contentHash }` when the reader approves the
 * exact content on screen.
 */
@customElement('extension-content-viewer')
export class ExtensionContentViewer extends LitElement {
  @property({ attribute: false }) extension: ExtensionKey | null = null;
  @property() contentHash = '';
  /** Hashes pinned for this extension. The diff compares against these. */
  @property({ attribute: false }) pinnedHashes: string[] = [];
  @property({ type: Boolean }) canManage = false;
  @property({ type: Boolean }) busy = false;
  @property({ attribute: false }) loader = getExtensionContent;
  @state() private content: ExtensionContent | null = null;
  @state() private status: 'idle' | 'loading' | 'ready' | 'absent' | 'error' =
    'idle';
  /** Null until the reader chooses: the default then depends on the file. */
  @state() private raw: boolean | null = null;
  @state() private openPath = '';
  @state() private compareHash = '';
  @state() private compareContent: ExtensionContent | null = null;
  @state() private compareStatus: 'idle' | 'loading' | 'ready' | 'absent' =
    'idle';
  private requestId = 0;

  static styles = [
    css`
      :host {
        display: block;
        font-size: var(--console-text-body);
        max-width: 960px;
      }
      .head {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
        margin: 12px 0 8px;
      }
      h4 {
        margin: 0;
        font-size: var(--console-text-card-title);
        font-weight: 600;
      }
      .meta,
      .note {
        color: var(--console-meta-color);
        font-size: var(--console-text-meta);
      }
      .note {
        margin: 0 0 8px;
      }
      .note strong {
        font-weight: 600;
        color: inherit;
      }
      /* A literal thing to read and select: the one filled block a card may
         hold, on the page rung (DESIGN.md, depth limit). */
      pre {
        margin: 0 0 12px;
        padding: 12px;
        background: var(--console-page);
        border-radius: var(--sl-border-radius-medium);
        font-family: var(--sl-font-mono);
        font-size: var(--console-text-meta);
        line-height: 1.5;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        max-height: 480px;
        overflow: auto;
      }
      .rendered {
        border-left: 2px solid var(--console-hairline);
        padding-left: 12px;
        margin: 0 0 12px;
      }
      .matcher-sets,
      .matcher-sets ul {
        margin: 0 0 8px;
        padding-left: 20px;
      }
      .hook {
        border-top: 1px solid var(--console-hairline);
        padding-top: 8px;
      }
      .hook .label {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-bottom: 4px;
      }
      .layout {
        display: grid;
        grid-template-columns: minmax(180px, 260px) minmax(0, 1fr);
        gap: 16px;
      }
      @media (max-width: 720px) {
        .layout {
          grid-template-columns: 1fr;
        }
      }
      .tree,
      .tree ul {
        list-style: none;
        margin: 0;
        padding: 0;
      }
      .tree ul {
        padding-left: 12px;
        border-left: 1px solid var(--console-hairline);
        margin-left: 4px;
      }
      .tree li {
        margin: 2px 0;
      }
      .tree .dir {
        font-weight: 600;
      }
      .tree button {
        all: unset;
        cursor: pointer;
        color: var(--console-link-color);
        overflow-wrap: anywhere;
      }
      .tree button:focus-visible {
        outline: 2px solid var(--sl-color-primary-600);
        outline-offset: 2px;
      }
      .tree button[aria-current='true'] {
        font-weight: 600;
        text-decoration: underline;
      }
      .tree .withheld {
        color: var(--console-meta-color);
      }
      /* Same recipe as the argument diff: a 16% tint with -800 ink, and a
         leading sign so the direction does not rest on colour alone. */
      .diff-lines {
        margin: 0 0 12px;
        padding: 12px;
        background: var(--console-page);
        border-radius: var(--sl-border-radius-medium);
        font-family: var(--sl-font-mono);
        font-size: var(--console-text-meta);
        line-height: 1.5;
        max-height: 480px;
        overflow: auto;
      }
      .diff-lines > div {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        min-height: 1.5em;
      }
      .diff .removed {
        background: color-mix(
          in srgb,
          var(--sl-color-danger-500) 16%,
          transparent
        );
        color: var(--sl-color-danger-800);
      }
      .diff .added {
        background: color-mix(
          in srgb,
          var(--sl-color-success-500) 16%,
          transparent
        );
        color: var(--sl-color-success-800);
      }
      .diff .gap {
        color: var(--console-meta-color);
      }
      .actions {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
        margin: 8px 0;
      }
      code {
        font-size: var(--console-text-meta);
      }
    `,
    unsafeCSS(markdownBodyCss),
  ];

  protected willUpdate(changed: PropertyValues<this>): void {
    if (changed.has('contentHash') || changed.has('extension'))
      void this.load();
  }

  private async load(): Promise<void> {
    const id = ++this.requestId;
    this.content = null;
    this.compareHash = '';
    this.compareContent = null;
    this.compareStatus = 'idle';
    this.openPath = '';
    this.raw = null;
    if (!this.contentHash || !this.extension || !this.canManage) {
      this.status = 'idle';
      return;
    }
    this.status = 'loading';
    try {
      const content = await this.loader(
        this.extension.extension_kind,
        this.contentHash
      );
      if (id !== this.requestId) return;
      this.content = content;
      this.status = content ? 'ready' : 'absent';
      this.openPath = content ? this.defaultPath(content) : '';
    } catch {
      if (id === this.requestId) this.status = 'error';
    }
  }

  private defaultPath(content: ExtensionContent): string {
    const stored = content.files.filter((file) => file.status === 'stored');
    const preferred =
      stored.find((file) => file.path === 'SKILL.md') ??
      stored.find((file) => file.path.endsWith('plugin.json')) ??
      stored.find((file) => isMarkdown(file.path)) ??
      stored[0] ??
      content.files[0];
    return preferred?.path ?? '';
  }

  private async compare(hash: string): Promise<void> {
    if (!this.extension) return;
    const id = this.requestId;
    this.compareHash = hash;
    this.compareContent = null;
    this.compareStatus = 'loading';
    try {
      const content = await this.loader(this.extension.extension_kind, hash);
      if (id !== this.requestId || this.compareHash !== hash) return;
      this.compareContent = content;
      this.compareStatus = content ? 'ready' : 'absent';
    } catch {
      if (id === this.requestId) this.compareStatus = 'absent';
    }
  }

  private approve(): void {
    this.dispatchEvent(
      new CustomEvent('approve-content', {
        detail: { contentHash: this.contentHash },
        bubbles: true,
        composed: true,
      })
    );
  }

  private redactionNote(counts: Record<string, number>): TemplateResult {
    const types = Object.entries(counts)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([kind, count]) => `${kind} (${count})`)
      .join(', ');
    return html`<p class="note redaction">
      <strong>${total(counts)} value(s) redacted on the device:</strong>
      ${types}. The text below is not the exact bytes behind the hash, and the
      server could not check it against the hash.
    </p>`;
  }

  private fileBody(file: ExtensionContentFile): TemplateResult {
    if (file.status !== 'stored' || file.text === null)
      return html`<p class="note withheld-file">
        <strong>${file.path}:</strong> ${
          WITHHELD_FILE_TEXT[
            file.status as Exclude<ExtensionContentFileStatus, 'stored'>
          ]
        }
      </p>`;
    const markdown = isMarkdown(file.path);
    const { front, body } = markdown
      ? splitFrontMatter(file.text)
      : { front: '', body: file.text };
    const rendered = markdown
      ? renderUntrustedMarkdownChecked(body)
      : { html: '', omitted: 0 };
    // A file whose rendering leaves text out opens as raw source: an agent
    // reads the whole file, so the reviewer is shown the whole file first.
    const raw = this.raw ?? rendered.omitted > 0;
    return html`
      <div class="actions">
        <code>${file.path}</code>
        ${
          file.verified
            ? html`<span class="meta verified">Matches its hash</span>`
            : nothing
        }
        ${
          markdown
            ? html`<sl-button
                size="small"
                variant="text"
                class="toggle-raw"
                aria-pressed=${raw ? 'true' : 'false'}
                @click=${() => (this.raw = !raw)}
                >${raw ? 'Show rendered' : 'Show raw'}</sl-button
              >`
            : nothing
        }
      </div>
      ${total(file.redactions) ? this.redactionNote(file.redactions) : nothing}
      ${
        rendered.omitted
          ? html`<p class="note omitted-note" role="note">
              <strong
                >The rendered view leaves out ${rendered.omitted} part(s) of
                this file:</strong
              >
              HTML, comments, images or link targets that are not shown as text.
              An agent reads them.
              ${
                raw
                  ? 'The raw source is shown so that nothing is hidden.'
                  : 'Switch to the raw source before approving.'
              }
            </p>`
          : nothing
      }
      ${
        markdown && !raw
          ? html`${front ? html`<pre class="front">${front}</pre>` : nothing}
              <div class="rendered markdown-body">
                ${unsafeHTML(rendered.html)}
              </div>`
          : html`<pre class="raw">${file.text}</pre>`
      }
    `;
  }

  private tree(node: TreeNode): TemplateResult {
    const children = [...node.children.values()].sort(
      (a, b) =>
        Number(!!a.file) - Number(!!b.file) || a.name.localeCompare(b.name)
    );
    return html`<ul>
      ${children.map((child) =>
        child.file
          ? html`<li>
              <button
                class=${child.file.status === 'stored' ? 'file' : 'file withheld'}
                aria-current=${this.openPath === child.file.path ? 'true' : 'false'}
                @click=${() => {
                  this.openPath = (child.file as ExtensionContentFile).path;
                  this.raw = null;
                }}
              >
                ${child.name}
              </button>
              ${
                child.file.status === 'stored'
                  ? total(child.file.redactions)
                    ? html`<span class="meta">redacted</span>`
                    : nothing
                  : html`<span class="meta"
                      >${child.file.status.replace(/_/g, ' ')}</span
                    >`
              }
            </li>`
          : html`<li>
              <span class="dir">${child.name}/</span>${this.tree(child)}
            </li>`
      )}
    </ul>`;
  }

  private files(content: ExtensionContent): TemplateResult {
    const open =
      content.files.find((file) => file.path === this.openPath) ??
      content.files[0];
    if (content.files.length === 1) return this.fileBody(open);
    return html`<div class="layout">
      <nav class="tree" aria-label="Files in this extension">
        ${this.tree(buildTree(content.files))}
      </nav>
      <div class="viewer">${open ? this.fileBody(open) : nothing}</div>
    </div>`;
  }

  private hook(hook: ExtensionContentHook): TemplateResult {
    return html`<div class="hook">
      <div class="label">
        <span>Matcher: <code>${hook.matcher || 'every call'}</code></span>
        ${
          hook.verified
            ? html`<span class="meta verified">Matches its hash</span>`
            : nothing
        }
      </div>
      ${
        total(hook.matcher_redactions ?? {})
          ? html`<p class="note redaction matcher-redaction">
              <strong>The matcher was redacted on the device:</strong>
              ${Object.keys(hook.matcher_redactions ?? {})
                .sort()
                .join(', ')}.
            </p>`
          : nothing
      }
      ${total(hook.redactions) ? this.redactionNote(hook.redactions) : nothing}
      ${
        hook.status === 'stored' && hook.command !== null
          ? html`<pre class="raw">${hook.command}</pre>`
          : html`<p class="note withheld-file">
              ${
                hook.status === 'private_key'
                  ? WITHHELD_FILE_TEXT.private_key
                  : hook.status === 'over_budget'
                    ? WITHHELD_FILE_TEXT.over_budget
                    : WITHHELD_FILE_TEXT.too_large
              }
            </p>`
      }
    </div>`;
  }

  /**
   * What a pin on a hook hash does and does not cover.
   *
   * The hash is computed from the command strings. The matcher decides when
   * a command runs and is not in the hash, so it can change, or differ
   * between devices, while the hash and any pin stay the same. That is said
   * outright, and every assignment reported for the hash is listed.
   */
  private matcherNote(content: ExtensionContent): TemplateResult {
    const sets = content.matcher_sets ?? [];
    const command = new Map(
      content.hooks.map((hook) => [hook.command_hash, hook.command])
    );
    const describe = (matcher: string | null, hash: string): string => {
      const text = command.get(hash) ?? '';
      const first = text.split('\n')[0].slice(0, 60);
      return `${matcher || 'every call'}: ${first || hash.slice(0, 19)}`;
    };
    return html`<div class="matchers">
      <p class="note matcher-scope">
        <strong
          >A pin on this hash covers the commands, not the matchers.</strong
        >
        The content hash is computed from the command strings. A matcher decides
        when a command runs and can change without the hash changing. Check the
        matchers below each time you rely on this approval.
      </p>
      ${
        sets.length > 1
          ? html`<p class="note matcher-changed" role="note">
                <strong
                  >${sets.length} different matcher assignments were reported
                  for these commands.</strong
                >
                The matchers changed, or differ between devices. Newest last:
              </p>
              <ul class="matcher-sets">
                ${sets.map(
                  (set) =>
                    html`<li>
                      <span class="meta"
                        >Last reported
                        ${set.last_seen_at?.slice(0, 16).replace('T', ' ') ?? 'unknown'}
                        UTC</span
                      >
                      <ul>
                        ${set.matchers.map(
                          (entry) =>
                            html`<li>
                              <code
                                >${describe(entry.matcher, entry.command_hash)}</code
                              >
                            </li>`
                        )}
                      </ul>
                    </li>`
                )}
              </ul>`
          : nothing
      }
    </div>`;
  }

  /** Stable text per entry, so two contents can be compared by name. */
  private entries(content: ExtensionContent): Map<string, string | null> {
    const entries = new Map<string, string | null>();
    for (const file of content.files)
      entries.set(file.path, file.status === 'stored' ? file.text : null);
    if (content.hooks.length)
      entries.set(
        'hook commands',
        content.hooks
          .map(
            (hook) =>
              `[${hook.matcher || 'every call'}] ${hook.command ?? `(${hook.status})`}`
          )
          .join('\n')
      );
    return entries;
  }

  private digests(content: ExtensionContent): Map<string, string> {
    const digests = new Map<string, string>();
    for (const file of content.files)
      digests.set(file.path, file.sha256 ?? file.status);
    if (content.hooks.length)
      digests.set(
        'hook commands',
        content.hooks.map((hook) => hook.command_hash).join(',')
      );
    return digests;
  }

  private diff(pinned: ExtensionContent, current: ExtensionContent) {
    const before = this.entries(pinned);
    const after = this.entries(current);
    const beforeDigest = this.digests(pinned);
    const afterDigest = this.digests(current);
    const names = [...new Set([...before.keys(), ...after.keys()])].sort();
    const changed = names.filter(
      (name) => beforeDigest.get(name) !== afterDigest.get(name)
    );
    return html`<section
      class="diff"
      aria-label="Changes since the pinned content"
    >
      <p class="note">
        ${changed.length} of ${names.length} entries differ between pinned
        <code>${shortHash(pinned.content_hash)}</code> and
        <code>${shortHash(current.content_hash)}</code>. Removed lines are from
        the pinned content, added lines from the content being read.
      </p>
      ${changed.map((name) => {
        const old = before.get(name);
        const now = after.get(name);
        const head = html`<div class="actions">
          <code>${name}</code>
          <span class="meta"
            >${
              !before.has(name)
                ? 'added'
                : !after.has(name)
                  ? 'removed'
                  : 'changed'
            }</span
          >
        </div>`;
        if (old == null && now == null)
          return html`${head}
            <p class="note">
              No text is stored for either version of this entry.
            </p>`;
        // An entry that exists on one side only has no lines on the other.
        const hunks = diffHunks(
          diffLines(
            old == null ? [] : toDiffLines(old),
            now == null ? [] : toDiffLines(now)
          )
        );
        if (!hunks.length)
          return html`${head}
            <p class="note">
              The bytes changed but the stored text is identical: the change is
              inside a redacted value or in text that was not uploaded.
            </p>`;
        return html`${head}
          <div class="diff-lines">
            ${hunks.map((line) => this.diffLine(line))}
          </div>`;
      })}
    </section>`;
  }

  /** One diff line as its own block, so layout never depends on the
   *  whitespace of this template. */
  private diffLine(line: HunkLine): TemplateResult {
    const sign =
      line.type === 'removed'
        ? '- '
        : line.type === 'added'
          ? '+ '
          : line.type === 'gap'
            ? '… '
            : '  ';
    return html`<div class=${line.type}>${sign}${line.text}</div>`;
  }

  private absent(): TemplateResult {
    return html`<div class="absent">
      <p class="note">
        <strong>No content is stored for this hash.</strong> Content arrives
        with the next discovery report from a device that has this exact
        version, unless the account setting "Collect extension content for
        review" is off, the device or the extension opted out, or the collector
        on that device predates content collection.
      </p>
      <p class="note">
        To read it on a device without sending anything, use the local
        <code>preloop agents extensions show</code> command given for this
        extension.
      </p>
    </div>`;
  }

  render(): TemplateResult | typeof nothing {
    if (!this.contentHash || !this.extension) return nothing;
    if (!this.canManage)
      return html`<p class="note">
        Reading extension content needs the permission to approve extensions.
      </p>`;
    if (this.status === 'loading')
      return html`<p class="note" role="status">Loading content…</p>`;
    if (this.status === 'error')
      return html`<p class="note" role="alert">
        Could not load the content. Try again.
      </p>`;
    if (this.status === 'absent') return this.absent();
    const content = this.content;
    if (!content) return nothing;
    const pinned = this.pinnedHashes.includes(content.content_hash);
    const others = this.pinnedHashes.filter(
      (hash) => hash !== content.content_hash
    );
    return html`
      <div class="head">
        <h4>Content of <code>${shortHash(content.content_hash)}</code></h4>
        ${
          pinned
            ? html`<sl-badge pill variant="success">Pinned</sl-badge>`
            : nothing
        }
        ${
          content.redacted
            ? html`<sl-badge pill variant="warning" class="redacted-badge"
                >${content.redaction_count} redacted</sl-badge
              >`
            : nothing
        }
        ${
          content.withheld_count
            ? html`<sl-badge pill variant="neutral" class="withheld-badge"
                >${content.withheld_count} not uploaded</sl-badge
              >`
            : nothing
        }
      </div>
      <p class="note">
        ${
          content.verification === 'verified'
            ? 'Every text below matched its hash when the server received it.'
            : content.verification === 'none'
              ? 'No text is stored for this content.'
              : 'Text marked as redacted was changed on the device before upload and could not be checked against the hash. Redaction reduces the chance that a secret is stored; it is not a guarantee.'
        }
        Collected by CLI ${content.collector_version ?? 'unknown'}, kept
        ${content.retention_days} days after the last report. This read was
        recorded in the audit log.
      </p>
      ${
        content.withheld
          ? html`<p class="note withheld-extension">
              <strong>Withheld on the device.</strong>
              ${
                content.withheld === 'excluded'
                  ? 'This extension is excluded from content collection on the reporting device (a .preloop-no-content file or an exclusion pattern in the CLI config).'
                  : 'This extension is over the size caps for content collection.'
              }
            </p>`
          : nothing
      }
      ${content.hooks.length ? this.matcherNote(content) : nothing}
      ${content.hooks.map((hook) => this.hook(hook))}
      ${content.files.length ? this.files(content) : nothing}
      <div class="actions">
        ${
          this.canManage && !pinned && !content.withheld
            ? html`<sl-button
                size="small"
                variant="primary"
                class="approve-content"
                ?disabled=${this.busy}
                @click=${() => this.approve()}
                >Approve this exact content</sl-button
              >`
            : nothing
        }
        ${others.map(
          (hash) =>
            html`<sl-button
              size="small"
              class="compare"
              ?loading=${this.compareStatus === 'loading' && this.compareHash === hash}
              @click=${() => this.compare(hash)}
              >Compare with pinned ${shortHash(hash)}</sl-button
            >`
        )}
      </div>
      ${
        this.compareStatus === 'ready' && this.compareContent
          ? this.diff(this.compareContent, content)
          : this.compareStatus === 'absent'
            ? html`<p class="note compare-absent">
                The pinned content is not stored, so there is nothing to compare
                with. It was never uploaded or has passed its retention.
              </p>`
            : nothing
      }
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'extension-content-viewer': ExtensionContentViewer;
  }
}
