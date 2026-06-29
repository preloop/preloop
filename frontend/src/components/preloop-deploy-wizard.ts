import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import {
  getAIModels,
  createFlow,
  createManagedAgent,
  createManagedAgentCredential,
} from '../api';
import type { AIModel } from '../types';
import './preloop-flow-form';
import './preloop-agent-deployer';

type OnboardingPath = 'choose' | 'cli' | 'deploy' | 'custom';

// Per-run session header the gateway maps a LangGraph thread_id onto. This MUST
// match the header name the gateway implementation reads. If the gateway team
// changes it, update this constant (and the snippet rendered below).
const PRELOOP_SESSION_HEADER = 'X-Preloop-Session-Id';

@customElement('preloop-deploy-wizard')
export class PreloopDeployWizard extends LitElement {
  static styles = css`
    :host {
      display: block;
      width: 100%;
    }

    .wizard-shell {
      width: 100%;
      max-width: 820px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-large);
      color: var(--sl-color-neutral-800);
    }

    .wizard-shell.wide {
      max-width: 920px;
    }

    .wizard-header {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-2x-small);
    }

    .wizard-title {
      color: var(--sl-color-neutral-900);
      font-size: var(--sl-font-size-large);
      font-weight: var(--sl-font-weight-semibold);
      line-height: 1.25;
      margin: 0;
    }

    .wizard-copy {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-medium);
      line-height: 1.55;
      margin: 0;
    }

    .wizard-card-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: var(--sl-spacing-large);
      width: 100%;
    }

    .wizard-option-button {
      display: block;
      width: 100%;
      height: 100%;
    }

    .wizard-option-button::part(base) {
      width: 100%;
      height: auto;
      min-height: 132px;
      padding: var(--sl-spacing-large);
      justify-content: flex-start;
      text-align: left;
      align-items: center;
      text-wrap: wrap;
    }

    .wizard-option-body {
      display: flex;
      align-items: flex-start;
      gap: var(--sl-spacing-medium);
    }

    .wizard-option-icon {
      width: 44px;
      height: 44px;
      border-radius: var(--sl-border-radius-large);
      color: var(--sl-color-primary-600);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
    }

    .wizard-option-icon sl-icon {
      font-size: 1.35rem;
    }

    .wizard-option-copy {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-2x-small);
      min-width: 0;
    }

    .wizard-option-title {
      color: var(--sl-color-neutral-900);
      font-size: var(--sl-font-size-medium);
      font-weight: var(--sl-font-weight-semibold);
      line-height: 1.3;
    }

    .wizard-option-description {
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
      font-weight: var(--sl-font-weight-normal);
      line-height: 1.45;
    }

    .wizard-panel {
      width: 100%;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-large);
      background: var(--sl-color-neutral-0);
      box-shadow: var(--sl-shadow-small);
      padding: var(--sl-spacing-large);
      box-sizing: border-box;
    }

    .command-steps {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-large);
    }

    .command-step {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-2x-small);
    }

    .command-label {
      color: var(--sl-color-neutral-800);
      font-weight: var(--sl-font-weight-semibold);
    }

    .command-row {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-small);
    }

    .command-code {
      flex: 1;
      min-width: 0;
      background: var(--sl-color-neutral-100);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium);
      color: var(--sl-color-neutral-800);
      font-family: var(--sl-font-mono);
      font-size: var(--sl-font-size-small);
      padding: var(--sl-spacing-small) var(--sl-spacing-medium);
      overflow-x: auto;
      white-space: nowrap;
    }

    .wizard-back {
      align-self: flex-start;
      margin-left: calc(-1 * var(--sl-spacing-small));
    }

    .custom-form {
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-medium);
    }

    .custom-error {
      width: 100%;
    }

    .command-snippet {
      margin: 0;
      white-space: pre;
      line-height: 1.5;
    }

    @media (max-width: 640px) {
      .wizard-option-body,
      .command-row {
        align-items: stretch;
        flex-direction: column;
      }
    }
  `;

  @property({ type: Array })
  aiModels: AIModel[] = [];

  @property({ type: Boolean })
  computeFeatureEnabled = false;

  @property({ type: Boolean })
  isEnterprise = false;

  @property({ type: Boolean })
  isAdmin = false;

  @property({ type: Boolean, attribute: 'hide-cancel' })
  hideCancel = false;

  @property({ type: String, attribute: 'initial-path' })
  initialPath: OnboardingPath = 'choose';

  @property({ type: Boolean, attribute: 'hide-back' })
  hideBack = false;

  @property({ type: Boolean, attribute: 'hide-step-title' })
  hideStepTitle = false;

  @state()
  private onboardingPath: OnboardingPath = 'choose';

  @state()
  private deploySubStep:
    | 'type'
    | 'agent-host'
    | 'ssh-config'
    | 'fresh-vm-premium'
    | 'flow-config' = 'type';

  // Custom-agent onboarding substeps:
  //  - 'name'   : collect display_name (+ optional description)
  //  - 'result' : show the minted credential token + copy-paste snippet ONCE
  @state()
  private customSubStep: 'name' | 'result' = 'name';

  @state()
  private customDisplayName = '';

  @state()
  private customDescription = '';

  @state()
  private customBusy = false;

  @state()
  private customError = '';

  // Populated after a successful register + mint. The token is shown ONCE.
  @state()
  private customAgentId: string | null = null;

  @state()
  private customCredentialToken: string | null = null;

  async connectedCallback() {
    super.connectedCallback();
    this.onboardingPath = this.initialPath;
    if (this.initialPath === 'deploy') {
      this.deploySubStep = 'type';
    }
    if (this.initialPath === 'custom') {
      this.resetCustomState();
    }
    if (this.aiModels.length === 0) {
      this.aiModels = await getAIModels().catch(() => []);
    }
  }

  updated(changedProperties: Map<string, unknown>) {
    super.updated(changedProperties);
    if (changedProperties.has('initialPath')) {
      this.onboardingPath = this.initialPath;
      if (this.initialPath === 'deploy') {
        this.deploySubStep = 'type';
      }
      if (this.initialPath === 'custom') {
        this.resetCustomState();
      }
    }
  }

  private handleAgentDeployCancel() {
    this.deploySubStep = 'type';
    this.requestUpdate();
  }

  private handleAgentDeploySuccess(e: CustomEvent) {
    this.dispatchEvent(
      new CustomEvent('deploy-agent-success', {
        bubbles: true,
        composed: true,
        detail: e.detail,
      })
    );
    // On agent success, we can fire wizard-done to close dialog/view
    this.dispatchEvent(
      new CustomEvent('deploy-wizard-done', {
        bubbles: true,
        composed: true,
      })
    );
  }

  private handleBack() {
    if (this.onboardingPath === 'choose') {
      this.dispatchEvent(
        new CustomEvent('deploy-cancel', { bubbles: true, composed: true })
      );
    } else if (this.onboardingPath === 'cli') {
      this.onboardingPath = 'choose';
    } else if (this.onboardingPath === 'deploy') {
      if (this.deploySubStep === 'type') {
        this.onboardingPath = 'choose';
      } else if (this.deploySubStep === 'flow-config') {
        this.deploySubStep = 'type';
      }
    } else if (this.onboardingPath === 'custom') {
      if (this.customSubStep === 'name') {
        // First substep -> back to the choose screen.
        this.onboardingPath = 'choose';
      } else if (this.customSubStep === 'result') {
        // Show-once guard: the credential token cannot be recovered. Confirm
        // before leaving the result screen.
        const confirmed = window.confirm(
          'The agent credential token is shown only once and cannot be ' +
            'recovered. Have you copied it somewhere safe? Leaving this screen ' +
            'will discard the token.'
        );
        if (!confirmed) {
          return;
        }
        // Discard the token from memory and return to the name form.
        this.customCredentialToken = null;
        this.customAgentId = null;
        this.customSubStep = 'name';
      }
    }
    this.requestUpdate();
  }

  private resetCustomState() {
    this.customSubStep = 'name';
    this.customDisplayName = '';
    this.customDescription = '';
    this.customBusy = false;
    this.customError = '';
    this.customAgentId = null;
    this.customCredentialToken = null;
  }

  private async handleCustomRegister() {
    const displayName = this.customDisplayName.trim();
    if (!displayName) {
      this.customError = 'Agent name is required.';
      return;
    }
    this.customBusy = true;
    this.customError = '';
    try {
      const description = this.customDescription.trim();
      // 1. Register the agent: POST /api/v1/agents
      const agent = await createManagedAgent({
        display_name: displayName,
        ...(description ? { description } : {}),
      });
      this.customAgentId = agent.id;

      // 2. Mint a gateway credential: POST /api/v1/agents/{id}/credentials
      // Scopes must be in RUNTIME_SESSION_ALLOWED_SCOPES (api/auth/router.py);
      // mcp:read/mcp:write authorize gateway model traffic. gateway:invoke is
      // rejected at mint time (HTTP 400).
      const result = await createManagedAgentCredential(agent.id, {
        name: `${displayName} gateway credential`,
        scopes: ['mcp:read', 'mcp:write'],
      });
      this.customCredentialToken = result.token;

      // 3. Show the result screen (token presented ONCE).
      this.customSubStep = 'result';
      this.dispatchEvent(
        new CustomEvent('deploy-custom-agent-success', {
          bubbles: true,
          composed: true,
          detail: { agent },
        })
      );
    } catch (error: any) {
      this.customError =
        error?.message || 'Failed to connect custom agent. Please try again.';
    } finally {
      this.customBusy = false;
    }
  }

  private buildGatewayBaseUrl(): string {
    // Mirror the installCommand hostname-switch pattern. On the hosted product
    // the gateway lives on the same origin; for self-hosted/dev we derive it
    // from window.location.origin. The OpenAI-compatible gateway is mounted at
    // /openai/v1 (SDKs append /chat/completions).
    return window.location.hostname === 'preloop.ai'
      ? 'https://preloop.ai/openai/v1'
      : `${window.location.origin}/openai/v1`;
  }

  private buildCustomSnippet(baseUrl: string, token: string): string {
    return `from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

# Route all model traffic through Preloop's OpenAI-compatible gateway.
llm = ChatOpenAI(
    base_url="${baseUrl}",
    api_key="${token}",  # Preloop gateway credential (shown once)
    model="gpt-4o",
)

agent = create_react_agent(llm, tools=[])

# Pass a per-run session id so Preloop groups this run's traffic. The
# LangGraph thread_id maps to the ${PRELOOP_SESSION_HEADER} header. This
# header name MUST match the Preloop gateway implementation.
thread_id = "run-12345"
agent.invoke(
    {"messages": [{"role": "user", "content": "Hello"}]},
    config={
        "configurable": {"thread_id": thread_id},
        "metadata": {"headers": {"${PRELOOP_SESSION_HEADER}": thread_id}},
    },
)`;
  }

  render() {
    return html`
      <div style="width: 100%;">
        ${this.onboardingPath === 'choose'
          ? this.renderChoosePathState()
          : this.onboardingPath === 'cli'
            ? this.renderCliPathState()
            : this.onboardingPath === 'custom'
              ? this.renderCustomPathState()
              : this.renderDeployPathState()}
      </div>
    `;
  }

  private renderChoosePathState() {
    return html`
      <div class="wizard-shell">
        <div class="wizard-header" style="text-align: center;">
          <p class="wizard-copy">
            Preloop is the open source control plane for AI agents. Connect or
            deploy your first agent to begin.
          </p>
        </div>
        <div class="wizard-card-grid">
          <sl-button
            class="wizard-option-button"
            variant="default"
            @click=${() => (this.onboardingPath = 'cli')}
          >
            <div class="wizard-option-body">
              <span class="wizard-option-icon">
                <sl-icon name="shield-check"></sl-icon>
              </span>
              <span class="wizard-option-copy">
                <span class="wizard-option-title">Govern Existing Agents</span>
                <span class="wizard-option-description">
                  Connect local running agents via CLI
                </span>
              </span>
            </div>
          </sl-button>

          <sl-button
            class="wizard-option-button"
            variant="default"
            @click=${() => {
              this.onboardingPath = 'deploy';
              this.deploySubStep = 'type';
              this.requestUpdate();
            }}
          >
            <div class="wizard-option-body">
              <span class="wizard-option-icon">
                <sl-icon name="cloud-arrow-up"></sl-icon>
              </span>
              <span class="wizard-option-copy">
                <span class="wizard-option-title">Deploy New Agents</span>
                <span class="wizard-option-description">
                  Spin up new persistent agents or flows
                </span>
              </span>
            </div>
          </sl-button>

          <sl-button
            class="wizard-option-button"
            variant="default"
            @click=${() => {
              this.resetCustomState();
              this.onboardingPath = 'custom';
              this.requestUpdate();
            }}
          >
            <div class="wizard-option-body">
              <span class="wizard-option-icon">
                <sl-icon name="plug"></sl-icon>
              </span>
              <span class="wizard-option-copy">
                <span class="wizard-option-title">Connect a custom agent</span>
                <span class="wizard-option-description">
                  Onboard an existing agent (LangGraph, custom SDK) the CLI
                  can't discover
                </span>
              </span>
            </div>
          </sl-button>
        </div>
      </div>
    `;
  }

  private renderCliPathState() {
    const installCommand =
      window.location.hostname === 'preloop.ai'
        ? 'curl -fsSL https://preloop.ai/install/cli | sh'
        : `export PRELOOP_URL=${window.location.origin} && curl -fsSL https://preloop.ai/install/cli | sh`;
    const loginCommand =
      window.location.hostname === 'preloop.ai'
        ? 'preloop login'
        : `export PRELOOP_URL=${window.location.origin} && preloop login`;

    return html`
      <div class="wizard-shell">
        ${this.hideBack
          ? nothing
          : html`
              <sl-button
                class="wizard-back"
                variant="text"
                size="small"
                @click=${this.handleBack}
              >
                <sl-icon name="arrow-left" slot="prefix"></sl-icon> Back
              </sl-button>
            `}

        <div class="wizard-header">
          ${this.hideStepTitle
            ? nothing
            : html`
                <h3 class="wizard-title">
                  Onboard Existing Agent via Preloop CLI
                </h3>
              `}
          <p class="wizard-copy">
            Run these commands from the machine where your agents are installed.
          </p>
        </div>

        <div class="wizard-panel command-steps">
          <div class="command-step">
            <div class="command-label">1. Install the Preloop CLI tool</div>
            <div class="command-row">
              <code class="command-code">${installCommand}</code>
              <sl-copy-button .value=${installCommand}></sl-copy-button>
            </div>
          </div>

          <div class="command-step">
            <div class="command-label">2. Authenticate CLI session</div>
            <div class="command-row">
              <code class="command-code">${loginCommand}</code>
              <sl-copy-button .value=${loginCommand}></sl-copy-button>
            </div>
          </div>

          <div class="command-step">
            <div class="command-label">3. Discover and sync local agents</div>
            <div class="command-row">
              <code class="command-code">preloop agents discover</code>
              <sl-copy-button value="preloop agents discover"></sl-copy-button>
            </div>
          </div>
        </div>
      </div>
    `;
  }

  private renderCustomPathState() {
    return html`
      <div class="wizard-shell">
        ${this.hideBack
          ? nothing
          : html`
              <sl-button
                class="wizard-back"
                variant="text"
                size="small"
                @click=${this.handleBack}
              >
                <sl-icon name="arrow-left" slot="prefix"></sl-icon> Back
              </sl-button>
            `}
        ${this.customSubStep === 'name'
          ? this.renderCustomNameState()
          : this.renderCustomResultState()}
      </div>
    `;
  }

  private renderCustomNameState() {
    return html`
      <div class="wizard-header">
        ${this.hideStepTitle
          ? nothing
          : html`<h3 class="wizard-title">Connect a custom agent</h3>`}
        <p class="wizard-copy">
          Register an existing agent (LangGraph, custom SDK) the CLI can't
          discover. We'll mint a gateway credential so it can route model
          traffic through Preloop.
        </p>
      </div>

      <div class="wizard-panel custom-form">
        ${this.customError
          ? html`
              <sl-alert variant="danger" open class="custom-error">
                <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                ${this.customError}
              </sl-alert>
            `
          : nothing}

        <sl-input
          label="Agent name"
          name="display_name"
          placeholder="e.g. Support triage agent"
          required
          ?disabled=${this.customBusy}
          .value=${this.customDisplayName}
          @sl-input=${(e: Event) => {
            this.customDisplayName = (e.target as HTMLInputElement).value;
          }}
        ></sl-input>

        <sl-textarea
          label="Description (optional)"
          name="description"
          placeholder="What does this agent do?"
          rows="2"
          ?disabled=${this.customBusy}
          .value=${this.customDescription}
          @sl-input=${(e: Event) => {
            this.customDescription = (e.target as HTMLTextAreaElement).value;
          }}
        ></sl-textarea>

        <sl-button
          variant="primary"
          ?loading=${this.customBusy}
          ?disabled=${this.customBusy || !this.customDisplayName.trim()}
          @click=${this.handleCustomRegister}
        >
          Register agent &amp; mint credential
        </sl-button>
      </div>
    `;
  }

  private renderCustomResultState() {
    const baseUrl = this.buildGatewayBaseUrl();
    const token = this.customCredentialToken || '';
    const snippet = this.buildCustomSnippet(baseUrl, token);

    return html`
      <div class="wizard-header">
        ${this.hideStepTitle
          ? nothing
          : html`<h3 class="wizard-title">Your agent is connected</h3>`}
        <p class="wizard-copy">
          Point your agent at the Preloop gateway using the base URL and
          credential below, then route model traffic through it.
        </p>
      </div>

      <div class="wizard-panel command-steps">
        <sl-alert variant="warning" open>
          <sl-icon slot="icon" name="exclamation-triangle"></sl-icon>
          This credential token is shown only once and cannot be recovered. Copy
          it now and store it securely.
        </sl-alert>

        <div class="command-step">
          <div class="command-label">Gateway base URL (OpenAI-compatible)</div>
          <div class="command-row">
            <code class="command-code">${baseUrl}</code>
            <sl-copy-button .value=${baseUrl}></sl-copy-button>
          </div>
        </div>

        <div class="command-step">
          <div class="command-label">Gateway credential (api_key)</div>
          <div class="command-row">
            <code class="command-code">${token}</code>
            <sl-copy-button .value=${token}></sl-copy-button>
          </div>
        </div>

        <div class="command-step">
          <div class="command-label">
            Example: LangGraph + OpenAI SDK with a per-run session id
          </div>
          <div class="command-row">
            <pre class="command-code command-snippet">${snippet}</pre>
            <sl-copy-button .value=${snippet}></sl-copy-button>
          </div>
        </div>

        <sl-button
          variant="primary"
          @click=${() => {
            this.customCredentialToken = null;
            this.dispatchEvent(
              new CustomEvent('deploy-wizard-done', {
                bubbles: true,
                composed: true,
              })
            );
          }}
        >
          Done
        </sl-button>
      </div>
    `;
  }

  private renderDeployPathState() {
    if (this.deploySubStep !== 'type' && this.deploySubStep !== 'flow-config') {
      return html`
        <preloop-agent-deployer
          .aiModels=${this.aiModels}
          .computeFeatureEnabled=${this.computeFeatureEnabled}
          .isEnterprise=${this.isEnterprise}
          .isAdmin=${this.isAdmin}
          @deploy-agent-success=${this.handleAgentDeploySuccess}
          @deploy-cancel=${this.handleAgentDeployCancel}
        ></preloop-agent-deployer>
      `;
    }

    return html`
      <div class="wizard-shell wide">
        <sl-button
          class="wizard-back"
          variant="text"
          size="small"
          @click=${this.handleBack}
        >
          <sl-icon name="arrow-left" slot="prefix"></sl-icon> Back
        </sl-button>

        ${this.deploySubStep === 'type'
          ? html`
              <div class="wizard-header">
                <h3 class="wizard-title">Deploy New Agent or Flow</h3>
                <p class="wizard-copy">
                  Select which type of deployment fits your automation scenario.
                </p>
              </div>

              <div class="wizard-card-grid">
                <sl-button
                  class="wizard-option-button"
                  variant="default"
                  @click=${() => {
                    this.deploySubStep = 'agent-host';
                    this.requestUpdate();
                  }}
                >
                  <div class="wizard-option-body">
                    <span class="wizard-option-icon">
                      <sl-icon name="server"></sl-icon>
                    </span>
                    <span class="wizard-option-copy">
                      <span class="wizard-option-title">
                        Deploy Persistent Agent
                      </span>
                      <span class="wizard-option-description">
                        Deploy a dedicated, persistent long-running agent node
                        that stays active and ready to perform autonomous tasks.
                      </span>
                    </span>
                  </div>
                </sl-button>

                <sl-button
                  class="wizard-option-button"
                  variant="default"
                  @click=${() => {
                    this.deploySubStep = 'flow-config';
                    this.requestUpdate();
                  }}
                >
                  <div class="wizard-option-body">
                    <span class="wizard-option-icon">
                      <sl-icon name="diagram-3"></sl-icon>
                    </span>
                    <span class="wizard-option-copy">
                      <span class="wizard-option-title">
                        Configure Event-Driven Flow
                      </span>
                      <span class="wizard-option-description">
                        Configure a short-lived agent that is provisioned on
                        demand and decommissioned when execution completes.
                      </span>
                    </span>
                  </div>
                </sl-button>
              </div>
            `
          : nothing}
        ${this.deploySubStep === 'flow-config'
          ? html`
              <div class="wizard-header">
                <h3 class="wizard-title">
                  Configure Event-Driven Agentic Flow
                </h3>
              </div>
              <div class="wizard-panel">
                <preloop-flow-form
                  @flow-submit=${async (e: CustomEvent) => {
                    const payload = e.detail.flow;
                    try {
                      const newFlow = await createFlow(payload);
                      this.dispatchEvent(
                        new CustomEvent('deploy-flow-success', {
                          bubbles: true,
                          composed: true,
                          detail: { flow: newFlow },
                        })
                      );
                    } catch (error: any) {
                      const form = e.target as HTMLElement & {
                        formError?: string;
                      };
                      form.formError =
                        error?.message || 'Failed to create flow.';
                    }
                  }}
                  @flow-cancel=${() => {
                    this.deploySubStep = 'type';
                  }}
                ></preloop-flow-form>
              </div>
            `
          : nothing}
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'preloop-deploy-wizard': PreloopDeployWizard;
  }
}
