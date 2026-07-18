import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import { getAIModels } from '../api';
import type { AIModel } from '../types';
import {
  pickDefaultModel,
  selectableModels,
} from '../utils/ai-model-selection';
import './add-ai-model-modal';

@customElement('preloop-agent-deployer')
export class PreloopAgentDeployer extends LitElement {
  static styles = css`
    :host {
      display: block;
      width: 100%;
    }

    .deploy-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: var(--sl-spacing-large);
    }

    .inner-deploy-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: var(--sl-spacing-medium);
    }

    @media (max-width: 768px) {
      .deploy-grid,
      .inner-deploy-grid {
        grid-template-columns: 1fr;
      }
    }

    .wizard-shell {
      width: 100%;
      max-width: 920px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: var(--sl-spacing-large);
      color: var(--sl-color-neutral-800);
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

    .wizard-back {
      align-self: flex-start;
      margin-left: calc(-1 * var(--sl-spacing-small));
    }

    .wizard-actions {
      display: flex;
      justify-content: flex-end;
      gap: var(--sl-spacing-medium);
      margin-top: var(--sl-spacing-large);
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

    @media (max-width: 640px) {
      .wizard-option-body,
      .command-row {
        flex-direction: column;
        align-items: stretch;
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

  @property({ type: Boolean, attribute: 'hide-back-button' })
  hideBackButton = false;

  @state()
  private deploySubStep:
    | 'agent-host'
    | 'existing-host-method'
    | 'ssh-config'
    | 'cli-install'
    | 'fresh-vm-premium' = 'agent-host';

  @state()
  private isBooting = false;

  @state()
  private bootLogs: string[] = [];

  @state()
  private sshHost = '';

  @state()
  private sshUsername = '';

  @state()
  private sshPort = '22';

  @state()
  private sshAuthType: 'password' | 'key' = 'password';

  @state()
  private sshPassword = '';

  @state()
  private sshPrivateKey = '';

  @state()
  private deployAgentType: 'hermes' | 'openclaw' = 'hermes';

  @state()
  private deployModel = '';

  @state()
  private deployComputeSize = 'standard';

  @state()
  private deployEnableVnc = false;

  @state()
  private isAddingAIModel = false;

  @state()
  private showComputeSetupHelp = false;

  @state()
  private showComputeAdminNotice = false;

  @state()
  private showComputePromo = false;

  async connectedCallback() {
    super.connectedCallback();
    if (this.aiModels.length === 0) {
      this.aiModels = await getAIModels().catch(() => []);
    }
    if (!this.deployModel) {
      // Never auto-select a principal-bound OAuth model: it cannot serve
      // server-side generation and would fail on first use.
      this.deployModel = pickDefaultModel(this.aiModels)?.id || '';
    }
  }

  private handleAIModelAdded() {
    this.isAddingAIModel = false;
    void getAIModels().then((models) => {
      this.aiModels = models;
      // Prefer the model the user just added, but only if Preloop can
      // actually generate with it; otherwise fall back to the standard rule.
      const usable = selectableModels(models);
      this.deployModel =
        usable[usable.length - 1]?.id || pickDefaultModel(models)?.id || '';
      this.requestUpdate();
    });
  }

  private preloopCliInstallCommand(): string {
    return window.location.hostname === 'preloop.ai'
      ? 'curl -fsSL https://preloop.ai/install/cli | sh'
      : `export PRELOOP_URL=${window.location.origin} && curl -fsSL https://preloop.ai/install/cli | sh`;
  }

  private preloopLoginCommand(): string {
    return window.location.hostname === 'preloop.ai'
      ? 'preloop login'
      : `export PRELOOP_URL=${window.location.origin} && preloop login`;
  }

  private runtimeInstallCommand(agentType: 'hermes' | 'openclaw'): string {
    return agentType === 'hermes'
      ? 'pipx install hermes-agent'
      : 'npm install -g openclaw@latest';
  }

  private runtimeOnboardCommand(agentType: 'hermes' | 'openclaw'): string {
    const agentName = agentType === 'hermes' ? 'hermes' : 'openclaw';
    const base = `preloop agents onboard ${agentName} -y`;
    if (window.location.hostname === 'preloop.ai') {
      return base;
    }
    return `export PRELOOP_URL=${window.location.origin} && ${base}`;
  }

  private runtimeInstallAndOnboardCommand(
    agentType: 'hermes' | 'openclaw'
  ): string {
    const base = `preloop agents install-runtime ${agentType} -y`;
    if (window.location.hostname === 'preloop.ai') {
      return base;
    }
    return `export PRELOOP_URL=${window.location.origin} && ${base}`;
  }

  private handleFreshVmSelection() {
    if (this.computeFeatureEnabled) {
      this.deploySubStep = 'fresh-vm-premium';
    } else {
      if (this.isEnterprise) {
        if (this.isAdmin) {
          this.showComputeSetupHelp = true;
        } else {
          this.showComputeAdminNotice = true;
        }
      } else {
        this.showComputePromo = true;
      }
    }
    this.requestUpdate();
  }

  private startDeployBootSequence() {
    this.isBooting = true;
    this.bootLogs = [];
    const selectedModel = this.aiModels.find((m) => m.id === this.deployModel);
    const modelName = selectedModel ? selectedModel.name : this.deployModel;
    const logs = [
      '[system] Initializing virtual environment metadata...',
      `[system] Provisioning secure cloud container for agent ${this.deployAgentType.toUpperCase()} (Compute Size: ${this.deployComputeSize.toUpperCase()})...`,
      '[system] Mounting 20GB encrypted sandboxed virtual volume...',
      '[system] Booting Debian Linux kernel environment...',
      '[system] Configuring network route interfaces and proxy gateways...',
      `[system] Injecting secure Preloop Audit Firewall & LLM proxy credentials for model ${modelName.toUpperCase()}...`,
      `[system] Downloading & starting ${this.deployAgentType} autonomous micro-service runtime...`,
      '[system] Handshaking and establishing low-latency telemetry channel with Preloop API Gateway...',
      'SUCCESS: Autonomous Agent Node fully provisioned and securely active!',
    ];

    let index = 0;
    const addLog = () => {
      if (index < logs.length) {
        this.bootLogs = [...this.bootLogs, logs[index]];
        index++;
        this.requestUpdate();
        setTimeout(addLog, 600);
      } else {
        const mockAgent = {
          id: 'agent-vm-' + Math.random().toString(36).substr(2, 9),
          display_name: 'Secure VM ' + this.deployAgentType.toUpperCase(),
          agent_kind: this.deployAgentType,
          session_source_type: 'kube_virt',
          session_source_id: 'vm-' + this.deployAgentType + '-01',
          session_reference: 'Preloop VM Instance',
          enrolled_via: 'kube_virt',
          lifecycle_state: 'active',
          last_seen_at: new Date().toISOString(),
          tags: {
            compute: 'kube_virt',
            vnc: this.deployEnableVnc ? 'true' : 'false',
            size: this.deployComputeSize,
            model: this.deployModel,
          },
        };

        this.dispatchEvent(
          new CustomEvent('deploy-agent-success', {
            bubbles: true,
            composed: true,
            detail: { agent: mockAgent },
          })
        );
      }
    };
    addLog();
  }

  private startSshDeployBootSequence() {
    this.isBooting = true;
    this.bootLogs = [];
    const selectedModel = this.aiModels.find((m) => m.id === this.deployModel);
    const modelName = selectedModel ? selectedModel.name : this.deployModel;
    const logs = [
      `[ssh] Connecting to target host ${this.sshHost}:${this.sshPort}...`,
      `[ssh] Authorized successfully as user "${this.sshUsername}"...`,
      '[ssh] Validating base Linux dependencies (Python 3, Docker/Podman)...',
      '[ssh] Creating sandboxed operational root /opt/preloop-agent...',
      `[ssh] Downloading latest governed agent ${this.deployAgentType.toUpperCase()} bundle...`,
      '[ssh] Installing secure Preloop governance policies & firewalls...',
      `[ssh] Handshaking secure proxy gateway for model ${modelName.toUpperCase()}...`,
      'SUCCESS: Persistent governed agent node successfully activated via SSH!',
    ];

    let index = 0;
    const addLog = () => {
      if (index < logs.length) {
        this.bootLogs = [...this.bootLogs, logs[index]];
        index++;
        this.requestUpdate();
        setTimeout(addLog, 600);
      } else {
        const mockAgent = {
          id: 'agent-ssh-' + Math.random().toString(36).substr(2, 9),
          display_name: 'SSH Governed ' + this.deployAgentType.toUpperCase(),
          agent_kind: this.deployAgentType,
          session_source_type: 'ssh',
          session_source_id: 'ssh-' + this.sshUsername + '@' + this.sshHost,
          session_reference: 'Preloop SSH Governed Host',
          enrolled_via: 'ssh',
          lifecycle_state: 'active',
          last_seen_at: new Date().toISOString(),
          tags: {
            compute: 'ssh',
            host: this.sshHost,
            model: this.deployModel,
          },
        };

        this.dispatchEvent(
          new CustomEvent('deploy-agent-success', {
            bubbles: true,
            composed: true,
            detail: { agent: mockAgent },
          })
        );
      }
    };
    addLog();
  }

  private handleBack() {
    if (this.deploySubStep === 'agent-host') {
      this.dispatchEvent(
        new CustomEvent('deploy-cancel', { bubbles: true, composed: true })
      );
    } else if (
      this.deploySubStep === 'ssh-config' ||
      this.deploySubStep === 'cli-install'
    ) {
      this.deploySubStep = 'existing-host-method';
    } else if (this.deploySubStep === 'existing-host-method') {
      this.deploySubStep = 'agent-host';
    } else if (this.deploySubStep === 'fresh-vm-premium') {
      this.deploySubStep = 'agent-host';
    }
    this.requestUpdate();
  }

  render() {
    if (this.isBooting) {
      return this.renderSimulatedBoot();
    }

    return html`
      <div style="width: 100%;">
        <div class="wizard-shell">
          ${
            !this.hideBackButton
              ? html`
                  <sl-button
                    class="wizard-back"
                    variant="text"
                    size="small"
                    @click=${this.handleBack}
                  >
                    <sl-icon name="arrow-left" slot="prefix"></sl-icon> Back
                  </sl-button>
                `
              : nothing
          }
          ${
            this.deploySubStep === 'agent-host'
              ? html`
                  <div class="wizard-header">
                    <h3 class="wizard-title">Choose Agent Hosting Target</h3>
                    <p class="wizard-copy">
                      Where would you like to host this persistent agent?
                    </p>
                  </div>

                  <div class="wizard-card-grid">
                    <sl-button
                      class="wizard-option-button"
                      variant="default"
                      @click=${() => {
                        this.deploySubStep = 'existing-host-method';
                        this.requestUpdate();
                      }}
                    >
                      <div class="wizard-option-body">
                        <span class="wizard-option-icon">
                          <sl-icon name="hdd-network"></sl-icon>
                        </span>
                        <span class="wizard-option-copy">
                          <span class="wizard-option-title">
                            Deploy on Existing Host
                          </span>
                          <span class="wizard-option-description">
                            Install a Hermes or OpenClaw agent on a machine you
                            control using SSH or the Preloop CLI.
                          </span>
                        </span>
                      </div>
                    </sl-button>

                    ${
                      this.isEnterprise
                        ? html`
                            <sl-button
                              class="wizard-option-button"
                              variant="default"
                              @click=${this.handleFreshVmSelection}
                            >
                              <div class="wizard-option-body">
                                <span class="wizard-option-icon">
                                  <sl-icon name="cpu"></sl-icon>
                                </span>
                                <span class="wizard-option-copy">
                                  <span class="wizard-option-title">
                                    Deploy on Fresh VM (Cloud)
                                  </span>
                                  <span class="wizard-option-description">
                                    Provision a brand new isolated VM instance
                                    managed by Preloop compute backends.
                                  </span>
                                </span>
                              </div>
                            </sl-button>
                          `
                        : nothing
                    }
                  </div>
                `
              : nothing
          }
          ${
            this.deploySubStep === 'existing-host-method'
              ? html`
                  <div class="wizard-header">
                    <h3 class="wizard-title">Deploy on Existing Host</h3>
                    <p class="wizard-copy">
                      Choose how Preloop should reach the host where the agent
                      will run.
                    </p>
                  </div>

                  <div class="wizard-card-grid">
                    <sl-button
                      class="wizard-option-button"
                      variant="default"
                      @click=${() => {
                        this.deploySubStep = 'ssh-config';
                        this.requestUpdate();
                      }}
                    >
                      <div class="wizard-option-body">
                        <span class="wizard-option-icon">
                          <sl-icon name="key"></sl-icon>
                        </span>
                        <span class="wizard-option-copy">
                          <span class="wizard-option-title">SSH Access</span>
                          <span class="wizard-option-description">
                            Preloop connects to the host over SSH and deploys
                            the agent. Requires a public IP or routable address
                            from your Preloop instance.
                          </span>
                        </span>
                      </div>
                    </sl-button>

                    <sl-button
                      class="wizard-option-button"
                      variant="default"
                      @click=${() => {
                        this.deploySubStep = 'cli-install';
                        this.requestUpdate();
                      }}
                    >
                      <div class="wizard-option-body">
                        <span class="wizard-option-icon">
                          <sl-icon name="terminal"></sl-icon>
                        </span>
                        <span class="wizard-option-copy">
                          <span class="wizard-option-title">
                            Install with Preloop CLI
                          </span>
                          <span class="wizard-option-description">
                            Run commands on the host to install Hermes or
                            OpenClaw and onboard through Preloop. Works when the
                            host only has outbound access to Preloop.
                          </span>
                        </span>
                      </div>
                    </sl-button>
                  </div>
                `
              : nothing
          }
          ${
            this.deploySubStep === 'cli-install'
              ? html`
                  <div class="wizard-header">
                    <h3 class="wizard-title">
                      Install Agent Runtime with Preloop CLI
                    </h3>
                    <p class="wizard-copy">
                      Run these commands on the host where the agent should run.
                      The agent connects outbound to Preloop, so inbound SSH
                      access from Preloop is not required.
                    </p>
                  </div>

                  <div class="wizard-panel">
                    <sl-select
                      label="Agent Runtime"
                      value=${this.deployAgentType}
                      @sl-change=${(e: any) => {
                        this.deployAgentType = e.target.value;
                        this.requestUpdate();
                      }}
                      style="margin-bottom: var(--sl-spacing-large);"
                    >
                      <sl-option value="hermes">Hermes</sl-option>
                      <sl-option value="openclaw">OpenClaw</sl-option>
                    </sl-select>

                    <div class="command-steps">
                      <div class="command-step">
                        <div class="command-label">
                          1. Install the Preloop CLI on the host
                        </div>
                        <div class="command-row">
                          <code class="command-code"
                            >${this.preloopCliInstallCommand()}</code
                          >
                          <sl-copy-button
                            .value=${this.preloopCliInstallCommand()}
                          ></sl-copy-button>
                        </div>
                      </div>

                      <div class="command-step">
                        <div class="command-label">2. Authenticate the CLI</div>
                        <div class="command-row">
                          <code class="command-code"
                            >${this.preloopLoginCommand()}</code
                          >
                          <sl-copy-button
                            .value=${this.preloopLoginCommand()}
                          ></sl-copy-button>
                        </div>
                      </div>

                      <div class="command-step">
                        <div class="command-label">
                          3. Install
                          ${
                            this.deployAgentType === 'hermes'
                              ? 'Hermes'
                              : 'OpenClaw'
                          }
                          and onboard through Preloop
                        </div>
                        <div class="command-row">
                          <code class="command-code"
                            >${this.runtimeInstallAndOnboardCommand(
                              this.deployAgentType
                            )}</code
                          >
                          <sl-copy-button
                            .value=${this.runtimeInstallAndOnboardCommand(
                              this.deployAgentType
                            )}
                          ></sl-copy-button>
                        </div>
                      </div>

                      <div class="command-step">
                        <div class="command-label">
                          Or run the steps separately
                        </div>
                        <div class="command-row">
                          <code class="command-code"
                            >${this.runtimeInstallCommand(
                              this.deployAgentType
                            )}</code
                          >
                          <sl-copy-button
                            .value=${this.runtimeInstallCommand(
                              this.deployAgentType
                            )}
                          ></sl-copy-button>
                        </div>
                        <div class="command-row">
                          <code class="command-code"
                            >${this.runtimeOnboardCommand(
                              this.deployAgentType
                            )}</code
                          >
                          <sl-copy-button
                            .value=${this.runtimeOnboardCommand(
                              this.deployAgentType
                            )}
                          ></sl-copy-button>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div class="wizard-actions">
                    <sl-button
                      variant="default"
                      @click=${() =>
                        (this.deploySubStep = 'existing-host-method')}
                      >Back</sl-button
                    >
                  </div>
                `
              : nothing
          }
          ${
            this.deploySubStep === 'ssh-config'
              ? html`
                  <div class="wizard-header">
                    <h3 class="wizard-title">Deploy via SSH</h3>
                    <p class="wizard-copy">
                      Provide SSH credentials for the host where the agent
                      should run. Preloop must be able to connect to this
                      address.
                    </p>
                  </div>

                  <div class="wizard-panel deploy-grid">
                    <div
                      style="display: flex; flex-direction: column; gap: var(--sl-spacing-medium);"
                    >
                      <sl-input
                        label="Host Address / IP"
                        placeholder="e.g. 192.168.1.100"
                        .value=${this.sshHost}
                        @sl-input=${(e: any) => (this.sshHost = e.target.value)}
                      ></sl-input>
                      <sl-input
                        label="SSH Username"
                        placeholder="e.g. ubuntu"
                        .value=${this.sshUsername}
                        @sl-input=${(e: any) =>
                          (this.sshUsername = e.target.value)}
                      ></sl-input>
                      <sl-input
                        label="SSH Port"
                        placeholder="22"
                        .value=${this.sshPort}
                        @sl-input=${(e: any) => (this.sshPort = e.target.value)}
                      ></sl-input>
                    </div>

                    <div
                      style="display: flex; flex-direction: column; gap: var(--sl-spacing-medium);"
                    >
                      <div style="margin-bottom: var(--sl-spacing-small);">
                        <label
                          style="display: block; margin-bottom: 0.5rem; font-weight: 500; font-size: 0.875rem;"
                          >Authentication Method</label
                        >
                        <sl-radio-group
                          value=${this.sshAuthType}
                          @sl-change=${(e: any) => {
                            this.sshAuthType = e.target.value;
                            this.requestUpdate();
                          }}
                          style="display: flex; gap: var(--sl-spacing-medium);"
                        >
                          <sl-radio value="password">Password</sl-radio>
                          <sl-radio value="key">Private Key</sl-radio>
                        </sl-radio-group>
                      </div>

                      ${
                        this.sshAuthType === 'password'
                          ? html`
                              <sl-input
                                type="password"
                                label="SSH Password"
                                placeholder="Enter SSH password"
                                password-toggle
                                .value=${this.sshPassword}
                                @sl-input=${(e: any) =>
                                  (this.sshPassword = e.target.value)}
                              ></sl-input>
                            `
                          : html`
                              <sl-textarea
                                label="SSH Private Key"
                                placeholder="Paste your SSH Private Key here..."
                                rows="4"
                                .value=${this.sshPrivateKey}
                                @sl-input=${(e: any) =>
                                  (this.sshPrivateKey = e.target.value)}
                              ></sl-textarea>
                            `
                      }

                      <div class="inner-deploy-grid">
                        <sl-select
                          label="Agent Runtime Kind"
                          value=${this.deployAgentType}
                          @sl-change=${(e: any) =>
                            (this.deployAgentType = e.target.value)}
                        >
                          <sl-option value="hermes">Hermes</sl-option>
                          <sl-option value="openclaw">OpenClaw</sl-option>
                        </sl-select>

                        <div
                          style="display: flex; flex-direction: column; gap: var(--sl-spacing-2x-small);"
                        >
                          <sl-select
                            label="AI Model"
                            value=${this.deployModel}
                            @sl-change=${(e: any) =>
                              (this.deployModel = e.target.value)}
                            style="margin-bottom: 0; width: 100%;"
                          >
                            ${this.aiModels
                              .filter(
                                (m) =>
                                  m.model_kind !== 'stt' &&
                                  m.model_kind !== 'tts'
                              )
                              .map(
                                (m) =>
                                  html`<sl-option .value=${m.id}
                                    >${m.name}</sl-option
                                  >`
                              )}
                          </sl-select>
                          <sl-button
                            size="small"
                            variant="text"
                            @click=${() => (this.isAddingAIModel = true)}
                            style="align-self: flex-start; margin-top: -0.25rem; height: auto; padding: 0;"
                          >
                            <sl-icon slot="prefix" name="plus-lg"></sl-icon> Add
                            New AI Model
                          </sl-button>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div class="wizard-actions">
                    <sl-button
                      variant="default"
                      @click=${() =>
                        (this.deploySubStep = 'existing-host-method')}
                      >Back</sl-button
                    >
                    <sl-button
                      variant="primary"
                      ?disabled=${
                        !this.sshHost ||
                        !this.sshUsername ||
                        (this.sshAuthType === 'password'
                          ? !this.sshPassword
                          : !this.sshPrivateKey)
                      }
                      @click=${this.startSshDeployBootSequence}
                      >Deploy Agent</sl-button
                    >
                  </div>
                `
              : nothing
          }
          ${
            this.deploySubStep === 'fresh-vm-premium'
              ? html`
                  <div class="wizard-header">
                    <h3 class="wizard-title">
                      Provision Secure Cloud Agent VM
                    </h3>
                  </div>

                  <div class="wizard-panel deploy-grid">
                    <div
                      style="display: flex; flex-direction: column; gap: var(--sl-spacing-medium);"
                    >
                      <sl-select
                        label="Agent Runtime Kind"
                        value=${this.deployAgentType}
                        @sl-change=${(e: any) =>
                          (this.deployAgentType = e.target.value)}
                      >
                        <sl-option value="hermes">Hermes</sl-option>
                        <sl-option value="openclaw">OpenClaw</sl-option>
                      </sl-select>

                      <sl-select
                        label="Compute Sandbox Size"
                        value=${this.deployComputeSize}
                        @sl-change=${(e: any) =>
                          (this.deployComputeSize = e.target.value)}
                      >
                        <sl-option value="standard"
                          >Standard (2 vCPU, 4GB RAM)</sl-option
                        >
                        <sl-option value="performance"
                          >Performance (4 vCPU, 8GB RAM)</sl-option
                        >
                        <sl-option value="high-mem"
                          >High Memory (8 vCPU, 16GB RAM)</sl-option
                        >
                      </sl-select>
                    </div>

                    <div
                      style="display: flex; flex-direction: column; gap: var(--sl-spacing-medium);"
                    >
                      <sl-select
                        label="AI Model"
                        value=${this.deployModel}
                        @sl-change=${(e: any) =>
                          (this.deployModel = e.target.value)}
                      >
                        ${this.aiModels
                          .filter(
                            (m) =>
                              m.model_kind !== 'stt' && m.model_kind !== 'tts'
                          )
                          .map(
                            (m) =>
                              html`<sl-option .value=${m.id}
                                >${m.name}</sl-option
                              >`
                          )}
                      </sl-select>

                      <div style="margin-top: var(--sl-spacing-medium);">
                        <sl-checkbox
                          ?checked=${this.deployEnableVnc}
                          @sl-change=${(e: any) =>
                            (this.deployEnableVnc = e.target.checked)}
                        >
                          Enable VNC Graphical Desktop access
                        </sl-checkbox>
                      </div>
                    </div>
                  </div>

                  <div class="wizard-actions">
                    <sl-button
                      variant="default"
                      @click=${() => (this.deploySubStep = 'agent-host')}
                      >Back</sl-button
                    >
                    <sl-button
                      variant="primary"
                      @click=${this.startDeployBootSequence}
                      >Provision VM Agent Node</sl-button
                    >
                  </div>
                `
              : nothing
          }
        </div>

        <!-- Compute Backends Support Dialogs -->
        <sl-dialog
          label="Setup Compute Backend"
          ?open=${this.showComputeSetupHelp}
          @sl-after-hide=${() => (this.showComputeSetupHelp = false)}
        >
          <div
            style="font-size: 0.9rem; line-height: 1.5; color: var(--sl-color-neutral-700);"
          >
            <p>
              <strong
                >AWS, GCP, or KubeVirt VM compute backends are not
                configured.</strong
              >
            </p>
            <p>
              To enable direct agent deployments to VMs in KubeVirt or Cloud
              providers, please set the compute configurations on your server
              environment variables or in the organization configurations.
            </p>
            <p>
              Refer to the
              <a
                href="file:///Users/dimo/git/spacecode/preloop-ee/README.md#🖥️-configuring-compute-backends-aws-gcp-kubevirt"
                style="color: var(--sl-color-primary-600); font-weight: 600;"
                >README.md instructions</a
              >
              for details.
            </p>
          </div>
          <sl-button
            slot="footer"
            variant="primary"
            @click=${() => (this.showComputeSetupHelp = false)}
            >Got it</sl-button
          >
        </sl-dialog>

        <sl-dialog
          label="Provision VM Notice"
          ?open=${this.showComputeAdminNotice}
          @sl-after-hide=${() => (this.showComputeAdminNotice = false)}
        >
          <div
            style="font-size: 0.9rem; line-height: 1.5; color: var(--sl-color-neutral-700);"
          >
            <p>
              Compute backends are not configured for this account. Please ask
              an Administrator to configure direct VM compute backends in
              settings.
            </p>
          </div>
          <sl-button
            slot="footer"
            variant="primary"
            @click=${() => (this.showComputeAdminNotice = false)}
            >Close</sl-button
          >
        </sl-dialog>

        <sl-dialog
          label="Unlock Cloud VM Provisioning"
          ?open=${this.showComputePromo}
          @sl-after-hide=${() => (this.showComputePromo = false)}
        >
          <div style="text-align: center; padding: var(--sl-spacing-medium);">
            <sl-icon
              name="cpu"
              style="font-size: 3rem; color: var(--sl-color-primary-500); margin-bottom: var(--sl-spacing-medium);"
            ></sl-icon>
            <h3 style="margin: 0 0 var(--sl-spacing-small) 0;">
              Cloud VM Compute Backends
            </h3>
            <p
              style="font-size: 0.9rem; color: var(--sl-color-neutral-600); line-height: 1.5; margin-bottom: var(--sl-spacing-large);"
            >
              Provisioning secure virtual machines in the cloud is an Enterprise
              Edition feature. Connect with our team to upgrade your workspace.
            </p>
            <sl-button
              variant="primary"
              href="mailto:sales@preloop.ai?subject=Preloop%20Enterprise%20Compute%20Backend"
              target="_blank"
              style="width: 100%;"
            >
              Contact Sales
            </sl-button>
          </div>
        </sl-dialog>

        <add-ai-model-modal
          ?open=${this.isAddingAIModel}
          @close=${() => {
            this.isAddingAIModel = false;
          }}
          @model-added=${() => this.handleAIModelAdded()}
        ></add-ai-model-modal>
      </div>
    `;
  }

  private renderSimulatedBoot() {
    return html`
      <div style="width: 100%;">
        <h3
          style="font-size: var(--sl-font-size-large); font-weight: 600; color: var(--sl-color-neutral-800); margin: 0 0 var(--sl-spacing-medium) 0; display: flex; align-items: center; gap: 8px;"
        >
          <sl-spinner style="font-size: 1rem;"></sl-spinner>
          Deploying Secure Agent Node Environment...
        </h3>

        <div
          style="
            background: #0f172a;
            border-radius: var(--sl-border-radius-medium);
            padding: var(--sl-spacing-large);
            height: 280px;
            overflow-y: auto;
            font-family: var(--sl-font-mono);
            font-size: 0.85rem;
            color: #38bdf8;
            box-shadow: inset 0 2px 8px rgba(0,0,0,0.8);
          "
        >
          ${this.bootLogs.map((log) => {
            const isSuccess = log.startsWith('SUCCESS');
            return html`<div
              style="margin-bottom: 6px; line-height: 1.4; color: ${
                isSuccess ? '#4ade80' : '#38bdf8'
              }; font-weight: ${isSuccess ? 'bold' : 'normal'};"
            >
              ${log}
            </div>`;
          })}
          ${
            this.bootLogs.length < 8 &&
            !this.bootLogs[this.bootLogs.length - 1]?.startsWith('SUCCESS')
              ? html`<div style="color: #38bdf8; animation: pulse 1s infinite;">
                  _
                </div>`
              : html`
                  <div
                    style="margin-top: var(--sl-spacing-large); display: flex; justify-content: flex-end;"
                  >
                    <sl-button
                      variant="success"
                      size="small"
                      @click=${() => {
                        this.isBooting = false;
                        this.dispatchEvent(
                          new CustomEvent('deploy-wizard-done', {
                            bubbles: true,
                            composed: true,
                          })
                        );
                      }}
                    >
                      View Connected Agent Node
                    </sl-button>
                  </div>
                `
          }
        </div>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'preloop-agent-deployer': PreloopAgentDeployer;
  }
}
