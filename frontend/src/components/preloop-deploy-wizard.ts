import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import { getAIModels, createFlow } from '../api';
import type { AIModel } from '../types';
import './preloop-flow-form';
import './preloop-agent-deployer';

type OnboardingPath = 'choose' | 'cli' | 'deploy';

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

  async connectedCallback() {
    super.connectedCallback();
    this.onboardingPath = this.initialPath;
    if (this.initialPath === 'deploy') {
      this.deploySubStep = 'type';
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
    }
    this.requestUpdate();
  }

  render() {
    return html`
      <div style="width: 100%;">
        ${this.onboardingPath === 'choose'
          ? this.renderChoosePathState()
          : this.onboardingPath === 'cli'
            ? this.renderCliPathState()
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
