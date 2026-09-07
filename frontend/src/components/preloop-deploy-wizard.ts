import { LitElement, html, nothing, unsafeCSS } from 'lit';
import type { TemplateResult } from 'lit';
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
  getAccountAgents,
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
import consoleStyles from '../styles/console-styles.css?inline';
import { deployWizardStyles } from '../styles/deploy-wizard';

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
// Absolute wall-clock cap for CLI-path polling. Soft deadline can extend on
// each new agent arrival, but never past this hard stop.
const CLI_AGENT_POLL_HARD_CAP_MS = 10 * 60 * 1000;
// How long the "Connected" success state stays up before auto-dismiss.
const FIRST_DATA_SUCCESS_DISMISS_MS = 2000;

@customElement('preloop-deploy-wizard')
export class PreloopDeployWizard extends LitElement {
  static styles = [unsafeCSS(consoleStyles), deployWizardStyles];

  @property({ type: Array })
  aiModels: AIModel[] = [];

  /**
   * When the host already fetches `GET /ai-models` (Overview), skip the
   * connect-time lookup so the page does not pay for the list twice.
   */
  @property({ type: Boolean })
  modelsFromHost = false;

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

  // CLI-path polling: while the CLI command screen is open we watch the
  // managed-agents list so the browser visibly reacts as `preloop agents
  // discover` onboards agents in the terminal. Reuses the custom-path poll
  // cadence and cap; polls only while the CLI screen is showing.
  @state()
  private cliConnectedAgents: ManagedAgentSummary[] = [];

  // Agent ids that already existed when the CLI screen opened — only agents
  // onboarded after that count as newly connected.
  private cliBaselineIds: Set<string> | null = null;

  @state()
  private cliPollingActive = false;

  private cliPollTimer: number | null = null;
  private cliPollDeadline = 0;
  private cliPollHardStop = 0;

  async connectedCallback() {
    super.connectedCallback();
    this.onboardingPath = this.initialPath;
    if (this.initialPath === 'deploy') {
      this.deploySubStep = 'type';
    }
    if (this.initialPath === 'custom') {
      this.resetCustomState();
    }
  }

  protected async firstUpdated(
    changedProperties: Map<string, unknown>
  ): Promise<void> {
    super.firstUpdated(changedProperties);
    // Host properties are assigned before firstUpdated. Overview passes
    // modelsFromHost so this page does not fetch GET /ai-models twice.
    if (this.aiModels.length === 0 && !this.modelsFromHost) {
      this.aiModels = await getAIModels().catch(() => []);
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    // Cancel any in-flight polling / dismiss timers so a torn-down wizard does
    // not keep firing fetches or dispatch events after it is gone.
    this.cancelFirstDataPolling();
    this.cancelCliAgentPolling();
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
    // Poll for CLI-onboarded agents only while the CLI command screen is
    // showing (same lifecycle as the custom-path first-data poll).
    if (changedProperties.has('onboardingPath')) {
      if (this.onboardingPath === 'cli') {
        this.startCliAgentPolling();
      } else {
        this.cancelCliAgentPolling();
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

  /**
   * Begin watching the managed-agents list while the CLI command screen is
   * open. The first fetch snapshots the agents that already exist (the
   * baseline); every agent that appears afterwards was just onboarded by the
   * CLI and flips the screen to a "✓ <agent> connected" line. Polling reuses
   * the custom-path cadence/cap and stops on navigation, teardown, or timeout.
   */
  private startCliAgentPolling() {
    this.cancelCliAgentPolling();
    this.cliConnectedAgents = [];
    this.cliBaselineIds = null;
    this.cliPollingActive = true;
    const now = Date.now();
    this.cliPollHardStop = now + CLI_AGENT_POLL_HARD_CAP_MS;
    this.cliPollDeadline = Math.min(
      now + FIRST_DATA_POLL_TIMEOUT_MS,
      this.cliPollHardStop
    );
    const poll = async () => {
      if (!this.cliPollingActive) {
        return;
      }
      try {
        const response = await getAccountAgents({ limit: 100 });
        const items = response.items || [];
        if (!this.cliPollingActive) {
          return;
        }
        if (this.cliBaselineIds === null) {
          this.cliBaselineIds = new Set(items.map((agent) => agent.id));
        } else {
          const knownIds = new Set(
            this.cliConnectedAgents.map((agent) => agent.id)
          );
          const arrivals = items.filter(
            (agent) =>
              !this.cliBaselineIds!.has(agent.id) && !knownIds.has(agent.id)
          );
          if (arrivals.length > 0) {
            this.cliConnectedAgents = [...this.cliConnectedAgents, ...arrivals];
            // Agents are still landing — extend the soft window, but never
            // past the absolute wall-clock hard stop.
            this.cliPollDeadline = Math.min(
              Date.now() + FIRST_DATA_POLL_TIMEOUT_MS,
              this.cliPollHardStop
            );
          }
        }
      } catch {
        // Transient errors are ignored; we keep polling until the deadline.
      }
      if (!this.cliPollingActive) {
        return;
      }
      if (Date.now() >= this.cliPollDeadline) {
        // Cap reached: stop polling quietly. Connected lines stay visible.
        this.cliPollingActive = false;
        this.cliPollTimer = null;
        return;
      }
      this.cliPollTimer = window.setTimeout(poll, FIRST_DATA_POLL_INTERVAL_MS);
    };
    // Snapshot the baseline immediately so the first CLI-onboarded agent is
    // detected one interval later, not two.
    void poll();
  }

  /** Stop the CLI-path agents-list polling and clear its timer. */
  private cancelCliAgentPolling() {
    this.cliPollingActive = false;
    this.cliBaselineIds = null;
    if (this.cliPollTimer !== null) {
      window.clearTimeout(this.cliPollTimer);
      this.cliPollTimer = null;
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

  /**
   * Where this screen sits in the path. `total` is null on a screen that
   * branches: the wizard cannot honestly promise a length before the user has
   * picked a path, so the header shows the number alone rather than inventing
   * a total it may not keep.
   */
  private stepPosition(): { index: number; total: number | null } {
    // The choose screen only counts as a step when the wizard starts there.
    // Deep links (initial-path="govern" from the agents view) start at 1.
    const base = this.initialPath === 'choose' ? 1 : 0;
    switch (this.onboardingPath) {
      case 'choose':
        return { index: 1, total: null };
      case 'govern':
        return { index: base + 1, total: null };
      case 'cli':
        return { index: base + 2, total: base + 2 };
      case 'custom': {
        const first = base + 2;
        const offset =
          this.customSubStep === 'name'
            ? 0
            : this.customSubStep === 'models'
              ? 1
              : 2;
        return { index: first + offset, total: first + 2 };
      }
      default:
        return this.deploySubStep === 'type'
          ? { index: base + 1, total: null }
          : { index: base + 2, total: base + 2 };
    }
  }

  /** Steps already taken when the nested deployer takes over the screen. */
  private deployerStepOffset(): number {
    return (this.initialPath === 'choose' ? 1 : 0) + 1;
  }

  /**
   * Every step opens the same way: which step this is, what it is for, and one
   * line of context. The rail is only drawn when the total is known.
   */
  private renderStepHeader(title: string, copy: unknown) {
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
        ${
          this.hideStepTitle
            ? nothing
            : html`<h3 class="wizard-title">${title}</h3>`
        }
        <p class="wizard-copy">${copy}</p>
      </div>
    `;
  }

  /**
   * One action bar per step: the primary action on the right, Back as a text
   * button on the left. Back stays in the DOM before the primary so the
   * keyboard reaches the step's main action last.
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

  /** A choice on a branch screen: icon column, title, one line, chevron. */
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

  /** A labelled, copyable command. Long commands wrap; they never clip. */
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

  private renderError() {
    if (!this.customError) {
      return nothing;
    }
    return html`
      <div class="notice danger" role="alert">
        <sl-icon name="exclamation-octagon"></sl-icon>
        <span>${this.customError}</span>
      </div>
    `;
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
        ${this.renderStepHeader(
          'Choose how to start',
          `Preloop is the open source control plane for AI agents. Connect the
           agents you already run, or deploy a new one.`
        )}
        <div class="wizard-card-grid">
          ${this.renderOptionCard(
            'shield-check',
            'Govern Existing Agents',
            'Connect agents you already run, via CLI autodiscovery or by onboarding a custom agent.',
            () => {
              this.onboardingPath = 'govern';
              this.requestUpdate();
            }
          )}
          ${this.renderOptionCard(
            'cloud-arrow-up',
            'Deploy New Agents',
            'Spin up a new persistent agent or an event-driven flow.',
            () => {
              this.onboardingPath = 'deploy';
              this.deploySubStep = 'type';
              this.requestUpdate();
            }
          )}
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
        ${this.renderStepHeader(
          'Govern existing agents',
          `Bring agents you already run under Preloop's control plane. The CLI
           can autodiscover local agents, or you can onboard a custom agent it
           cannot reach.`
        )}

        <div class="wizard-card-grid">
          ${this.renderOptionCard(
            'terminal',
            'Autodiscover via CLI',
            'Install the Preloop CLI and discover local running agents automatically.',
            () => {
              this.onboardingPath = 'cli';
              this.requestUpdate();
            }
          )}
          ${this.renderOptionCard(
            'plug',
            'Connect a custom agent',
            "Onboard an existing agent (LangGraph, custom SDK) the CLI can't discover.",
            () => {
              this.resetCustomState();
              this.onboardingPath = 'custom';
              this.requestUpdate();
            }
          )}
        </div>

        ${this.renderActions(!hideGovernBack)}
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
        ${this.renderStepHeader(
          'Install the CLI and discover agents',
          `Run these three commands on the machine where your agents live. It
           takes about two minutes.`
        )}

        <div class="command-steps">
          ${this.renderCommandStep(
            'Install the Preloop CLI tool',
            installCommand,
            1
          )}
          ${this.renderCommandStep('Authenticate the CLI session', loginCommand, 2)}
          ${this.renderCommandStep(
            'Discover and onboard local agents',
            'preloop agents discover',
            3
          )}
          ${this.renderCliConnStatus()}
        </div>

        <p class="wizard-copy">
          The CLI lists what it finds (Claude Code, Cursor, Codex CLI, OpenClaw,
          Gemini CLI and more) and asks before onboarding each one. Onboarded
          agents appear on the Agents page; their first model call shows up
          under Sessions.
        </p>

        ${this.renderActions(!this.hideBack)}
      </div>
    `;
  }

  /**
   * Live status under the CLI command boxes: a waiting line while we watch
   * for CLI-onboarded agents, flipping to one "✓ <agent> connected" line per
   * agent as they land, with a link to the Agents page.
   */
  private renderCliConnStatus() {
    const connected = this.cliConnectedAgents;
    if (!this.cliPollingActive && connected.length === 0) {
      return nothing;
    }
    return html`
      <div class="conn-status">
        ${connected.map(
          (agent) => html`
            <div class="cli-connected-line">
              ✓ ${agent.display_name} connected
            </div>
          `
        )}
        ${
          connected.length > 0
            ? html`
                <a class="cli-connected-link" href="/console/agents"
                  >View it on the Agents page</a
                >
              `
            : nothing
        }
        ${
          this.cliPollingActive
            ? html`
                <div class="conn-waiting">
                  <sl-spinner></sl-spinner>
                  <span>
                    Waiting for the CLI. Onboarded agents appear here
                    automatically.
                  </span>
                </div>
              `
            : nothing
        }
      </div>
    `;
  }

  private renderCustomPathState() {
    return html`
      <div class="wizard-shell">
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
      ${this.renderStepHeader(
        'Name the agent',
        `Register an existing agent (LangGraph, custom SDK) the CLI cannot
         discover. Preloop mints a gateway credential so the agent can route
         model traffic through the control plane.`
      )}

      <div class="wizard-form">
        ${this.renderError()}

        <sl-input
          label="Agent name"
          name="display_name"
          help-text="The name you will recognise in the console, for example Support triage agent."
          required
          ?disabled=${this.customBusy}
          .value=${this.customDisplayName}
          @sl-input=${(e: Event) => {
            this.customDisplayName = (e.target as HTMLInputElement).value;
          }}
        ></sl-input>

        <sl-textarea
          label="Description"
          name="description"
          help-text="Optional. One line on what this agent does."
          rows="2"
          ?disabled=${this.customBusy}
          .value=${this.customDescription}
          @sl-input=${(e: Event) => {
            this.customDescription = (e.target as HTMLTextAreaElement).value;
          }}
        ></sl-textarea>

        <sl-input
          label="Tags"
          name="tags"
          help-text="Optional. Space-separated: key=value for pairs, or just key for a label. For example env=prod team=support."
          ?disabled=${this.customBusy}
          .value=${this.customTagsInput}
          @sl-input=${(e: Event) => {
            this.customTagsInput = (e.target as HTMLInputElement).value;
          }}
        ></sl-input>
      </div>

      ${this.renderActions(
        !this.hideBack,
        html`
          <sl-button
            variant="primary"
            ?disabled=${!this.customDisplayName.trim()}
            @click=${this.handleCustomContinueToModels}
          >
            Continue
          </sl-button>
        `
      )}
    `;
  }

  // NOTE: Available-tools selection is intentionally NOT part of this flow.
  // The reusable `tools-editor-component` operates on tools already DISCOVERED
  // from MCP/HTTP servers and built-ins, and persists governance via separate
  // per-subject endpoints; it has no notion of "define this new agent's tool
  // surface" and no register-time persistence path. Wiring it here would require
  // new backend endpoints (out of scope for this task: account.py must not
  // change). Tool selection is therefore deferred pending a dedicated backend
  // endpoint. Tags + models ship now.
  private renderCustomModelState() {
    const enabledModels = this.gatewayEnabledModels();
    const hasModels = enabledModels.length > 0;
    const canRegister =
      !this.customBusy &&
      (!hasModels || this.customSelectedModelIds.length > 0);

    return html`
      ${this.renderStepHeader(
        'Choose allowed models',
        `Pick the models this agent may use through the Preloop gateway. The
         first one becomes the default the example snippet routes to.`
      )}

      <div class="wizard-form">
        ${this.renderError()}
        ${
          hasModels
            ? html`
                <sl-select
                  label="Allowed models"
                  name="allowed_models"
                  multiple
                  clearable
                  placeholder="Select one or more models"
                  help-text="The agent may only call the models you grant it here."
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
                <div class="notice info">
                  <sl-icon name="info-circle"></sl-icon>
                  <span>
                    No gateway-enabled models are configured for this account.
                    You can register the agent now and set its allowed model
                    later.
                  </span>
                </div>
              `
        }
        ${this.renderCustomSummary(enabledModels)}
      </div>

      ${this.renderActions(
        !this.hideBack,
        html`
          <sl-button
            variant="primary"
            ?loading=${this.customBusy}
            ?disabled=${!canRegister}
            @click=${this.handleCustomRegister}
          >
            Register agent &amp; mint credential
          </sl-button>
        `
      )}
    `;
  }

  /**
   * What the next click is about to create. Registering mints a credential
   * that is shown once, so the last screen before it states the agent it is
   * about to register rather than asking the user to remember two screens
   * back.
   */
  private renderCustomSummary(enabledModels: AIModel[]) {
    const selected = enabledModels.filter((model) =>
      this.customSelectedModelIds.includes(model.id)
    );
    const tags = Object.entries(this.parseCustomTags()).map(
      ([key, value]) => `${key}=${value}`
    );
    const row = (key: string, value: unknown) => html`
      <div class="wizard-summary-row">
        <span class="wizard-summary-key">${key}</span>
        <span class="wizard-summary-value">${value}</span>
      </div>
    `;
    return html`
      <div class="wizard-summary">
        <div class="wizard-summary-title">About to register</div>
        ${row('Agent', this.customDisplayName.trim() || 'Not set')}
        ${
          this.customDescription.trim()
            ? row('Description', this.customDescription.trim())
            : nothing
        }
        ${tags.length > 0 ? row('Tags', tags.join(' ')) : nothing}
        ${row(
          'Models',
          selected.length > 0
            ? selected.map((model) => model.name).join(', ')
            : 'None selected'
        )}
        ${row('Credential', 'One gateway credential, shown once')}
      </div>
    `;
  }

  private renderCustomResultState() {
    const baseUrl = this.buildGatewayBaseUrl();
    const token = this.customCredentialToken || '';
    const snippet = this.buildCustomSnippet(baseUrl, token);

    return html`
      ${this.renderStepHeader(
        'Your agent is connected',
        `Point your agent at the Preloop gateway with the base URL and
         credential below, then route its model traffic through it.`
      )}

      <div class="notice warning">
        <sl-icon name="exclamation-triangle"></sl-icon>
        <span>
          This credential token is shown only once and cannot be recovered. Copy
          it now and store it securely.
        </span>
      </div>

      <div class="command-steps">
        ${this.renderCommandStep(
          'Gateway base URL (OpenAI-compatible)',
          baseUrl
        )}
        ${this.renderCommandStep('Gateway credential (api_key)', token)}

        <div class="command-step">
          <div class="command-label">
            <span
              >Example: LangGraph + OpenAI SDK with a per-run session id</span
            >
          </div>
          <div class="command-row">
            <pre class="command-code command-snippet">${snippet}</pre>
            <sl-copy-button .value=${snippet}></sl-copy-button>
          </div>
        </div>

        ${this.renderCustomConnStatus()}
      </div>

      ${this.renderActions(
        !this.hideBack,
        html`
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
        `
      )}
    `;
  }

  private renderCustomConnStatus() {
    if (this.customConnState === 'connected') {
      const count = this.customConnRequestCount;
      const label =
        count > 0
          ? `Agent connected: ${count} request${count === 1 ? '' : 's'} received`
          : 'Agent connected';
      return html`
        <div class="conn-status">
          <div class="conn-connected">
            <sl-icon name="check-circle"></sl-icon>
            <span>${label}</span>
          </div>
        </div>
      `;
    }
    return html`
      <div class="conn-status">
        <div class="conn-waiting">
          <sl-spinner></sl-spinner>
          <span>Waiting for your agent's first request…</span>
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
          .stepOffset=${this.deployerStepOffset()}
          @deploy-agent-success=${this.handleAgentDeploySuccess}
          @deploy-cancel=${this.handleAgentDeployCancel}
        ></preloop-agent-deployer>
      `;
    }

    if (this.deploySubStep === 'flow-config') {
      return html`
        <div class="wizard-shell wide">
          ${this.renderStepHeader(
            'Configure the event-driven flow',
            `Start an agent when an event fires (issue created, webhook) and
             stop it when the run completes.`
          )}
          <div class="wizard-section">
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
                  form.formError = error?.message || 'Failed to create flow.';
                }
              }}
              @flow-cancel=${() => {
                this.deploySubStep = 'type';
              }}
            ></preloop-flow-form>
          </div>
        </div>
      `;
    }

    return html`
      <div class="wizard-shell">
        ${this.renderStepHeader(
          'Deploy a new agent',
          'Choose how the new agent should run.'
        )}

        <div class="wizard-card-grid">
          ${this.renderOptionCard(
            'server',
            'Deploy Persistent Agent',
            'Run a long-lived agent that stays connected and picks up work continuously.',
            () => {
              this.deploySubStep = 'agent-host';
              this.requestUpdate();
            }
          )}
          ${this.renderOptionCard(
            'diagram-3',
            'Configure Event-Driven Flow',
            'Start an agent when an event fires (issue created, webhook), stop it when the run completes.',
            () => {
              this.deploySubStep = 'flow-config';
              this.requestUpdate();
            }
          )}
        </div>

        ${this.renderActions(!this.hideBack && this.initialPath !== 'deploy')}
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'preloop-deploy-wizard': PreloopDeployWizard;
  }
}
