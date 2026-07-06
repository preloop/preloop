import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

@customElement('json-tree')
export class JsonTree extends LitElement {
  static styles = css`
    :host {
      display: block;
      font-family:
        ui-monospace, SFMono-Regular, Consolas, 'Liberation Mono', Menlo,
        monospace;
      font-size: 0.85rem;
      line-height: 1.5;
    }

    .node {
      display: flex;
      flex-direction: row;
      align-items: flex-start;
      padding-left: 1rem;
      position: relative;
    }

    .node.root {
      padding-left: 0;
    }

    .key {
      color: var(--color-primary-500, #3b82f6);
      font-weight: 500;
      margin-right: 0.5rem;
      cursor: pointer;
      user-select: none;
    }

    .colon {
      color: var(--color-gray-500, #6b7280);
      margin-right: 0.5rem;
    }

    .value.string {
      color: var(--color-green-600, #16a34a);
      word-break: break-all;
    }
    .value.number {
      color: var(--color-orange-600, #ea580c);
    }
    .value.boolean {
      color: var(--color-purple-600, #9333ea);
    }
    .value.null {
      color: var(--color-gray-500, #6b7280);
      font-style: italic;
    }

    .toggle {
      position: absolute;
      left: 0;
      top: 0.1rem;
      width: 1rem;
      height: 1rem;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      color: var(--color-gray-500, #6b7280);
      font-size: 0.7rem;
      transition: transform 0.2s;
    }

    .toggle:hover {
      color: var(--color-gray-800, #1f2937);
    }

    .toggle.expanded {
      transform: rotate(90deg);
    }

    .brace {
      color: var(--color-gray-600, #4b5563);
    }

    .children {
      display: flex;
      flex-direction: column;
    }

    .hidden {
      display: none;
    }

    .summary {
      color: var(--color-gray-400, #9ca3af);
      font-style: italic;
      cursor: pointer;
      user-select: none;
    }

    .summary:hover {
      color: var(--color-gray-600, #4b5563);
    }

    /* Dark mode support if applicable */
    @media (prefers-color-scheme: dark) {
      .key {
        color: var(--color-primary-400, #60a5fa);
      }
      .value.string {
        color: var(--color-green-400, #4ade80);
      }
      .value.number {
        color: var(--color-orange-400, #fb923c);
      }
      .value.boolean {
        color: var(--color-purple-400, #c084fc);
      }
      .value.null {
        color: var(--color-gray-400, #9ca3af);
      }
      .toggle:hover {
        color: var(--color-gray-200, #e5e7eb);
      }
    }
  `;

  @property({ type: Object }) data: any = null;
  @property({ type: String }) name: string = '';
  @property({ type: Boolean }) isRoot: boolean = true;
  @property({ type: Boolean }) initiallyExpanded: boolean = false;
  // Let parents pass down current path context like "messages[0].role"
  @property({ type: String }) path: string = '';
  @property({ type: Boolean }) isLastArrayItem: boolean = false;

  @state() private expanded = false;

  connectedCallback() {
    super.connectedCallback();
    this.expanded =
      this.isRoot || this.initiallyExpanded || this.shouldAutoExpand();

    if (
      this.expanded &&
      this.isLastArrayItem &&
      this.path.match(/messages\[\d+\]$/)
    ) {
      // Emit event so the parent widget can scroll to it
      requestAnimationFrame(() => {
        this.dispatchEvent(
          new CustomEvent('json-tree-scroll-target', {
            bubbles: true,
            composed: true,
          })
        );
      });
    }
  }

  private shouldAutoExpand(): boolean {
    // Basic heuristics: auto-expand small arrays or objects
    if (!this.data) return false;

    // Auto expand the specific "user" and "assistant" roles if requested
    if (this.path.includes('messages')) {
      // If we are inside an item of the messages array (like "messages[count-1]"),
      // expand only if it is the precisely marked last item.
      if (this.path.match(/messages\[\d+\]$/)) {
        return this.isLastArrayItem;
      }

      // Keep expanding contents inside the single expanded last message object
      if (
        typeof this.data === 'object' &&
        !Array.isArray(this.data) &&
        this.data.role
      ) {
        return true;
      }
    }

    // Expand small arrays/objects
    if (Array.isArray(this.data) && this.data.length <= 3) return true;
    if (typeof this.data === 'object' && Object.keys(this.data).length <= 3)
      return true;

    return false;
  }

  private toggle() {
    this.expanded = !this.expanded;
  }

  render() {
    if (this.data === undefined) return nothing;

    const isArray = Array.isArray(this.data);
    const isObject =
      this.data !== null && typeof this.data === 'object' && !isArray;

    if (!isObject && !isArray) {
      return this.renderPrimitive();
    }

    const items = isArray ? this.data : Object.entries(this.data);
    const isEmpty = items.length === 0;

    const startBrace = isArray ? '[' : '{';
    const endBrace = isArray ? ']' : '}';
    const summary = isArray
      ? `Array(${items.length})`
      : `Object(${items.length} keys)`;

    return html`
      <div class="node ${this.isRoot ? 'root' : ''}">
        ${
          !this.isRoot && !isEmpty
            ? html`
                <span
                  class="toggle ${this.expanded ? 'expanded' : ''}"
                  @click=${this.toggle}
                  >▶</span
                >
              `
            : nothing
        }

        <div>
          ${
            this.name
              ? html`
                  <span class="key" @click=${this.toggle}>${this.name}</span
                  ><span class="colon">:</span>
                `
              : nothing
          }

          <span class="brace">${startBrace}</span>

          ${
            !this.expanded && !isEmpty
              ? html`
                  <span class="summary" @click=${this.toggle}>${summary}</span>
                `
              : nothing
          }

          <div class="children ${this.expanded ? '' : 'hidden'}">
            ${
              isArray
                ? items.map(
                    (item: any, i: number) => html`
                      <json-tree
                        .data=${item}
                        .isRoot=${false}
                        path="${this.path}[${i}]"
                        .isLastArrayItem=${i === items.length - 1}
                      ></json-tree>
                    `
                  )
                : items.map(
                    ([key, value]: [string, any]) => html`
                      <json-tree
                        .data=${value}
                        name=${key}
                        .isRoot=${false}
                        path="${this.path ? this.path + '.' : ''}${key}"
                      ></json-tree>
                    `
                  )
            }
          </div>

          ${
            isEmpty || this.expanded
              ? html`<span class="brace">${endBrace}</span>`
              : nothing
          }
        </div>
      </div>
    `;
  }

  private renderPrimitive() {
    const type = this.data === null ? 'null' : typeof this.data;
    let displayValue = String(this.data);

    if (type === 'string') {
      displayValue = `"${this.data}"`;
    }

    return html`
      <div class="node ${this.isRoot ? 'root' : ''}">
        <div>
          ${
            this.name
              ? html`
                  <span class="key">${this.name}</span
                  ><span class="colon">:</span>
                `
              : nothing
          }
          <span class="value ${type}">${displayValue}</span>
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'json-tree': JsonTree;
  }
}
