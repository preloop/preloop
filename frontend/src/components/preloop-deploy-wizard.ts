import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/copy-button/copy-button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import {
  getAIModels,
  getAccountAgent,
  createFlow,
  createManagedAgent,
  createManagedAgentCredential,
  replaceManagedAgentModelBindings,
  updateManagedAgent,
} from '../api';
import type { ManagedAgentModelBindingSyncItem } from '../api';
import type { AIModel, ManagedAgentSummary } from '../types';
import './preloop-flow-form';
import './preloop-agent-deployer';

type OnboardingPath = 'choose' | 'govern' | 'cli' | 'deploy' | 'custom';

// Per-run session header the gateway maps a LangGraph thread_id onto. This MUST
// match the header name the gateway implementation reads. If the gateway team
// changes it, update this constant (and the snippet rendered below).
const PRELOOP_SESSION_HEADER = 'X-Preloop-Session-Id';

// First-data polling: after the credential + snippet are shown we poll the
// managed-agent summary for the first gateway request. `total_requests` is the
// most reliable signal — it is derived from api_usage rows and deterministically
// goes 0 -> 1 on the agent's first gateway call (independent of lifecycle state
// or runtime-session binding). We poll on an interval and cap the total wait so
// the dialog never hangs; a manual "Done" button is always available.
const FIRST_DATA_POLL_INTERVAL_MS = 2500;
const FIRST_DATA_POLL_TIMEOUT_MS = 3 * 60 * 1000; // stop polling after ~3 minutes
// How long the "Connected" success state stays up before auto-dismiss.
const FIRST_DATA_SUCCESS_DISMISS_MS = 2000;

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

    .conn-status {
      width: 100%;
    }

    .conn-waiting {
      display: flex;
      align-items: center;
      gap: var(--sl-spacing-small);
      color: var(--sl-color-neutral-600);
      font-size: var(--sl-font-size-small);
    }

    .conn-waiting sl-spinner {
      font-size: 1rem;
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
    'type' | 'agent-host' | 'ssh-config' | 'fresh-vm-premium' | 'flow-config' =
    'type';

  // Custom-agent onboarding substeps:
  //  - 'name'   : collect display_name (+ optional description and tags)
  //  - 'models' : choose which gateway-enabled models the agent may use
  //  - 'result' : show the minted credential token + copy-paste snippet ONCE,
  //               then wait for the agent's first gateway request.
  @state()
  private customSubStep: 'name' | 'models' | 'result' = 'name';

  @state()
  private customDisplayName = '';

  @state()
  private customDescription = '';

  // Raw tag input: a space-separated list of `key` or `key=value` tokens. This
  // mirrors the tag-edit convention used in agent-detail-view / agents-view:
  // a valueless token becomes `{key: "true"}`, and tokens round-trip via the
  // same `key=value` / `split(/\s+/)` format. Parsed to a Record<string,string>
  // and sent to PATCH /api/v1/agents/{id} after registration.
  @state()
  private customTagsInput = '';

  @state()
  private customBusy = false;

  @state()
  private customError = '';

  // IDs of the gateway-enabled AI models the user picked for this agent.
  @state()
  private customSelectedModelIds: string[] = [];

  // Populated after a successful register + mint. The token is shown ONCE.
  @state()
  private customAgentId: string | null = null;

  @state()
  private customCredentialToken: string | null = null;

  // The gateway alias the agent should pass as `model=...`. Derived from the
  // first selected model; falls back to a generic placeholder if none chosen.
  @state()
  private customModelAlias: string | null = null;

  // First-data polling lifecycle. `customConnState` drives the waiting/success
  // UI on the result screen.
  @state()
  private customConnState: 'waiting' | 'connected' = 'waiting';

  @state()
  private customConnRequestCount = 0;

  // Timer/handle bookkeeping so we can cancel cleanly on teardown / navigation.
  private customPollTimer: number | null = null;
  private customPollDeadline = 0;
  private customDismissTimer: number | null = null;
  private customPolling = false;

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

  disconnectedCallback() {
    super.disconnectedCallback();
    // Cancel any in-flight polling / dismiss timers so a torn-down wizard does
    // not keep firing fetches or dispatch events after it is gone.
    this.cancelFirstDataPolling();
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
    } else if (this.onboardingPath === 'govern') {
      // The govern sub-choice sits below the top-level choose screen. When the
      // wizard was opened directly at govern (e.g. the agents-view "Onboard
      // Agents" dialog, initial-path="govern") there is no choose screen above
      // it, so backing out cancels. Otherwise return to the choose screen.
      if (this.initialPath === 'govern') {
        this.dispatchEvent(
          new CustomEvent('deploy-cancel', { bubbles: true, composed: true })
        );
      } else {
        this.onboardingPath = 'choose';
      }
    } else if (this.onboardingPath === 'cli') {
      // The CLI path is reached from the govern sub-choice, so back returns
      // there rather than all the way to the top-level choose screen.
      this.onboardingPath = 'govern';
    } else if (this.onboardingPath === 'deploy') {
      if (this.deploySubStep === 'type') {
        this.onboardingPath = 'choose';
      } else if (this.deploySubStep === 'flow-config') {
        this.deploySubStep = 'type';
      }
    } else if (this.onboardingPath === 'custom') {
      if (this.customSubStep === 'name') {
        // First substep -> back to the govern sub-choice (the custom path is
        // reached from there, not directly from the top-level choose screen).
        this.onboardingPath = 'govern';
      } else if (this.customSubStep === 'models') {
        // Model selection -> back to the name form.
        this.customSubStep = 'name';
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
        // Stop any first-data polling and discard the token from memory.
        this.cancelFirstDataPolling();
        this.customCredentialToken = null;
        this.customAgentId = null;
        this.customSubStep = 'name';
      }
    }
    this.requestUpdate();
  }

  private resetCustomState() {
    this.cancelFirstDataPolling();
    this.customSubStep = 'name';
    this.customDisplayName = '';
    this.customDescription = '';
    this.customTagsInput = '';
    this.customBusy = false;
    this.customError = '';
    this.customSelectedModelIds = [];
    this.customAgentId = null;
    this.customCredentialToken = null;
    this.customModelAlias = null;
    this.customConnState = 'waiting';
    this.customConnRequestCount = 0;
  }

  /**
   * Gateway-enabled AI models the user can grant this agent. Mirrors the
   * backend filter in `resolve_ai_model_runtime`: a model is usable via the
   * gateway when `meta_data.gateway.enabled` is truthy. STT/TTS models are
   * excluded — only chat ("llm") models route through the OpenAI-compatible
   * gateway.
   */
  private gatewayEnabledModels(): AIModel[] {
    return (this.aiModels || []).filter((model) => {
      if (model.model_kind && model.model_kind !== 'llm') {
        return false;
      }
      const meta = (model.meta_data || {}) as Record<string, unknown>;
      const gateway = meta.gateway as Record<string, unknown> | undefined;
      return Boolean(gateway && gateway.enabled);
    });
  }

  /**
   * The gateway alias an agent passes as `model=...`. Mirrors the backend:
   * `meta_data.gateway.model_alias` when set, otherwise the default
   * `"{provider}/{model_identifier}"`. The gateway matches this exactly (or by
   * the suffix after the final "/").
   */
  private gatewayAliasForModel(model: AIModel): string {
    const meta = (model.meta_data || {}) as Record<string, unknown>;
    const gateway = (meta.gateway as Record<string, unknown> | undefined) || {};
    const explicit = gateway.model_alias;
    if (typeof explicit === 'string' && explicit.trim()) {
      return explicit.trim();
    }
    const provider = (model.provider_name || 'openai').trim().toLowerCase();
    const identifier = (model.model_identifier || '').trim();
    return identifier ? `${provider}/${identifier}` : provider;
  }

  /**
   * Parse the raw tag input into the `Record<string,string>` shape the backend
   * stores. Mirrors the agent-detail-view / agents-view convention exactly:
   * tokens are split on whitespace; each `key=value` token maps key -> value
   * (preserving any further `=` in the value), and a bare `key` token maps to
   * the boolean sentinel `"true"`. An empty / whitespace-only input yields {}.
   */
  private parseCustomTags(): Record<string, string> {
    const tags: Record<string, string> = {};
    this.customTagsInput.split(/\s+/).forEach((token) => {
      if (!token) {
        return;
      }
      const [key, ...valueParts] = token.split('=');
      if (!key) {
        return;
      }
      tags[key] = valueParts.length > 0 ? valueParts.join('=') : 'true';
    });
    return tags;
  }

  /**
   * Advance from the name form to the model-selection step. Validates the
   * display name first so the user cannot skip ahead without one.
   */
  private handleCustomContinueToModels() {
    const displayName = this.customDisplayName.trim();
    if (!displayName) {
      this.customError = 'Agent name is required.';
      return;
    }
    this.customError = '';
    this.customSubStep = 'models';
    this.requestUpdate();
  }

  /**
   * Register the agent, set its allowed model bindings, mint a gateway
   * credential, then move to the result screen and begin first-data polling.
   *
   * Sequence: register agent -> PUT model-bindings -> PATCH tags ->
   * mint credential -> result. Tags are optional; the PATCH is skipped when the
   * tag input is empty.
   *
   * Model selection policy: if the account has gateway-enabled models, the user
   * must pick at least one (enforced here and by a disabled button). If the
   * account has none, we register without bindings and the snippet shows a
   * placeholder alias the user must replace.
   */
  private async handleCustomRegister() {
    const displayName = this.customDisplayName.trim();
    if (!displayName) {
      this.customError = 'Agent name is required.';
      this.customSubStep = 'name';
      return;
    }

    const enabledModels = this.gatewayEnabledModels();
    const selectedModels = enabledModels.filter((m) =>
      this.customSelectedModelIds.includes(m.id)
    );
    if (enabledModels.length > 0 && selectedModels.length === 0) {
      this.customError = 'Select at least one model this agent may use.';
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

      // 2. Set the allowed model bindings: PUT /api/v1/agents/{id}/model-bindings
      // Each binding maps an account AI model to the gateway alias the agent
      // passes as `model=...`. The first selected model is marked primary and
      // drives the snippet's `model` parameter.
      if (selectedModels.length > 0) {
        const bindings: ManagedAgentModelBindingSyncItem[] = selectedModels.map(
          (model, index) => {
            const alias = this.gatewayAliasForModel(model);
            return {
              ai_model_id: model.id,
              binding_type: 'configured',
              config_key: `onboarding.${alias}`,
              gateway_alias: alias,
              is_primary: index === 0,
              status: 'gateway_ready',
            };
          }
        );
        await replaceManagedAgentModelBindings(agent.id, { bindings });
        this.customModelAlias = bindings[0].gateway_alias;
      } else {
        this.customModelAlias = null;
      }

      // 3. Apply tags (optional): PATCH /api/v1/agents/{id}
      // The register endpoint accepts only {display_name, description}, so tags
      // are set in this follow-up PATCH. Skip the call entirely when no tags
      // were entered so we don't issue an empty update.
      const tags = this.parseCustomTags();
      if (Object.keys(tags).length > 0) {
        await updateManagedAgent(agent.id, { tags });
      }

      // 4. Mint a gateway credential: POST /api/v1/agents/{id}/credentials
      // Scopes must be in RUNTIME_SESSION_ALLOWED_SCOPES (api/auth/router.py);
      // mcp:read/mcp:write authorize gateway model traffic. gateway:invoke is
      // rejected at mint time (HTTP 400).
      const result = await createManagedAgentCredential(agent.id, {
        name: `${displayName} gateway credential`,
        scopes: ['mcp:read', 'mcp:write'],
      });
      this.customCredentialToken = result.token;

      // 5. Show the result screen (token presented ONCE) and start watching for
      // the agent's first gateway request.
      this.customConnState = 'waiting';
      this.customConnRequestCount = 0;
      this.customSubStep = 'result';
      this.dispatchEvent(
        new CustomEvent('deploy-custom-agent-success', {
          bubbles: true,
          composed: true,
          detail: { agent },
        })
      );
      this.startFirstDataPolling(agent.id);
    } catch (error: any) {
      this.customError =
        error?.message || 'Failed to connect custom agent. Please try again.';
    } finally {
      this.customBusy = false;
    }
  }

  /**
   * Begin polling the managed-agent summary for the first gateway request.
   * `total_requests` going 0 -> 1 is the signal. Polling stops on success, on
   * timeout, or when cancelled (teardown / manual Done / back navigation).
   */
  private startFirstDataPolling(agentId: string) {
    this.cancelFirstDataPolling();
    this.customPolling = true;
    this.customPollDeadline = Date.now() + FIRST_DATA_POLL_TIMEOUT_MS;
    const poll = async () => {
      if (!this.customPolling || this.customAgentId !== agentId) {
        return;
      }
      let detectedCount: number | null = null;
      try {
        const detail = await getAccountAgent(agentId);
        const agent = detail?.agent as ManagedAgentSummary | undefined;
        const count = Number(agent?.total_requests ?? 0);
        if (Number.isFinite(count) && count > 0) {
          detectedCount = count;
        }
      } catch {
        // Transient errors are ignored; we keep polling until the deadline.
      }
      if (!this.customPolling || this.customAgentId !== agentId) {
        return;
      }
      if (detectedCount !== null) {
        this.onFirstDataDetected(detectedCount);
        return;
      }
      if (Date.now() >= this.customPollDeadline) {
        // Cap reached: stop polling and leave the manual "Done" button as the
        // fallback. We stay in the waiting state but no longer spin forever.
        this.customPolling = false;
        this.customPollTimer = null;
        this.requestUpdate();
        return;
      }
      this.customPollTimer = window.setTimeout(
        poll,
        FIRST_DATA_POLL_INTERVAL_MS
      );
    };
    // First check after one interval (gives the agent time to send a request).
    this.customPollTimer = window.setTimeout(poll, FIRST_DATA_POLL_INTERVAL_MS);
  }

  /**
   * Handle the first detected gateway request: show the connected success state,
   * wait briefly so the user sees it, then close the dialog and surface the
   * agent in the fleet via `deploy-wizard-done`.
   */
  private onFirstDataDetected(requestCount: number) {
    this.customPolling = false;
    if (this.customPollTimer !== null) {
      window.clearTimeout(this.customPollTimer);
      this.customPollTimer = null;
    }
    this.customConnRequestCount = requestCount;
    this.customConnState = 'connected';
    this.requestUpdate();
    this.customDismissTimer = window.setTimeout(() => {
      this.customDismissTimer = null;
      this.customCredentialToken = null;
      this.dispatchEvent(
        new CustomEvent('deploy-wizard-done', {
          bubbles: true,
          composed: true,
        })
      );
    }, FIRST_DATA_SUCCESS_DISMISS_MS);
  }

  /** Stop all first-data timers and clear the polling flag. */
  private cancelFirstDataPolling() {
    this.customPolling = false;
    if (this.customPollTimer !== null) {
      window.clearTimeout(this.customPollTimer);
      this.customPollTimer = null;
    }
    if (this.customDismissTimer !== null) {
      window.clearTimeout(this.customDismissTimer);
      this.customDismissTimer = null;
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
    // The `model` param must be a gateway alias bound to this agent. Use the
    // primary selected model's alias; if none was chosen, show a placeholder.
    const modelAlias =
      this.customModelAlias || 'your-model-alias  # set an allowed model';
    return `from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

# Route all model traffic through Preloop's OpenAI-compatible gateway.
llm = ChatOpenAI(
    base_url="${baseUrl}",
    api_key="${token}",  # Preloop gateway credential (shown once)
    model="${modelAlias}",
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
        ${
          this.onboardingPath === 'choose'
            ? this.renderChoosePathState()
            : this.onboardingPath === 'govern'
              ? this.renderGovernPathState()
              : this.onboardingPath === 'cli'
                ? this.renderCliPathState()
                : this.onboardingPath === 'custom'
                  ? this.renderCustomPathState()
                  : this.renderDeployPathState()
        }
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
            @click=${() => {
              this.onboardingPath = 'govern';
              this.requestUpdate();
            }}
          >
            <div class="wizard-option-body">
              <span class="wizard-option-icon">
                <sl-icon name="shield-check"></sl-icon>
              </span>
              <span class="wizard-option-copy">
                <span class="wizard-option-title">Govern Existing Agents</span>
                <span class="wizard-option-description">
                  Connect agents you already run — via CLI autodiscovery or by
                  onboarding a custom agent
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

  private renderGovernPathState() {
    // The govern sub-choice is the wizard's first screen only when it was
    // opened directly here (initial-path="govern", e.g. the agents-view
    // "Onboard Agents" dialog). In that case there is no previous step, so the
    // dialog's close button is the only exit and a Back link is just confusing.
    // Show Back only when govern was reached from the choose screen (e.g. the
    // "Get Started" flow, initial-path="choose").
    const hideGovernBack = this.hideBack || this.initialPath === 'govern';
    return html`
      <div class="wizard-shell">
        ${
          hideGovernBack
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
              `
        }

        <div class="wizard-header">
          ${
            this.hideStepTitle
              ? nothing
              : html`<h3 class="wizard-title">Govern Existing Agents</h3>`
          }
          <p class="wizard-copy">
            Bring agents you already run under Preloop's control plane. Let the
            CLI autodiscover local agents, or onboard a custom agent the CLI
            can't reach.
          </p>
        </div>

        <div class="wizard-card-grid">
          <sl-button
            class="wizard-option-button"
            variant="default"
            @click=${() => {
              this.onboardingPath = 'cli';
              this.requestUpdate();
            }}
          >
            <div class="wizard-option-body">
              <span class="wizard-option-icon">
                <sl-icon name="terminal"></sl-icon>
              </span>
              <span class="wizard-option-copy">
                <span class="wizard-option-title">Autodiscover via CLI</span>
                <span class="wizard-option-description">
                  Install the Preloop CLI and discover local running agents
                  automatically
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
        ${
          this.hideBack
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
              `
        }

        <div class="wizard-header">
          ${
            this.hideStepTitle
              ? nothing
              : html`
                  <h3 class="wizard-title">
                    Onboard Existing Agent via Preloop CLI
                  </h3>
                `
          }
          <p class="wizard-copy">
            Run these commands on the machine where your agents live. Takes
            about two minutes.
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
            <div class="command-label">
              3. Discover and onboard local agents
            </div>
            <div class="command-row">
              <code class="command-code">preloop agents discover</code>
              <sl-copy-button value="preloop agents discover"></sl-copy-button>
            </div>
          </div>
        </div>

        <p class="wizard-copy">
          The CLI lists what it finds — Claude Code, Cursor, Codex CLI,
          OpenClaw, Gemini CLI and more — and asks before onboarding each one.
          Onboarded agents appear on the Agents page; their first model call
          shows up under Sessions.
        </p>
      </div>
    `;
  }

  private renderCustomPathState() {
    return html`
      <div class="wizard-shell">
        ${
          this.hideBack
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
              `
        }
        ${
          this.customSubStep === 'name'
            ? this.renderCustomNameState()
            : this.customSubStep === 'models'
              ? this.renderCustomModelState()
              : this.renderCustomResultState()
        }
      </div>
    `;
  }

  private renderCustomNameState() {
    return html`
      <div class="wizard-header">
        ${
          this.hideStepTitle
            ? nothing
            : html`<h3 class="wizard-title">Connect a custom agent</h3>`
        }
        <p class="wizard-copy">
          Register an existing agent (LangGraph, custom SDK) the CLI can't
          discover. We'll mint a gateway credential so it can route model
          traffic through Preloop.
        </p>
      </div>

      <div class="wizard-panel custom-form">
        ${
          this.customError
            ? html`
                <sl-alert variant="danger" open class="custom-error">
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${this.customError}
                </sl-alert>
              `
            : nothing
        }

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

        <sl-input
          label="Tags (optional)"
          name="tags"
          placeholder="e.g. env=prod team=support beta"
          help-text="Space-separated. Use key=value for pairs, or just key for a label."
          ?disabled=${this.customBusy}
          .value=${this.customTagsInput}
          @sl-input=${(e: Event) => {
            this.customTagsInput = (e.target as HTMLInputElement).value;
          }}
        ></sl-input>

        <sl-button
          variant="primary"
          ?disabled=${!this.customDisplayName.trim()}
          @click=${this.handleCustomContinueToModels}
        >
          Continue
        </sl-button>
      </div>
    `;
  }

  // NOTE: Available-tools selection is intentionally NOT part of this flow.
  // The reusable `tools-editor-component` operates on tools already DISCOVERED
  // from MCP/HTTP servers and built-ins, and persists governance via separate
  // per-subject endpoints; it has no notion of "define this new agent's tool
  // surface" and no register-time persistence path. Wiring it here would require
  // new backend endpoints (out of scope for this task — account.py must not
  // change). Tool selection is therefore deferred pending a dedicated backend
  // endpoint. Tags + models ship now.
  private renderCustomModelState() {
    const enabledModels = this.gatewayEnabledModels();
    const hasModels = enabledModels.length > 0;
    const canRegister =
      !this.customBusy &&
      (!hasModels || this.customSelectedModelIds.length > 0);

    return html`
      <div class="wizard-header">
        ${
          this.hideStepTitle
            ? nothing
            : html`<h3 class="wizard-title">Choose allowed models</h3>`
        }
        <p class="wizard-copy">
          Pick which models this agent may use through the Preloop gateway. The
          first model becomes the default the example snippet routes to.
        </p>
      </div>

      <div class="wizard-panel custom-form">
        ${
          this.customError
            ? html`
                <sl-alert variant="danger" open class="custom-error">
                  <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
                  ${this.customError}
                </sl-alert>
              `
            : nothing
        }
        ${
          hasModels
            ? html`
                <sl-select
                  label="Allowed models"
                  name="allowed_models"
                  multiple
                  clearable
                  placeholder="Select one or more models"
                  ?disabled=${this.customBusy}
                  .value=${this.customSelectedModelIds}
                  @sl-change=${(e: Event) => {
                    const value = (
                      e.target as HTMLSelectElement & {
                        value: string[] | string;
                      }
                    ).value;
                    this.customSelectedModelIds = Array.isArray(value)
                      ? value
                      : [value].filter(Boolean);
                    this.customError = '';
                  }}
                >
                  ${enabledModels.map(
                    (model) => html`
                      <sl-option value=${model.id}>
                        ${model.name} (${this.gatewayAliasForModel(model)})
                      </sl-option>
                    `
                  )}
                </sl-select>
              `
            : html`
                <sl-alert variant="primary" open class="custom-error">
                  <sl-icon slot="icon" name="info-circle"></sl-icon>
                  No gateway-enabled models are configured for this account. You
                  can register the agent now and set its allowed model later.
                </sl-alert>
              `
        }

        <sl-button
          variant="primary"
          ?loading=${this.customBusy}
          ?disabled=${!canRegister}
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
        ${
          this.hideStepTitle
            ? nothing
            : html`<h3 class="wizard-title">Your agent is connected</h3>`
        }
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

        ${this.renderCustomConnStatus()}

        <sl-button
          variant=${
            this.customConnState === 'connected' ? 'success' : 'primary'
          }
          @click=${() => {
            this.cancelFirstDataPolling();
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

  private renderCustomConnStatus() {
    if (this.customConnState === 'connected') {
      const count = this.customConnRequestCount;
      const label =
        count > 0
          ? `Agent connected — ${count} request${count === 1 ? '' : 's'} received`
          : 'Agent connected';
      return html`
        <sl-alert variant="success" open class="conn-status">
          <sl-icon slot="icon" name="check-circle"></sl-icon>
          ${label}
        </sl-alert>
      `;
    }
    return html`
      <div class="conn-status conn-waiting">
        <sl-spinner></sl-spinner>
        <span>Waiting for your agent's first request…</span>
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

        ${
          this.deploySubStep === 'type'
            ? html`
                <div class="wizard-header">
                  <h3 class="wizard-title">Deploy New Agent or Flow</h3>
                  <p class="wizard-copy">
                    Choose how the new agent should run.
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
                          Run a long-lived agent that stays connected and picks
                          up work continuously.
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
                          Start an agent when an event fires (issue created,
                          webhook), stop it when the run completes.
                        </span>
                      </span>
                    </div>
                  </sl-button>
                </div>
              `
            : nothing
        }
        ${
          this.deploySubStep === 'flow-config'
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
            : nothing
        }
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'preloop-deploy-wizard': PreloopDeployWizard;
  }
}
