import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';

/**
 * A single streamed terminal output line. `cls` selects a colour treatment
 * ("ok" for discovered/routed agents, "done" for the final summary line).
 */
export interface OnboardingDemoLine {
  text: string;
  cls?: 'ok' | 'done' | 'muted';
}

export interface OnboardingDemoConfig {
  command: string;
  image: string;
  imageAlt?: string;
  caption?: string;
  lines: OnboardingDemoLine[];
}

/**
 * Onboarding demo: a self-contained, looping illustration that types
 * `preloop agents discover`, streams the command output, and reveals the
 * onboarding screenshot the moment the "onboarded" line lands.
 *
 * The terminal animation and the screenshot reveal are both driven by one JS
 * timeline, so they are inherently synchronised — there is no attempt to read
 * playback position from an animated asset (which is brittle/impossible for an
 * animated webp). The visual is decorative; the crawlable copy for this section
 * is emitted as slotted light-DOM content by the brand build plugin.
 */
@customElement('onboarding-demo')
export class OnboardingDemo extends LitElement {
  @property({ attribute: false }) config?: OnboardingDemoConfig;

  @state() private _typed = '';
  @state() private _visibleLines = 0;
  @state() private _revealed = false;
  @state() private _typing = false;

  private _timers: number[] = [];
  private _observer?: IntersectionObserver;
  private _started = false;

  static styles = css`
    :host {
      display: block;
    }

    .demo {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 3rem;
      align-items: center;
    }

    /* Terminal */
    .terminal {
      border-radius: 12px;
      border: 1px solid rgba(255, 255, 255, 0.1);
      background: #0d1117;
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4);
      overflow: hidden;
      font-family:
        'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas,
        monospace;
    }

    .term-bar {
      display: flex;
      gap: 0.5rem;
      align-items: center;
      padding: 0.85rem 1rem;
      background: rgba(255, 255, 255, 0.03);
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    }

    .term-bar span {
      width: 11px;
      height: 11px;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.18);
    }

    .term-bar span:nth-child(1) {
      background: #ff5f56;
    }
    .term-bar span:nth-child(2) {
      background: #ffbd2e;
    }
    .term-bar span:nth-child(3) {
      background: #27c93f;
    }

    .term-title {
      margin-left: 0.5rem;
      color: rgba(203, 213, 225, 0.55);
      font-size: 0.78rem;
      letter-spacing: 0.02em;
    }

    .term-body {
      padding: 1.25rem 1.4rem 1.5rem;
      font-size: 0.92rem;
      line-height: 1.7;
      min-height: 16.5rem;
      color: rgba(203, 213, 225, 0.9);
    }

    .term-line {
      white-space: pre-wrap;
      word-break: break-word;
    }

    .term-line.cmd {
      color: #f8fafc;
      margin-bottom: 0.35rem;
    }

    .prompt {
      color: #22d3ee;
      font-weight: 700;
      margin-right: 0.5rem;
    }

    .cursor {
      display: inline-block;
      width: 0.6ch;
      height: 1.1em;
      transform: translateY(0.18em);
      background: #22d3ee;
      margin-left: 1px;
    }

    .cursor.blink {
      animation: blink 1s step-end infinite;
    }

    @keyframes blink {
      50% {
        opacity: 0;
      }
    }

    .term-line.ok {
      color: #4ade80;
    }
    .term-line.done {
      color: #67e8f9;
      font-weight: 600;
      margin-top: 0.4rem;
    }
    .term-line.muted {
      color: rgba(203, 213, 225, 0.6);
    }

    /* Streamed lines animate in */
    .term-line:not(.cmd) {
      animation: lineIn 0.18s ease-out both;
    }

    @keyframes lineIn {
      from {
        opacity: 0;
        transform: translateY(3px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    /* The "effect": screenshot stays dim until the run completes, then snaps
       to full colour — visually presenting itself as the command's result. */
    .effect {
      margin: 0;
      position: relative;
    }

    .effect img {
      display: block;
      width: 100%;
      height: auto;
      border-radius: 12px;
      border: 1px solid rgba(255, 255, 255, 0.1);
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.32);
      filter: grayscale(0.85) brightness(0.7);
      opacity: 0.5;
      transform: scale(0.985);
      transition:
        filter 0.6s ease,
        opacity 0.6s ease,
        transform 0.6s ease;
    }

    .demo.is-revealed .effect img {
      filter: none;
      opacity: 1;
      transform: scale(1);
    }

    .effect figcaption {
      margin-top: 1rem;
      color: rgba(203, 213, 225, 0.7);
      font-size: 0.95rem;
      line-height: 1.55;
      text-align: center;
      opacity: 0;
      transition: opacity 0.6s ease 0.1s;
    }

    .demo.is-revealed .effect figcaption {
      opacity: 1;
    }

    @media (max-width: 900px) {
      .demo {
        grid-template-columns: 1fr;
        gap: 2rem;
      }
      .term-body {
        min-height: 0;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .term-line:not(.cmd),
      .effect img,
      .effect figcaption {
        animation: none;
        transition: none;
      }
    }
  `;

  firstUpdated() {
    const prefersReduced = window.matchMedia?.(
      '(prefers-reduced-motion: reduce)'
    ).matches;
    if (prefersReduced) {
      this._showFinalState();
      return;
    }

    // Start once the demo scrolls into view, so the typing isn't missed if it
    // sits below the fold.
    if ('IntersectionObserver' in window) {
      this._observer = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (entry.isIntersecting && !this._started) {
              this._started = true;
              this._start();
            }
          }
        },
        { threshold: 0.35 }
      );
      this._observer.observe(this);
    } else {
      this._started = true;
      this._start();
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._clearTimers();
    this._observer?.disconnect();
    this._observer = undefined;
  }

  private _clearTimers() {
    this._timers.forEach((id) => window.clearTimeout(id));
    this._timers = [];
  }

  private _after(ms: number, fn: () => void) {
    this._timers.push(window.setTimeout(fn, ms));
  }

  private _showFinalState() {
    const cfg = this.config;
    if (!cfg) return;
    this._typed = cfg.command;
    this._visibleLines = cfg.lines.length;
    this._revealed = true;
    this._typing = false;
  }

  private _start() {
    const cfg = this.config;
    if (!cfg || !cfg.command) return;
    this._clearTimers();
    this._typed = '';
    this._visibleLines = 0;
    this._revealed = false;
    this._typing = true;
    this._typeCommand(0);
  }

  private _typeCommand(i: number) {
    const cfg = this.config;
    if (!cfg) return;
    if (i > cfg.command.length) {
      this._typing = false;
      this._after(500, () => this._streamLines(0));
      return;
    }
    this._typed = cfg.command.slice(0, i);
    this._after(45, () => this._typeCommand(i + 1));
  }

  private _streamLines(n: number) {
    const cfg = this.config;
    if (!cfg) return;
    if (n >= cfg.lines.length) {
      // Hold the completed state, then loop.
      this._after(5500, () => {
        if (this._started) this._start();
      });
      return;
    }
    this._visibleLines = n + 1;
    const isLast = n === cfg.lines.length - 1;
    if (isLast) {
      this._revealed = true;
    }
    this._after(isLast ? 700 : 360, () => this._streamLines(n + 1));
  }

  render() {
    const cfg = this.config;
    if (!cfg) return html``;

    const lines = cfg.lines.slice(0, this._visibleLines);

    return html`
      <div class="demo ${this._revealed ? 'is-revealed' : ''}">
        <div
          class="terminal"
          role="img"
          aria-label="Terminal running ${cfg.command}"
        >
          <div class="term-bar" aria-hidden="true">
            <span></span><span></span><span></span>
            <span class="term-title">preloop — agents discover</span>
          </div>
          <div class="term-body">
            <div class="term-line cmd">
              <span class="prompt">$</span
              ><span class="typed">${this._typed}</span
              ><span class="cursor ${this._typing ? 'blink' : ''}"></span>
            </div>
            ${lines.map(
              (line) =>
                html`<div class="term-line ${line.cls || ''}">
                  ${line.text}
                </div>`
            )}
          </div>
        </div>

        <figure class="effect">
          <img src=${cfg.image} alt=${cfg.imageAlt || ''} loading="lazy" />
          ${cfg.caption ? html`<figcaption>${cfg.caption}</figcaption>` : ''}
        </figure>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'onboarding-demo': OnboardingDemo;
  }
}
