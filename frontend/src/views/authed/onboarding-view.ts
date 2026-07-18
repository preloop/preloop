import { LitElement, html, css } from 'lit';
import { customElement } from 'lit/decorators.js';
import '../../components/view-header.ts';

import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import { state } from 'lit/decorators.js';

@customElement('onboarding-view')
export class OnboardingView extends LitElement {
  @state()
  private activeTab: 'cli' | 'plugin' | 'gateway' = 'cli';
  static styles = [
    css`
      :host {
        display: block;
        padding: var(--sl-spacing-large);
        max-width: 800px;
        margin: 0 auto;
      }

      .hero {
        text-align: center;
        margin-bottom: var(--sl-spacing-3x-large);
      }

      .hero h1 {
        font-size: var(--sl-font-size-2x-large);
        margin-bottom: var(--sl-spacing-small);
        color: var(--sl-color-neutral-900);
      }

      .hero p {
        color: var(--sl-color-neutral-500);
        margin: 0;
      }

      .instruction-step {
        display: flex;
        gap: var(--sl-spacing-medium);
        margin-bottom: var(--sl-spacing-large);
      }

      .step-number {
        width: 40px;
        height: 40px;
        border-radius: 50%;
        background: var(--sl-color-primary-100);
        color: var(--sl-color-primary-600);
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: bold;
        flex-shrink: 0;
      }

      .step-content h3 {
        margin: 0 0 var(--sl-spacing-x-small) 0;
        color: var(--sl-color-neutral-800);
      }

      .step-content p {
        margin: 0 0 var(--sl-spacing-medium) 0;
        color: var(--sl-color-neutral-600);
        font-size: var(--sl-font-size-small);
      }

      .code-block {
        background: var(--sl-color-neutral-50);
        border: 1px solid var(--sl-color-neutral-200);
        border-radius: var(--sl-border-radius-medium);
        padding: var(--sl-spacing-small) var(--sl-spacing-medium);
        display: flex;
        justify-content: space-between;
        align-items: center;
      }

      .code-block code {
        font-family: var(--sl-font-mono);
        font-size: var(--sl-font-size-small);
        color: var(--sl-color-primary-600);
      }
    `,
  ];

  render() {
    return html`
      <div
        class="bg-background-dark text-text-main font-body p-6 md:p-12 min-h-[calc(100vh-64px)] w-full box-border flex justify-center items-start"
      >
        <div
          class="glass-panel w-full max-w-[800px] flex flex-col gap-10 relative z-10 p-8 md:p-12 mt-8 rounded-xl shadow-glass border border-white/5"
        >
          <!-- Hero Section -->
          <div class="text-center flex flex-col gap-3">
            <h1
              class="font-display text-4xl font-bold text-text-main tracking-tight shadow-neon-glow inline-block self-center pb-2 m-0"
            >
              Initialize Workforce
            </h1>
            <p class="text-text-muted text-lg font-body max-w-lg mx-auto m-0">
              Set up the Preloop CLI and connect your OpenClaw agents to the
              secure gateway.
            </p>
          </div>

          <!-- Tabs for Onboarding Paths -->
          <div class="flex flex-col gap-6">
            <div class="flex border-b border-white/10 gap-8">
              <button
                @click=${() => (this.activeTab = 'cli')}
                class="${
                  this.activeTab === 'cli'
                    ? 'text-primary border-primary'
                    : 'text-text-muted hover:text-text-main border-transparent'
                } pb-4 border-b-2 font-display text-sm font-bold tracking-widest flex items-center gap-2 transition-colors"
              >
                <sl-icon name="terminal"></sl-icon> CLI SETUP
              </button>
              <button
                @click=${() => (this.activeTab = 'plugin')}
                class="${
                  this.activeTab === 'plugin'
                    ? 'text-primary border-primary'
                    : 'text-text-muted hover:text-text-main border-transparent'
                } pb-4 border-b-2 font-display text-sm font-bold tracking-widest flex items-center gap-2 transition-colors"
              >
                <sl-icon name="puzzle"></sl-icon> OPENCLAW PLUGIN
              </button>
              <button
                @click=${() => (this.activeTab = 'gateway')}
                class="${
                  this.activeTab === 'gateway'
                    ? 'text-primary border-primary'
                    : 'text-text-muted hover:text-text-main border-transparent'
                } pb-4 border-b-2 font-display text-sm font-bold tracking-widest flex items-center gap-2 transition-colors"
              >
                <sl-icon name="key"></sl-icon> API GATEWAY
              </button>
            </div>

            <!-- CLI Content -->
            ${
              this.activeTab === 'cli'
                ? html`
                    <div
                      class="glass-panel rounded-xl p-8 flex flex-col gap-10"
                    >
                      <div class="flex gap-6 relative group">
                        <div
                          class="absolute -inset-2 bg-primary/5 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity"
                        ></div>
                        <div
                          class="relative z-10 flex-shrink-0 w-10 h-10 rounded-full border border-primary/30 bg-primary/10 flex items-center justify-center font-mono text-primary font-bold shadow-neon-glow"
                        >
                          01
                        </div>
                        <div class="relative z-10 flex flex-col gap-3 flex-1">
                          <h3
                            class="text-text-main font-display font-semibold text-lg tracking-wide m-0"
                          >
                            Install CLI
                          </h3>
                          <p
                            class="text-text-muted text-sm leading-relaxed m-0"
                          >
                            Install the Preloop development tools globally using
                            your package manager of choice.
                          </p>
                          <div
                            class="bg-surface-base border border-white/10 rounded-lg p-3 flex justify-between items-center group-hover:border-primary/50 transition-colors"
                          >
                            <code class="font-mono text-xs text-primary"
                              >npm install -g @preloop/cli</code
                            >
                            <sl-copy-button
                              value="npm install -g @preloop/cli"
                            ></sl-copy-button>
                          </div>
                        </div>
                      </div>

                      <div class="flex gap-6 relative group">
                        <div
                          class="absolute -inset-2 bg-success/5 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity"
                        ></div>
                        <div
                          class="relative z-10 flex-shrink-0 w-10 h-10 rounded-full border border-success/30 bg-success/10 flex items-center justify-center font-mono text-success font-bold drop-shadow-[0_0_8px_rgba(0,255,157,0.4)]"
                        >
                          02
                        </div>
                        <div class="relative z-10 flex flex-col gap-3 flex-1">
                          <h3
                            class="text-text-main font-display font-semibold text-lg tracking-wide m-0"
                          >
                            Authenticate
                          </h3>
                          <p
                            class="text-text-muted text-sm leading-relaxed m-0"
                          >
                            Run the login command. This will open a browser
                            window to securely authorize your local machine.
                          </p>
                          <div
                            class="bg-surface-base border border-white/10 rounded-lg p-3 flex justify-between items-center group-hover:border-success/50 transition-colors"
                          >
                            <code class="font-mono text-xs text-success"
                              >preloop auth login</code
                            >
                            <sl-copy-button
                              value="preloop auth login"
                            ></sl-copy-button>
                          </div>
                        </div>
                      </div>

                      <div class="flex gap-6 relative group">
                        <div
                          class="absolute -inset-2 bg-primary/5 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity"
                        ></div>
                        <div
                          class="relative z-10 flex-shrink-0 w-10 h-10 rounded-full border border-primary/30 bg-primary/10 flex items-center justify-center font-mono text-primary font-bold shadow-neon-glow"
                        >
                          03
                        </div>
                        <div class="relative z-10 flex flex-col gap-3 flex-1">
                          <h3
                            class="text-text-main font-display font-semibold text-lg tracking-wide m-0"
                          >
                            Discover Agents
                          </h3>
                          <p
                            class="text-text-muted text-sm leading-relaxed m-0"
                          >
                            Scan your local environment or remote registry to
                            find compatible AI agents to connect.
                          </p>
                          <div
                            class="bg-surface-base border border-white/10 rounded-lg p-3 flex justify-between items-center group-hover:border-primary/50 transition-colors"
                          >
                            <code class="font-mono text-xs text-primary"
                              >preloop agents discover</code
                            >
                            <sl-copy-button
                              value="preloop agents discover"
                            ></sl-copy-button>
                          </div>
                        </div>
                      </div>
                    </div>
                  `
                : ''
            }

            <!-- OpenClaw Plugin Content -->
            ${
              this.activeTab === 'plugin'
                ? html`
                    <div class="glass-panel rounded-xl p-8 flex flex-col gap-6">
                      <div class="flex justify-between items-start">
                        <div class="flex flex-col gap-1">
                          <h3
                            class="text-text-main font-display font-semibold text-lg tracking-wide m-0"
                          >
                            OpenClaw Integration
                          </h3>
                          <p class="text-text-muted text-sm m-0">
                            Connect your workforce directly to the OpenClaw
                            ecosystem.
                          </p>
                        </div>
                        <span
                          class="px-2 py-1 rounded bg-success/10 text-success text-[10px] font-bold tracking-widest border border-success/20 uppercase"
                          >Recommended</span
                        >
                      </div>

                      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <button
                          class="flex flex-col items-center justify-center p-6 rounded-lg border border-white/10 bg-white/5 hover:bg-primary/5 hover:border-primary/50 transition-all group gap-3 text-center cursor-pointer"
                        >
                          <sl-icon
                            name="box-arrow-up-right"
                            class="text-3xl text-primary group-hover:scale-110 transition-transform shadow-neon-glow"
                          ></sl-icon>
                          <div>
                            <span class="block text-text-main font-bold text-sm"
                              >OAuth Flow</span
                            >
                            <span class="block text-text-muted text-xs mt-1"
                              >Quick connect via browser</span
                            >
                          </div>
                        </button>
                        <button
                          class="flex flex-col items-center justify-center p-6 rounded-lg border border-white/10 bg-white/5 hover:bg-primary/5 hover:border-primary/50 transition-all group gap-3 text-center cursor-pointer"
                        >
                          <sl-icon
                            name="plug"
                            class="text-3xl text-text-muted group-hover:text-primary transition-colors"
                          ></sl-icon>
                          <div>
                            <span class="block text-text-main font-bold text-sm"
                              >Manual Config</span
                            >
                            <span class="block text-text-muted text-xs mt-1"
                              >Use existing API keys</span
                            >
                          </div>
                        </button>
                      </div>
                    </div>
                  `
                : ''
            }

            <!-- API Gateway Settings -->
            ${
              this.activeTab === 'gateway'
                ? html`
                    <div class="glass-panel rounded-xl p-8 flex flex-col gap-6">
                      <div class="flex items-center gap-3">
                        <sl-icon
                          name="key"
                          class="text-primary text-xl"
                        ></sl-icon>
                        <h3
                          class="text-text-main font-display font-semibold text-lg tracking-wide m-0"
                        >
                          Gateway API Keys
                        </h3>
                      </div>

                      <div
                        class="bg-surface-base/50 border border-white/10 rounded-lg p-4 flex flex-col gap-4"
                      >
                        <div
                          class="flex flex-col md:flex-row md:items-center gap-4"
                        >
                          <div class="flex-1 flex flex-col gap-1">
                            <label
                              class="text-[10px] font-bold tracking-widest text-text-muted uppercase"
                              >Global Gateway Key</label
                            >
                            <div class="flex items-center gap-2">
                              <code class="font-mono text-sm text-text-main"
                                >pl_test_••••••••••••••••••••••••••••</code
                              >
                            </div>
                          </div>
                          <div
                            class="hidden md:block h-10 w-px bg-white/10"
                          ></div>
                          <div
                            class="flex-shrink-0 md:text-right flex justify-between md:flex-col items-center md:items-end"
                          >
                            <span
                              class="block text-[10px] font-bold tracking-widest text-success uppercase"
                              >Active</span
                            >
                            <sl-button
                              variant="danger"
                              size="small"
                              outline
                              class="mt-1"
                              >Revoke Key</sl-button
                            >
                          </div>
                        </div>
                      </div>

                      <div
                        class="p-4 rounded-lg bg-danger/5 border border-danger/20 flex items-start gap-3"
                      >
                        <sl-icon
                          name="info-circle-fill"
                          class="text-danger mt-0.5 text-lg"
                        ></sl-icon>
                        <p class="text-xs text-text-muted leading-relaxed m-0">
                          <strong class="text-text-main"
                            >Security Protocol:</strong
                          >
                          Each agent requires a unique API key for gateway
                          routing. Do not reuse keys across different workforce
                          units. Use the
                          <code class="text-primary">preloop keys create</code>
                          command to generate scoped credentials.
                        </p>
                      </div>
                    </div>
                  `
                : ''
            }
          </div>

          <!-- Action Footer -->
          <div
            class="flex justify-between items-center mt-4 border-t border-white/10 pt-8"
          >
            <a
              href="https://docs.preloop.ai"
              target="_blank"
              class="text-text-muted hover:text-text-main flex items-center gap-2 transition-colors font-display text-sm font-bold tracking-widest no-underline"
            >
              <sl-icon name="book"></sl-icon> DEVELOPER DOCS
            </a>
            <div class="flex gap-4">
              <sl-button
                href="/console/agents"
                variant="primary"
                class="shadow-glow-primary"
              >
                PROCEED TO CANVAS
                <sl-icon slot="suffix" name="arrow-right"></sl-icon>
              </sl-button>
            </div>
          </div>
        </div>
      </div>
    `;
  }
}
