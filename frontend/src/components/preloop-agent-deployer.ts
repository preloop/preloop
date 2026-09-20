import { LitElement, html, css, nothing, unsafeCSS } from 'lit';
import type { TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import { fetchWithAuth, getAIModels } from '../api';
import type { AIModel } from '../types';
import {
  pickDefaultModel,
  selectableModels,
} from '../utils/ai-model-selection';
import './add-ai-model-modal';
import { consoleDialogStyles } from '../styles/console-dialog';
import consoleStyles from '../styles/console-styles.css?inline';
import { deployWizardStyles } from '../styles/deploy-wizard';

@customElement('preloop-agent-deployer')
export class PreloopAgentDeployer extends LitElement {
  static styles = [
    unsafeCSS(consoleStyles),
    consoleDialogStyles,
    deployWizardStyles,
    css`
      /* The provisioning log is the one dark-on-light block in the flow: a
         stream of machine output the operator reads, not a surface of the
         console. It takes the page tone (the same exception the design makes
         for a copyable command block) instead of the hard-coded slate and
         cyan hexes this used to inline, so it follows the theme. */
      .boot-log {
        background: var(--console-page);
        border-radius: var(--sl-border-radius-medium);
        box-sizing: border-box;
        color: var(--sl-color-neutral-700);
        font-family: var(--sl-font-mono);
        font-size: var(--console-text-meta);
        line-height: 1.5;
        max-height: 17rem;
        overflow-wrap: anywhere;
        overflow-y: auto;
        padding: var(--sl-spacing-medium);
        width: 100%;
      }

      .boot-log-line.success {
        color: var(--sl-color-success-700);
        font-weight: 600;
      }

      .boot-title {
        align-items: center;
        display: flex;
        gap: var(--sl-spacing-x-small);
      }

      .boot-title sl-spinner {
        font-size: 0.875rem;
      }

      .dialog-copy {
        color: var(--sl-color-neutral-700);
        font-size: var(--console-text-body);
        line-height: 1.5;
        margin: 0 0 var(--sl-spacing-small);
      }

      .dialog-copy:last-child {
        margin-bottom: 0;
      }

      .promo {
        align-items: center;
        display: flex;
        flex-direction: column;
        gap: var(--sl-spacing-small);
        text-align: center;
      }

      .promo sl-icon {
        color: var(--sl-color-primary-600);
        font-size: 1.75rem;
      }

      .promo-title {
        font-size: var(--console-text-card-title);
        font-weight: 600;
        margin: 0;
      }
    `,
  ];

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

  /**
   * Steps the host already walked before this component took over the screen,
   * so "Step 3 of 3" keeps counting from where the deploy wizard left off.
   * Zero when the deployer is opened on its own (the agents-view dialog).
   */
  @property({ type: Number, attribute: 'step-offset' })
  stepOffset = 0;

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
  private sshHostKey = '';

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
  private deploymentError = '';

  @state()
  private deploymentSucceeded = false;

  private deploymentRequestId = '';
  private deploymentRequestSignature = '';

  @state()
  private isAddingAIModel = false;

  @state()
  private showComputeSetupHelp = false;

  @state()
  private showComputeAdminNotice = false;

  @state()
  private showComputePromo = false;

  @state()
  private gcpConfigured = false;

  async connectedCallback() {
    super.connectedCallback();
    void fetchWithAuth('/api/v1/agent-deployments/capabilities', {
      passive: true,
    })
      .then(async (response) => {
        if (response.ok)
          this.gcpConfigured = Boolean((await response.json()).gcp);
      })
      .catch(() => {
        this.gcpConfigured = false;
      });
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
    const url =
      agentType === 'hermes'
        ? 'https://hermes-agent.nousresearch.com/install.sh'
        : 'https://openclaw.ai/install.sh';
    const args =
      agentType === 'hermes' ? '--non-interactive' : '--no-onboard --no-prompt';
    return `(installer=$(mktemp) && curl -fsSL ${url} -o "$installer" && bash "$installer" ${args}; status=$?; rm -f "$installer"; exit "$status")`;
  }

  private runtimeOnboardCommand(agentType: 'hermes' | 'openclaw'): string {
    const agentName = agentType === 'hermes' ? 'hermes' : 'openclaw';
    const base = `preloop agents onboard ${agentName} -y${this.cliModelArgument()}`;
    if (window.location.hostname === 'preloop.ai') {
      return base;
    }
    return `export PRELOOP_URL=${window.location.origin} && ${base}`;
  }

  private cliModelArgument(): string {
    const model = this.aiModels.find(
      (candidate) => candidate.id === this.deployModel
    );
    if (!model) return '';
    const gateway = (model.meta_data?.gateway || {}) as Record<string, unknown>;
    const explicit = gateway.model_alias;
    const alias =
      typeof explicit === 'string' && explicit.trim()
        ? explicit.trim()
        : `${(model.provider_name || 'openai').trim().toLowerCase()}/${(model.model_identifier || '').trim()}`;
    // Models are user-configured. Quote aliases as one literal shell argument.
    return ` --model '${alias.replace(/'/g, "'\\''")}'`;
  }

  private runtimeInstallAndOnboardCommand(
    agentType: 'hermes' | 'openclaw'
  ): string {
    const base = `preloop agents install-runtime ${agentType} -y${this.cliModelArgument()}`;
    if (window.location.hostname === 'preloop.ai') {
      return base;
    }
    return `export PRELOOP_URL=${window.location.origin} && ${base}`;
  }

  private handleFreshVmSelection() {
    if (this.gcpConfigured) {
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
    return this.deployAgent('gcp');
  }

  private startSshDeployBootSequence() {
    return this.deployAgent('ssh');
  }

  private async deployAgent(target: 'ssh' | 'gcp') {
    if (this.isBooting) return;
    this.isBooting = true;
    this.deploymentError = '';
    this.deploymentSucceeded = false;
    this.bootLogs = [
      'Submitting deployment. Installation and verified onboarding may take up to 15 minutes.',
    ];
    const request = {
      target,
      runtime: this.deployAgentType,
      model_id: this.deployModel,
      compute_size: this.deployComputeSize,
      ...(target === 'ssh'
        ? {
            ssh: {
              host: this.sshHost.trim(),
              port: Number(this.sshPort || '22'),
              username: this.sshUsername.trim(),
              host_key: this.sshHostKey.trim(),
              ...(this.sshAuthType === 'password'
                ? { password: this.sshPassword }
                : { private_key: this.sshPrivateKey }),
            },
          }
        : {}),
    };
    const signature = JSON.stringify(request);
    if (signature !== this.deploymentRequestSignature) {
      this.deploymentRequestId = crypto.randomUUID();
      this.deploymentRequestSignature = signature;
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 930_000);
    try {
      const response = await fetchWithAuth('/api/v1/agent-deployments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...request,
          idempotency_key: this.deploymentRequestId,
        }),
        signal: controller.signal,
      });
      const result = await response.json();
      if (!response.ok || result.status !== 'succeeded' || !result.agent?.id) {
        const detail = result.detail;
        throw new Error(
          typeof detail === 'string'
            ? detail
            : detail?.error ||
                result.error ||
                'Deployment did not complete. No connected agent was confirmed.'
        );
      }
      this.bootLogs = [
        ...(result.logs || []),
        'SUCCESS: Runtime installation and agent onboarding verified.',
      ];
      this.deploymentSucceeded = true;
      this.sshPassword = '';
      this.sshPrivateKey = '';
      this.deploymentRequestSignature = '';
      this.dispatchEvent(
        new CustomEvent('deploy-agent-success', {
          bubbles: true,
          composed: true,
          detail: { agent: result.agent },
        })
      );
    } catch (error) {
      this.deploymentError = controller.signal.aborted
        ? 'The deployment request timed out. Check Agents before retrying; the server may still be completing installation.'
        : error instanceof Error
          ? error.message
          : 'Deployment failed.';
      this.bootLogs = [...this.bootLogs, this.deploymentError];
    } finally {
      clearTimeout(timeout);
    }
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

  /**
   * Where this screen sits, counting from the step the host handed over on.
   * `total` is null on a screen that branches, because the path length is not
   * decided yet.
   */
  private stepPosition(): { index: number; total: number | null } {
    const base = this.stepOffset;
    switch (this.deploySubStep) {
      case 'agent-host':
        return { index: base + 1, total: null };
      case 'existing-host-method':
        return { index: base + 2, total: null };
      case 'fresh-vm-premium':
        return { index: base + 2, total: base + 2 };
      default:
        return { index: base + 3, total: base + 3 };
    }
  }

  private renderStepHeader(title: string, copy?: string) {
    const { index, total } = this.stepPosition();
    return html`
      <div class="wizard-header">
        <div class="wizard-step-count">
          <span
            >${total === null ? `Step ${index}` : `Step ${index} of ${total}`}</span
          >
          ${
            total === null
              ? nothing
              : html`
                  <span class="wizard-step-rail" aria-hidden="true">
                    ${Array.from(
                      { length: total },
                      (_unused, i) =>
                        html`<span class=${i < index ? 'done' : ''}></span>`
                    )}
                  </span>
                `
          }
        </div>
        <h3 class="wizard-title">${title}</h3>
        ${copy ? html`<p class="wizard-copy">${copy}</p>` : nothing}
      </div>
    `;
  }

  /**
   * One action bar per step: Back as a text button on the left, the single
   * primary action on the right.
   */
  private renderActions(showBack: boolean, primary: unknown = nothing) {
    if (!showBack && primary === nothing) {
      return nothing;
    }
    return html`
      <div class="wizard-actions">
        ${
          showBack
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
        ${primary}
      </div>
    `;
  }

  private renderOptionCard(
    icon: string,
    title: string,
    description: string,
    onClick: () => void
  ): TemplateResult {
    return html`
      <button type="button" class="wizard-option-button" @click=${onClick}>
        <sl-icon class="wizard-option-icon" name=${icon}></sl-icon>
        <span class="wizard-option-copy">
          <span class="wizard-option-title">${title}</span>
          <span class="wizard-option-description">${description}</span>
        </span>
        <sl-icon class="wizard-option-arrow" name="chevron-right"></sl-icon>
      </button>
    `;
  }

  private renderCommandStep(
    label: string,
    command: string,
    index?: number
  ): TemplateResult {
    return html`
      <div class="command-step">
        <div class="command-label">
          ${index ? html`<span class="command-index">${index}</span>` : nothing}
          <span>${label}</span>
        </div>
        <div class="command-row">
          <code class="command-code">${command}</code>
          <sl-copy-button .value=${command}></sl-copy-button>
        </div>
      </div>
    `;
  }

  private renderSummaryRow(key: string, value: string) {
    return html`
      <div class="wizard-summary-row">
        <span class="wizard-summary-key">${key}</span>
        <span class="wizard-summary-value">${value}</span>
      </div>
    `;
  }

  private selectedModelName(): string {
    const model = this.aiModels.find((m) => m.id === this.deployModel);
    return model?.name || 'Not set';
  }

  private renderModelField() {
    return html`
      <div class="wizard-field">
        <sl-select
          label="AI model"
          value=${this.deployModel}
          @sl-change=${(e: any) => (this.deployModel = e.target.value)}
        >
          ${this.aiModels
            .filter((m) => m.model_kind !== 'stt' && m.model_kind !== 'tts')
            .map((m) => html`<sl-option .value=${m.id}>${m.name}</sl-option>`)}
        </sl-select>
        <sl-button
          class="field-link"
          size="small"
          variant="text"
          @click=${() => (this.isAddingAIModel = true)}
        >
          <sl-icon slot="prefix" name="plus-lg"></sl-icon> Add an AI model
        </sl-button>
      </div>
    `;
  }

  private renderRuntimeField() {
    return html`
      <sl-select
        label="Agent runtime"
        value=${this.deployAgentType}
        help-text="Choose the agent runtime to install and connect to Preloop."
        @sl-change=${(e: any) => {
          this.deployAgentType = e.target.value;
          this.requestUpdate();
        }}
      >
        <sl-option value="hermes">Hermes</sl-option>
        <sl-option value="openclaw">OpenClaw</sl-option>
      </sl-select>
    `;
  }

  render() {
    if (this.isBooting) {
      return this.renderDeploymentProgress();
    }

    return html`
      <div style="width: 100%;">
        <div class="wizard-shell">
          ${
            this.deploySubStep === 'agent-host'
              ? this.renderAgentHostStep()
              : this.deploySubStep === 'existing-host-method'
                ? this.renderHostMethodStep()
                : this.deploySubStep === 'cli-install'
                  ? this.renderCliInstallStep()
                  : this.deploySubStep === 'ssh-config'
                    ? this.renderSshConfigStep()
                    : this.renderFreshVmStep()
          }
        </div>

        <!-- Compute Backends Support Dialogs -->
        <sl-dialog
          label="Set up a compute backend"
          ?open=${this.showComputeSetupHelp}
          @sl-after-hide=${() => (this.showComputeSetupHelp = false)}
        >
          <p class="dialog-copy">
            <strong>
              No AWS, GCP or KubeVirt compute backend is configured.
            </strong>
          </p>
          <p class="dialog-copy">
            To deploy agents onto VMs, set the compute configuration in your
            server environment variables or in the organization configuration,
            then reopen this step.
          </p>
          <sl-button
            slot="footer"
            variant="primary"
            @click=${() => (this.showComputeSetupHelp = false)}
            >Got it</sl-button
          >
        </sl-dialog>

        <sl-dialog
          label="Provision VM notice"
          ?open=${this.showComputeAdminNotice}
          @sl-after-hide=${() => (this.showComputeAdminNotice = false)}
        >
          <p class="dialog-copy">
            Compute backends are not configured for this account. Ask an
            administrator to configure a VM compute backend in settings.
          </p>
          <sl-button
            slot="footer"
            variant="primary"
            @click=${() => (this.showComputeAdminNotice = false)}
            >Close</sl-button
          >
        </sl-dialog>

        <sl-dialog
          label="Unlock cloud VM provisioning"
          ?open=${this.showComputePromo}
          @sl-after-hide=${() => (this.showComputePromo = false)}
        >
          <div class="promo">
            <sl-icon name="cpu"></sl-icon>
            <h3 class="promo-title">Cloud VM compute backends</h3>
            <p class="dialog-copy">
              Provisioning secure virtual machines in the cloud is an Enterprise
              Edition feature. Talk to our team to upgrade your workspace.
            </p>
          </div>
          <sl-button
            slot="footer"
            variant="primary"
            href="mailto:sales@preloop.ai?subject=Preloop%20Enterprise%20Compute%20Backend"
            target="_blank"
          >
            Contact sales
          </sl-button>
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

  private renderAgentHostStep() {
    return html`
      ${this.renderStepHeader(
        'Choose where the agent runs',
        'Preloop installs and governs the agent on the host you pick.'
      )}

      <div class="wizard-card-grid">
        ${this.renderOptionCard(
          'hdd-network',
          'Deploy on an existing host',
          'Install a Hermes or OpenClaw agent on a machine you control, over SSH or with the Preloop CLI.',
          () => {
            this.deploySubStep = 'existing-host-method';
            this.requestUpdate();
          }
        )}
        ${
          this.isEnterprise || this.gcpConfigured
            ? this.renderOptionCard(
                'cpu',
                'Deploy on a fresh cloud VM',
                'Provision a new isolated VM managed by a Preloop compute backend.',
                () => this.handleFreshVmSelection()
              )
            : nothing
        }
      </div>

      ${this.renderActions(!this.hideBackButton)}
    `;
  }

  private renderHostMethodStep() {
    return html`
      ${this.renderStepHeader(
        'Deploy on an existing host',
        'Choose how Preloop reaches the host where the agent will run.'
      )}

      <div class="wizard-card-grid">
        ${this.renderOptionCard(
          'key',
          'SSH access',
          'Preloop connects to the host over SSH and deploys the agent. Needs an address your Preloop instance can reach.',
          () => {
            this.deploySubStep = 'ssh-config';
            this.requestUpdate();
          }
        )}
        ${this.renderOptionCard(
          'terminal',
          'Install with the Preloop CLI',
          'Run three commands on the host. Works when the host only has outbound access to Preloop.',
          () => {
            this.deploySubStep = 'cli-install';
            this.requestUpdate();
          }
        )}
      </div>

      ${this.renderActions(true)}
    `;
  }

  private renderCliInstallStep() {
    const runtimeLabel =
      this.deployAgentType === 'hermes' ? 'Hermes' : 'OpenClaw';
    return html`
      ${this.renderStepHeader(
        'Install the runtime with the Preloop CLI',
        'Run these commands on the host. The agent connects outbound to Preloop, so inbound SSH access is not required.'
      )}

      <div class="wizard-form">
        ${this.renderRuntimeField()} ${this.renderModelField()}
      </div>

      <div class="command-steps">
        ${this.renderCommandStep(
          'Install the Preloop CLI on the host',
          this.preloopCliInstallCommand(),
          1
        )}
        ${this.renderCommandStep(
          'Authenticate the CLI',
          this.preloopLoginCommand(),
          2
        )}
        ${this.renderCommandStep(
          `Install ${runtimeLabel} and onboard it through Preloop`,
          this.runtimeInstallAndOnboardCommand(this.deployAgentType),
          3
        )}

        <div class="command-step">
          <div class="command-label">
            <span>Or run the steps separately</span>
          </div>
          <div class="command-row">
            <code class="command-code"
              >${this.runtimeInstallCommand(this.deployAgentType)}</code
            >
            <sl-copy-button
              .value=${this.runtimeInstallCommand(this.deployAgentType)}
            ></sl-copy-button>
          </div>
          <div class="command-row">
            <code class="command-code"
              >${this.runtimeOnboardCommand(this.deployAgentType)}</code
            >
            <sl-copy-button
              .value=${this.runtimeOnboardCommand(this.deployAgentType)}
            ></sl-copy-button>
          </div>
        </div>
      </div>

      ${this.renderActions(true)}
    `;
  }

  private renderSshConfigStep() {
    const canDeploy =
      Boolean(this.sshHost) &&
      Boolean(this.sshUsername) &&
      Boolean(this.sshHostKey.trim()) &&
      Boolean(this.deployModel) &&
      (this.sshAuthType === 'password'
        ? Boolean(this.sshPassword)
        : Boolean(this.sshPrivateKey));

    return html`
      ${this.renderStepHeader(
        'Deploy over SSH',
        'Preloop connects to this host and installs the governed agent on it.'
      )}

      <div class="wizard-form">
        <sl-input
          label="Host address"
          help-text="The address or IP your Preloop instance can reach, for example 192.168.1.100."
          .value=${this.sshHost}
          @sl-input=${(e: any) => (this.sshHost = e.target.value)}
        ></sl-input>

        <sl-input
          label="SSH username"
          help-text="The account Preloop logs in as, for example ubuntu."
          .value=${this.sshUsername}
          @sl-input=${(e: any) => (this.sshUsername = e.target.value)}
        ></sl-input>

        <sl-input
          label="SSH port"
          help-text="Defaults to 22."
          .value=${this.sshPort}
          @sl-input=${(e: any) => (this.sshPort = e.target.value)}
        ></sl-input>

        <sl-textarea
          label="SSH host public key"
          help-text="Verify this key with the host administrator over a trusted channel. Paste its OpenSSH public key (for example ssh-ed25519 AAAA...)."
          rows="2"
          .value=${this.sshHostKey}
          @sl-input=${(e: any) => (this.sshHostKey = e.target.value)}
        ></sl-textarea>

        <sl-radio-group
          label="Authentication"
          value=${this.sshAuthType}
          @sl-change=${(e: any) => {
            this.sshAuthType = e.target.value;
            this.requestUpdate();
          }}
        >
          <sl-radio value="password">Password</sl-radio>
          <sl-radio value="key">Private key</sl-radio>
        </sl-radio-group>

        ${
          this.sshAuthType === 'password'
            ? html`
                <sl-input
                  type="password"
                  label="SSH password"
                  help-text="Used for this deployment only; Preloop does not store it."
                  password-toggle
                  .value=${this.sshPassword}
                  @sl-input=${(e: any) => (this.sshPassword = e.target.value)}
                ></sl-input>
              `
            : html`
                <sl-textarea
                  label="SSH private key"
                  help-text="Paste the private key Preloop should use for this host."
                  rows="4"
                  .value=${this.sshPrivateKey}
                  @sl-input=${(e: any) => (this.sshPrivateKey = e.target.value)}
                ></sl-textarea>
              `
        }
        ${this.renderRuntimeField()} ${this.renderModelField()}

        <div class="wizard-summary">
          <div class="wizard-summary-title">About to deploy</div>
          ${this.renderSummaryRow(
            'Runtime',
            this.deployAgentType === 'hermes' ? 'Hermes' : 'OpenClaw'
          )}
          ${this.renderSummaryRow(
            'Host',
            `${this.sshUsername || 'user'}@${this.sshHost || 'host'}:${
              this.sshPort || '22'
            }`
          )}
          ${this.renderSummaryRow('Model', this.selectedModelName())}
          ${this.renderSummaryRow(
            'Authentication',
            this.sshAuthType === 'password' ? 'Password' : 'Private key'
          )}
        </div>
      </div>

      ${this.renderActions(
        true,
        html`
          <sl-button
            variant="primary"
            ?disabled=${!canDeploy}
            @click=${this.startSshDeployBootSequence}
            >Deploy agent</sl-button
          >
        `
      )}
    `;
  }

  private renderFreshVmStep() {
    const sizeLabels: Record<string, string> = {
      standard: 'Standard (2 vCPU, 8GB RAM)',
      performance: 'Performance (4 vCPU, 16GB RAM)',
      'high-mem': 'High memory (4 vCPU, 32GB RAM)',
    };
    return html`
      ${this.renderStepHeader(
        'Provision a secure cloud agent VM',
        'Preloop creates the VM, installs the runtime and enrolls the agent for you.'
      )}

      <div class="wizard-form">
        ${this.renderRuntimeField()}

        <sl-select
          label="Compute sandbox size"
          value=${this.deployComputeSize}
          help-text="Sizes the sandbox the agent runs in. You can change it later."
          @sl-change=${(e: any) => (this.deployComputeSize = e.target.value)}
        >
          <sl-option value="standard">Standard (2 vCPU, 8GB RAM)</sl-option>
          <sl-option value="performance"
            >Performance (4 vCPU, 16GB RAM)</sl-option
          >
          <sl-option value="high-mem">High memory (4 vCPU, 32GB RAM)</sl-option>
        </sl-select>

        ${this.renderModelField()}

        <div class="wizard-summary">
          <div class="wizard-summary-title">About to provision</div>
          ${this.renderSummaryRow(
            'Runtime',
            this.deployAgentType === 'hermes' ? 'Hermes' : 'OpenClaw'
          )}
          ${this.renderSummaryRow(
            'Size',
            sizeLabels[this.deployComputeSize] || this.deployComputeSize
          )}
          ${this.renderSummaryRow('Model', this.selectedModelName())}
        </div>
      </div>

      ${this.renderActions(
        true,
        html`
          <sl-button
            variant="primary"
            ?disabled=${!this.deployModel}
            @click=${this.startDeployBootSequence}
            >Provision VM agent node</sl-button
          >
        `
      )}
    `;
  }

  private renderDeploymentProgress() {
    const done = this.deploymentSucceeded;
    const failed = Boolean(this.deploymentError);
    return html`
      <div class="wizard-shell">
        <div class="wizard-header">
          <h3 class="wizard-title boot-title">
            ${done || failed ? nothing : html`<sl-spinner></sl-spinner>`}
            <span>
              ${
                done
                  ? 'Agent node provisioned'
                  : failed
                    ? 'Agent deployment failed'
                    : 'Provisioning the secure agent node'
              }
            </span>
          </h3>
          <p class="wizard-copy">
            ${failed ? 'Review the error below and check the host before trying again.' : done ? 'The server verified the installed runtime and its Preloop enrollment.' : 'Preloop is preparing the environment, installing the runtime and enrolling the agent. Keep this dialog open until verification finishes.'}
          </p>
        </div>

        <div class="boot-log" role="log" aria-live="polite">
          ${this.bootLogs.map((log) => {
            const isSuccess = log.startsWith('SUCCESS');
            return html`
              <div class="boot-log-line ${isSuccess ? 'success' : ''}">
                ${log}
              </div>
            `;
          })}
        </div>

        ${
          done || failed
            ? html`
                <div class="wizard-actions">
                  <sl-button
                    variant="primary"
                    @click=${() => {
                      this.isBooting = false;
                      if (!done) return;
                      this.dispatchEvent(
                        new CustomEvent('deploy-wizard-done', {
                          bubbles: true,
                          composed: true,
                        })
                      );
                    }}
                  >
                    ${done ? 'View the connected agent' : 'Back to deployment settings'}
                  </sl-button>
                </div>
              `
            : nothing
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'preloop-agent-deployer': PreloopAgentDeployer;
  }
}
