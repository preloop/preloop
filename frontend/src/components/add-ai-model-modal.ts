import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { when } from 'lit/directives/when.js';
import { repeat } from 'lit/directives/repeat.js';
import {
  getAvailableModelsForProvider,
  createAIModel,
  updateAIModel,
  type AvailableModelsResult,
  type AwsDiscoveryAuth,
} from '../api';
import type { AIModel } from '../types';
import type SlSelect from '@shoelace-style/shoelace/dist/components/select/select.js';
import type SlInput from '@shoelace-style/shoelace/dist/components/input/input.js';

import '@shoelace-style/shoelace/dist/components/dialog/dialog.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/select/select.js';
import '@shoelace-style/shoelace/dist/components/option/option.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '@shoelace-style/shoelace/dist/components/alert/alert.js';
import '@shoelace-style/shoelace/dist/components/spinner/spinner.js';
import '@shoelace-style/shoelace/dist/components/checkbox/checkbox.js';

type ServiceKind = 'llm' | 'stt' | 'tts';

interface ProviderOption {
  value: string;
  label: string;
  serviceKinds: ServiceKind[];
}

const PROVIDER_OPTIONS: ProviderOption[] = [
  { value: 'openai', label: 'OpenAI', serviceKinds: ['llm', 'stt', 'tts'] },
  { value: 'anthropic', label: 'Anthropic', serviceKinds: ['llm'] },
  { value: 'moonshot', label: 'Moonshot (Kimi)', serviceKinds: ['llm'] },
  { value: 'google', label: 'Google', serviceKinds: ['llm', 'stt'] },
  { value: 'qwen', label: 'Qwen', serviceKinds: ['llm'] },
  { value: 'deepseek', label: 'DeepSeek', serviceKinds: ['llm'] },
  { value: 'zai', label: 'Z.ai (GLM)', serviceKinds: ['llm'] },
  { value: 'mistral', label: 'Mistral', serviceKinds: ['llm'] },
  { value: 'bedrock', label: 'AWS Bedrock', serviceKinds: ['llm'] },
  { value: 'openrouter', label: 'OpenRouter', serviceKinds: ['llm'] },
  {
    value: 'openai-compatible',
    label: 'OpenAI-compatible',
    serviceKinds: ['llm', 'stt', 'tts'],
  },
  { value: 'custom', label: 'Custom', serviceKinds: ['llm', 'stt', 'tts'] },
];

/**
 * Base URLs we already know, prefilled when the provider is chosen. Empty
 * string means "the user has to supply it". OpenRouter has a fixed base URL,
 * so it is prefilled here and also defaulted server-side
 * (ai_model_provider.DEFAULT_PROVIDER_ENDPOINTS); the form still needs a value
 * because api_endpoint is a required field on submit.
 */
const PROVIDER_DEFAULT_ENDPOINTS: Record<string, string> = {
  openai: 'https://api.openai.com/v1',
  anthropic: 'https://api.anthropic.com/v1',
  google: 'https://generativelanguage.googleapis.com/v1beta',
  qwen: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  deepseek: 'https://api.deepseek.com/v1',
  moonshot: 'https://api.moonshot.ai/v1',
  zai: 'https://api.z.ai/api/paas/v4',
  mistral: 'https://api.mistral.ai/v1',
  openrouter: 'https://openrouter.ai/api/v1',
  'openai-compatible': '',
  custom: '',
};

/**
 * Providers with no fixed model catalog: their models are listed from the
 * endpoint's own OpenAI-compatible GET /models. Fetching needs an endpoint, so
 * one must be present before we can ask; for providers with a known default
 * (OpenRouter) that is already satisfied without the user typing anything.
 */
const ENDPOINT_LISTED_PROVIDERS = ['openai-compatible', 'custom', 'openrouter'];

/**
 * Default AWS region prefilled for the bedrock provider. Bedrock model
 * availability is regional, so the region must be resolved before listing.
 */
const BEDROCK_DEFAULT_REGION = 'us-east-1';

/**
 * Assemble the credential blob stored for a bedrock model. The gateway
 * (openai_gateway._bedrock_credential_kwargs) parses this JSON back into
 * litellm's aws_* kwargs, so the shape here is load-bearing.
 */
function buildBedrockCredentialBlob(creds: {
  accessKeyId?: string;
  secretAccessKey?: string;
  sessionToken?: string;
}): string | undefined {
  if (!creds.accessKeyId?.trim() || !creds.secretAccessKey?.trim()) {
    return undefined;
  }
  const payload: Record<string, string> = {
    aws_access_key_id: creds.accessKeyId.trim(),
    aws_secret_access_key: creds.secretAccessKey.trim(),
  };
  if (creds.sessionToken?.trim()) {
    payload.aws_session_token = creds.sessionToken.trim();
  }
  return JSON.stringify(payload);
}

/**
 * Human wording for the server's fixed fallback-reason vocabulary.
 *
 * Keys must stay in step with `AvailableModelsFallbackReason` in api.ts, which
 * mirrors the server's set. Anything unrecognized falls back to a generic
 * phrase rather than being rendered, so a server-side code can never put raw
 * upstream text on screen.
 */
const FALLBACK_REASON_LABELS: Record<string, string> = {
  timeout: 'timed out',
  network: 'network error',
  empty_response: 'empty response',
  unsupported: 'live listing not supported',
  missing_endpoint: 'no API endpoint configured',
  sdk_missing: 'provider SDK not installed',
  unknown: 'provider unavailable',
};

/**
 * Reusable AI model add/edit dialog.
 *
 * Usage:
 *   <add-ai-model-modal
 *     ?open=${this.isOpen}
 *     .model=${modelToEdit}        <!-- null/undefined for "Add" mode -->
 *     @model-created=${handler}     <!-- detail: { model } -->
 *     @model-updated=${handler}     <!-- detail: { model } -->
 *     @close-modal=${handler}
 *   ></add-ai-model-modal>
 */
@customElement('add-ai-model-modal')
export class AddAIModelModal extends LitElement {
  static styles = css`
    sl-dialog::part(panel) {
      width: 620px;
    }
    .form-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
    }
    .full-width {
      grid-column: 1 / -1;
    }
  `;

  /** Whether the dialog is open. */
  @property({ type: Boolean })
  open = false;

  /**
   * If provided, the dialog opens in "Edit" mode for this model.
   * Pass null / undefined for "Add" mode.
   */
  @property({ type: Object })
  model: AIModel | null = null;

  // ── internal state ───────────────────────────────────

  @state() private _currentModel: Partial<AIModel> = {};
  @state() private _formError: string | null = null;
  @state() private _isSubmitting = false;
  @state() private _modelSuggestions: string[] = [];
  @state() private _isOtherModel = false;
  @state() private _isFetchingModels = false;
  @state() private _modelsFetchError: string | null = null;
  /** Where the current suggestions came from: null until a fetch completes. */
  @state() private _modelsSource: 'live' | 'fallback' | null = null;
  /** Short safe reason code accompanying a fallback listing. */
  @state() private _modelsFallbackReason: string | null = null;
  /**
   * Extra models to create alongside the primary one, all sharing the single
   * provider key entered on this form. Create-mode only.
   */
  @state() private _additionalModelIds: string[] = [];
  /** When true, register this model for Preloop gateway routing (requires upstream API key). */
  @state() private _preloopGatewayEnabled = true;
  /** AWS credential inputs for the bedrock provider. */
  @state() private _bedrockAccessKeyId = '';
  @state() private _bedrockSecretAccessKey = '';
  @state() private _bedrockSessionToken = '';
  @state() private _bedrockRegion = BEDROCK_DEFAULT_REGION;

  private get _isEditing(): boolean {
    return !!this.model;
  }

  private get _isBedrock(): boolean {
    return this._currentModel.provider_name === 'bedrock';
  }

  /** True once enough AWS material is present to attempt a live listing. */
  private get _bedrockCredsComplete(): boolean {
    return Boolean(
      this._bedrockAccessKeyId.trim() &&
      this._bedrockSecretAccessKey.trim() &&
      this._bedrockRegion.trim()
    );
  }

  private get _canEnablePreloopGateway(): boolean {
    const apiKey = (this._currentModel.api_key || '').trim();
    const hasStoredKey = Boolean(this._isEditing && this.model?.has_api_key);
    return (
      this._currentModel.model_kind === 'llm' &&
      (apiKey.length > 0 || hasStoredKey)
    );
  }

  private get _selectedServiceKind(): ServiceKind {
    return (this._currentModel.model_kind || 'llm') as ServiceKind;
  }

  private get _availableProviders(): ProviderOption[] {
    const serviceKind = this._selectedServiceKind;
    return PROVIDER_OPTIONS.filter((provider) =>
      provider.serviceKinds.includes(serviceKind)
    );
  }

  // ── lifecycle ────────────────────────────────────────

  updated(changedProps: Map<string, unknown>) {
    if (changedProps.has('open') && this.open) {
      this._populateForm();
    }
  }

  // ── form helpers ─────────────────────────────────────

  private _populateForm() {
    if (this.model) {
      this._currentModel = {
        ...this.model,
        model_kind: this.model.model_kind || 'llm',
      };
      const gw = this.model.meta_data?.gateway;
      if (gw && typeof gw === 'object' && 'enabled' in gw) {
        this._preloopGatewayEnabled = Boolean(
          (gw as { enabled?: boolean }).enabled
        );
      } else {
        this._preloopGatewayEnabled = true;
      }
    } else {
      this._currentModel = { model_kind: 'llm' };
      this._preloopGatewayEnabled = true;
    }
    this._modelSuggestions = [];
    this._isOtherModel = false;
    this._additionalModelIds = [];
    this._formError = null;
    this._isSubmitting = false;
    this._isFetchingModels = false;
    this._modelsFetchError = null;
    this._modelsSource = null;
    this._modelsFallbackReason = null;
    this._resetBedrockFields();
    // Restore the persisted routing region so editing a model configured for
    // e.g. eu-west-1 does not silently re-route it to the default region on
    // save (the gateway reads the region from meta_data.provider_runtime).
    if (this.model) {
      const runtime = this.model.meta_data?.provider_runtime;
      const storedRegion =
        runtime && typeof runtime === 'object'
          ? (runtime as { region?: unknown }).region
          : undefined;
      if (typeof storedRegion === 'string' && storedRegion.trim()) {
        this._bedrockRegion = storedRegion.trim();
      }
    }
  }

  /** Clear the AWS credential inputs; editing falls back to "keep stored key". */
  private _resetBedrockFields() {
    this._bedrockAccessKeyId = '';
    this._bedrockSecretAccessKey = '';
    this._bedrockSessionToken = '';
    this._bedrockRegion = BEDROCK_DEFAULT_REGION;
  }

  /**
   * Mirror the assembled AWS credential blob into `api_key` so submit,
   * gateway gating, and extra-model creation all keep working unchanged:
   * for bedrock the stored secret IS this JSON payload.
   */
  private _syncBedrockApiKey() {
    if (!this._isBedrock) return;
    this._currentModel.api_key =
      buildBedrockCredentialBlob({
        accessKeyId: this._bedrockAccessKeyId,
        secretAccessKey: this._bedrockSecretAccessKey,
        sessionToken: this._bedrockSessionToken,
      }) || undefined;
  }

  /**
   * @param modelIdOverride Build the gateway alias for this model id instead of
   *   the primary one. Used when creating extra models that share one key —
   *   each needs its own alias or they would all resolve to the same entry.
   */
  private _buildMetaDataForSubmit(
    modelIdOverride?: string
  ): Record<string, unknown> {
    const existing =
      this._isEditing &&
      !modelIdOverride &&
      this.model?.meta_data &&
      typeof this.model.meta_data === 'object'
        ? { ...this.model.meta_data }
        : {};
    const provider = this._currentModel.provider_name;
    const modelId = modelIdOverride ?? this._currentModel.model_identifier;
    const modelKind = this._currentModel.model_kind || 'llm';
    const baseMeta: Record<string, unknown> = {
      ...existing,
      service_kind: modelKind,
    };
    if (!provider || !modelId) {
      return baseMeta;
    }
    if (provider === 'bedrock') {
      // The gateway reads the routing region from here
      // (openai_gateway._bedrock_region) when litellm needs aws_region_name.
      baseMeta.provider_runtime = {
        ...(baseMeta.provider_runtime as Record<string, unknown> | undefined),
        region: this._bedrockRegion.trim() || BEDROCK_DEFAULT_REGION,
      };
    }
    const gatewayEnabled =
      modelKind === 'llm' &&
      this._preloopGatewayEnabled &&
      this._canEnablePreloopGateway;
    return {
      ...baseMeta,
      gateway: {
        enabled: gatewayEnabled,
        provider_adapter: 'preloop',
        model_alias: `${String(provider).toLowerCase()}/${modelId}`,
      },
    };
  }

  /**
   * Read current input values directly from shadow DOM elements.
   *
   * Fields are located by their stable `data-field` attribute, never by the
   * visible label text: renaming or translating a label must not silently
   * stop a field from syncing into the submitted model.
   */
  private _syncFormFromDom() {
    const inputs = this.shadowRoot?.querySelectorAll('[data-field]') ?? [];
    for (const input of inputs) {
      const field = input.getAttribute('data-field');
      const val = (input as any).value as string;
      if (field === 'name') this._currentModel.name = val || undefined;
      else if (field === 'api_endpoint' && val)
        this._currentModel.api_endpoint = val;
      else if (field === 'api_key' && val) this._currentModel.api_key = val;
      else if (field === 'bedrock_access_key_id') {
        this._bedrockAccessKeyId = val || '';
      } else if (field === 'bedrock_secret_access_key') {
        this._bedrockSecretAccessKey = val || '';
      } else if (field === 'bedrock_session_token') {
        this._bedrockSessionToken = val || '';
      } else if (field === 'bedrock_region') {
        this._bedrockRegion = val || BEDROCK_DEFAULT_REGION;
      } else if (field === 'model_identifier')
        this._currentModel.model_identifier = val || undefined;
    }
    if (this._isBedrock) this._syncBedrockApiKey();
    const serviceKindSelect = this.shadowRoot?.querySelector(
      'sl-select[data-field="model_kind"]'
    ) as SlSelect | null;
    if (serviceKindSelect?.value) {
      this._currentModel.model_kind = serviceKindSelect.value as
        'llm' | 'stt' | 'tts';
    }
  }

  private _handleClose() {
    this.dispatchEvent(new CustomEvent('close-modal'));
  }

  private _handleRequestClose(event: CustomEvent) {
    const source = (event.detail as any).source;
    if (source === 'close-button' || !this._currentModel.provider_name) {
      this._handleClose();
    } else {
      event.preventDefault();
    }
  }

  // ── provider / model fetching ────────────────────────

  private async _handleProviderChange(e: Event) {
    const provider = (e.target as SlSelect).value as string;

    this._currentModel = {
      ...this._currentModel,
      provider_name: provider,
      api_endpoint: PROVIDER_DEFAULT_ENDPOINTS[provider] || '',
      model_identifier: '',
    };

    // Bedrock uses its own credential inputs; start from a clean slate with
    // a usable default region so only key + secret are required.
    this._resetBedrockFields();
    if (provider === 'bedrock') {
      this._currentModel.api_endpoint = '';
      this._currentModel.api_key = undefined;
    }

    this._modelSuggestions = [];
    this._isOtherModel = false;
    this._additionalModelIds = [];
    this._modelsFetchError = null;
    this._modelsSource = null;
    this._modelsFallbackReason = null;
    this.requestUpdate();
    if (this._selectedServiceKind !== 'llm') {
      void this._fetchModelsForCurrentProvider();
    }
  }

  private _getProviderKeyUrl(provider: string | undefined): string {
    switch (provider) {
      case 'openai':
        return 'https://platform.openai.com/api-keys';
      case 'anthropic':
        return 'https://console.anthropic.com/settings/keys';
      case 'google':
        return 'https://aistudio.google.com/app/apikey';
      case 'qwen':
        return 'https://dashscope.console.aliyun.com/apiKey';
      case 'deepseek':
        return 'https://platform.deepseek.com/api_keys';
      case 'moonshot':
        return 'https://platform.moonshot.ai/console/api-keys';
      case 'zai':
        return 'https://z.ai/manage-apikey/apikey-list';
      case 'mistral':
        return 'https://console.mistral.ai/api-keys';
      case 'openrouter':
        return 'https://openrouter.ai/keys';
      case 'openai-compatible':
      case 'custom':
        return 'https://platform.openai.com/api-keys';
      default:
        return '';
    }
  }

  private _handleServiceKindChange(e: Event) {
    const modelKind = (e.target as SlSelect).value as ServiceKind;
    const provider = this._currentModel.provider_name;
    const providerSupported = PROVIDER_OPTIONS.some(
      (option) =>
        option.value === provider && option.serviceKinds.includes(modelKind)
    );

    this._currentModel = {
      ...this._currentModel,
      model_kind: modelKind,
      provider_name: providerSupported ? provider : '',
      model_identifier: '',
    };
    if (modelKind !== 'llm') {
      this._preloopGatewayEnabled = false;
    }
    this._modelSuggestions = [];
    this._isOtherModel = false;
    this._additionalModelIds = [];
    this._modelsFetchError = null;
    this._modelsSource = null;
    this._modelsFallbackReason = null;
    this.requestUpdate();
    if (providerSupported && modelKind !== 'llm') {
      void this._fetchModelsForCurrentProvider();
    }
  }

  private async _fetchModelSuggestionsForProvider(
    provider: string,
    apiKey?: string,
    apiEndpoint?: string,
    awsAuth?: AwsDiscoveryAuth
  ): Promise<AvailableModelsResult> {
    return await getAvailableModelsForProvider(
      provider,
      apiKey,
      this._selectedServiceKind,
      apiEndpoint,
      awsAuth
    );
  }

  private async _fetchModelsForCurrentProvider() {
    if (!this._currentModel.provider_name) {
      this._modelsFetchError = 'Select a provider first';
      return;
    }

    const provider = this._currentModel.provider_name;
    // Bedrock authenticates with AWS credential fields, not a key/endpoint.
    let awsAuth: AwsDiscoveryAuth | undefined;
    if (provider === 'bedrock') {
      if (!this._bedrockCredsComplete) {
        this._modelsFetchError =
          'Enter the AWS access key, secret key and region first, then fetch models';
        return;
      }
      awsAuth = {
        accessKeyId: this._bedrockAccessKeyId.trim(),
        secretAccessKey: this._bedrockSecretAccessKey.trim(),
        sessionToken: this._bedrockSessionToken.trim() || undefined,
        region: this._bedrockRegion.trim(),
      };
    }
    // A provider with a known base URL can be listed even if the field was
    // cleared: the default stands in, matching the server-side default.
    const apiEndpoint =
      this._currentModel.api_endpoint?.trim() ||
      PROVIDER_DEFAULT_ENDPOINTS[provider] ||
      '';
    // These providers have no fixed catalog: the list comes from the
    // endpoint's own /models, so without an endpoint there is nothing to ask.
    if (ENDPOINT_LISTED_PROVIDERS.includes(provider) && !apiEndpoint) {
      this._modelsFetchError =
        'Enter the API endpoint first, then fetch models';
      return;
    }

    this._isFetchingModels = true;
    this._modelsFetchError = null;
    this._modelsSource = null;
    this._modelsFallbackReason = null;

    try {
      const result = await this._fetchModelSuggestionsForProvider(
        provider,
        // Bedrock credentials travel via the dedicated aws_* body fields;
        // sending the stored JSON blob as api_key would be redundant.
        this._isBedrock ? undefined : this._currentModel.api_key,
        apiEndpoint,
        awsAuth
      );
      this._modelSuggestions = result.models;
      this._modelsSource = result.source;
      this._modelsFallbackReason =
        result.source === 'fallback' ? result.error || null : null;
      if (this._modelSuggestions.length === 0) {
        this._modelsFetchError = `No ${this._selectedServiceKind.toUpperCase()} models available for this provider`;
      }
    } catch (error) {
      console.error('Failed to fetch models:', error);
      this._modelSuggestions = [];
      this._modelsSource = null;
      this._modelsFallbackReason = null;
      this._modelsFetchError =
        error instanceof Error ? error.message : 'Failed to fetch models';
    } finally {
      this._isFetchingModels = false;
      this.requestUpdate();
    }
  }

  private _handleModelNameChange(e: Event) {
    const selectedValue = (e.target as SlSelect).value as string;
    if (selectedValue === 'other') {
      this._isOtherModel = true;
      this._currentModel.model_identifier = '';
    } else {
      this._isOtherModel = false;
      this._currentModel.model_identifier = selectedValue;
    }
    // The primary model must never also appear in the extras list, or we would
    // try to create it twice.
    this._additionalModelIds = this._additionalModelIds.filter(
      (id) => id !== this._currentModel.model_identifier
    );
  }

  private _handleCustomModelInput(e: Event) {
    this._currentModel.model_identifier = (e.target as SlInput).value;
  }

  private _handleAdditionalModelsChange(e: Event) {
    const value = (e.target as SlSelect).value;
    this._additionalModelIds = Array.isArray(value) ? [...value] : [];
  }

  /**
   * Models offered as extras: everything discovered for this provider except
   * the one already chosen as primary.
   */
  private get _additionalModelChoices(): string[] {
    const primary = this._currentModel.model_identifier;
    return this._modelSuggestions.filter((id) => id !== primary);
  }

  /**
   * Create the extra selected models, each reusing the primary model's secret
   * so a single provider key backs all of them.
   *
   * A failure here must not look like a total failure: the primary model was
   * already created successfully, so we surface a partial-success message
   * rather than throwing.
   */
  private async _createAdditionalModels(primary: AIModel): Promise<AIModel[]> {
    const secretId = primary.credentials_secret_id;
    if (!this._additionalModelIds.length || !secretId) return [];

    const created: AIModel[] = [];
    const failed: string[] = [];

    for (const modelId of this._additionalModelIds) {
      try {
        created.push(
          await createAIModel({
            name: modelId,
            description: this._currentModel.description,
            provider_name: this._currentModel.provider_name,
            model_identifier: modelId,
            model_kind: this._currentModel.model_kind,
            api_endpoint: this._currentModel.api_endpoint,
            credentials_secret_id: secretId,
            is_default: false,
            meta_data: this._buildMetaDataForSubmit(modelId),
          })
        );
      } catch (error) {
        console.error(`Failed to create additional model ${modelId}:`, error);
        failed.push(modelId);
      }
    }

    if (failed.length) {
      this._formError = `Added ${primary.model_identifier}, but these could not be added: ${failed.join(', ')}. You can add them separately.`;
    }
    return created;
  }

  // ── submit ───────────────────────────────────────────

  private async _handleFormSubmit(e: Event) {
    e.preventDefault();
    this._formError = null;

    // Sync values from DOM in case event handlers missed a mutation
    this._syncFormFromDom();

    // Shared required-field guard. Only `api_endpoint` is provider-specific:
    // Bedrock has no HTTP endpoint (the region travels in meta_data instead).
    if (
      !this._currentModel.name ||
      !this._currentModel.provider_name ||
      !this._currentModel.model_identifier
    ) {
      this._formError = 'Please fill in all required fields';
      return;
    }
    if (this._isBedrock) {
      // Blank credential fields on an edited model mean "keep the stored key".
      const hasStoredBedrockKey = Boolean(
        this._isEditing && this.model?.has_api_key
      );
      if (!hasStoredBedrockKey && !this._bedrockCredsComplete) {
        this._formError =
          'Please fill in the AWS access key, secret key and region';
        return;
      }
    } else if (!this._currentModel.api_endpoint) {
      this._formError = 'Please fill in all required fields';
      return;
    }

    if (
      this._currentModel.model_kind === 'llm' &&
      this._preloopGatewayEnabled &&
      !this._canEnablePreloopGateway
    ) {
      this._formError =
        'Preloop gateway routing needs upstream API credentials. Enter an API key or turn off gateway routing.';
      return;
    }

    this._isSubmitting = true;

    try {
      // Build the payload from form-managed fields only. Spreading the whole
      // model would echo stored credential bookkeeping (credential_type,
      // credentials_secret_id, credentials_backend_type, ...) back into the
      // update schema, whose validator rejects inline + external credential
      // fields together, breaking every edit. A key is sent only when the
      // user typed a new one; blank means "keep the existing key".
      const typedApiKey = (this._currentModel.api_key || '').trim();
      const payload: Record<string, unknown> = {
        name: this._currentModel.name,
        description: this._currentModel.description,
        provider_name: this._currentModel.provider_name,
        model_identifier: this._currentModel.model_identifier,
        model_kind: this._currentModel.model_kind,
        // Bedrock has no HTTP endpoint; region travels in meta_data.
        api_endpoint: this._isBedrock ? null : this._currentModel.api_endpoint,
        is_default: this._currentModel.is_default,
        meta_data: this._buildMetaDataForSubmit(),
        ...(typedApiKey ? { api_key: typedApiKey } : {}),
      };
      if (this._isEditing) {
        const updated = await updateAIModel(this._currentModel.id!, payload);
        this.dispatchEvent(
          new CustomEvent('model-updated', {
            detail: { model: updated },
            bubbles: true,
            composed: true,
          })
        );
      } else {
        const created = await createAIModel(payload);
        const extraModels = await this._createAdditionalModels(created);
        this.dispatchEvent(
          new CustomEvent('model-created', {
            detail: { model: created, additionalModels: extraModels },
            bubbles: true,
            composed: true,
          })
        );
      }
      this._handleClose();
    } catch (error) {
      this._formError =
        error instanceof Error
          ? error.message
          : 'Failed to save model. Please try again.';
      console.error('Failed to save model:', error);
    } finally {
      this._isSubmitting = false;
    }
  }

  // ── render ───────────────────────────────────────────

  /**
   * Non-blocking provenance notice for the model list. Fallback listings say
   * so, with a short safe reason; live listings get a subtle count. Never
   * renders raw upstream text.
   *
   * A fallback WITHOUT a reason is not a failure: the server sends that when
   * no live attempt was possible, which for these providers means no API key
   * was entered yet. Saying "could not fetch" there would blame the provider
   * for the user not having typed a key.
   */
  private _renderModelsProvenanceNotice() {
    if (this._modelsSource === 'fallback') {
      if (!this._modelsFallbackReason) {
        return html`
          <div
            class="models-provenance-notice"
            style="color: var(--sl-color-neutral-600); font-size: 0.875rem; margin-top: 0.5rem;"
          >
            Showing known models. Enter an API key and fetch again for this
            provider's live list. You can also enter any model id via Other...
          </div>
        `;
      }
      const reason =
        FALLBACK_REASON_LABELS[this._modelsFallbackReason] ||
        'provider unavailable';
      return html`
        <div
          class="models-provenance-notice"
          style="color: var(--sl-color-warning-700); font-size: 0.875rem; margin-top: 0.5rem;"
        >
          Could not fetch the live model list (${reason}). Showing known models,
          which may be incomplete. You can still enter any model id via Other...
        </div>
      `;
    }
    if (this._modelsSource === 'live' && this._modelSuggestions.length > 0) {
      return html`
        <div
          class="models-provenance-notice"
          style="color: var(--sl-color-neutral-500); font-size: 0.8125rem; margin-top: 0.5rem;"
        >
          Fetched ${this._modelSuggestions.length} models
        </div>
      `;
    }
    return html``;
  }

  render() {
    if (!this.open) return html``;

    return html`
      <sl-dialog
        label="${this._isEditing ? 'Edit' : 'Add'} AI Model"
        .open=${this.open}
        @sl-request-close=${this._handleRequestClose}
      >
        ${when(
          this._formError,
          () => html`
            <sl-alert variant="danger" open>
              <sl-icon slot="icon" name="exclamation-octagon"></sl-icon>
              <strong>Error:</strong> ${this._formError}
            </sl-alert>
          `
        )}
        <div class="form-grid">
          <sl-input
            class="full-width"
            label="Friendly Name"
            data-field="name"
            .value=${this._currentModel.name || ''}
            @sl-input=${(e: Event) => {
              this._currentModel.name = (e.target as HTMLInputElement).value;
              this.requestUpdate();
            }}
            ?disabled=${this._isSubmitting}
          ></sl-input>
          <sl-select
            label="Service Kind"
            data-field="model_kind"
            .value=${this._currentModel.model_kind || 'llm'}
            @sl-change=${this._handleServiceKindChange}
            ?disabled=${this._isSubmitting}
          >
            <sl-option value="llm">Inference / chat</sl-option>
            <sl-option value="stt">Speech to text</sl-option>
            <sl-option value="tts">Text to speech</sl-option>
          </sl-select>

          <sl-select
            label="Provider"
            .value=${this._currentModel.provider_name || ''}
            @sl-change=${this._handleProviderChange}
            ?disabled=${this._isSubmitting}
          >
            ${this._availableProviders.map(
              (provider) => html`
                <sl-option value=${provider.value}>${provider.label}</sl-option>
              `
            )}
          </sl-select>

          ${
            this._isBedrock
              ? html`
                  <sl-input
                    label="AWS Access Key ID"
                    data-field="bedrock_access_key_id"
                    .value=${this._bedrockAccessKeyId}
                    @sl-input=${(e: Event) => {
                      this._bedrockAccessKeyId = (
                        e.target as HTMLInputElement
                      ).value;
                      this._syncBedrockApiKey();
                      this.requestUpdate();
                    }}
                    placeholder=${
                      this._isEditing ? 'Leave blank to keep existing key' : ''
                    }
                    ?disabled=${this._isSubmitting}
                  >
                    <div slot="help-text">
                      IAM credentials with Bedrock access. Find them in the AWS
                      console under IAM &gt; Access keys.
                    </div>
                  </sl-input>
                  <sl-input
                    type="password"
                    label="AWS Secret Access Key"
                    data-field="bedrock_secret_access_key"
                    .value=${this._bedrockSecretAccessKey}
                    @sl-input=${(e: Event) => {
                      this._bedrockSecretAccessKey = (
                        e.target as HTMLInputElement
                      ).value;
                      this._syncBedrockApiKey();
                      this.requestUpdate();
                    }}
                    placeholder=${
                      this._isEditing ? 'Leave blank to keep existing key' : ''
                    }
                    ?disabled=${this._isSubmitting}
                  ></sl-input>
                  <sl-input
                    type="password"
                    label="AWS Session Token"
                    data-field="bedrock_session_token"
                    .value=${this._bedrockSessionToken}
                    @sl-input=${(e: Event) => {
                      this._bedrockSessionToken = (
                        e.target as HTMLInputElement
                      ).value;
                      this._syncBedrockApiKey();
                      this.requestUpdate();
                    }}
                    placeholder="Optional, for temporary credentials"
                    ?disabled=${this._isSubmitting}
                  ></sl-input>
                  <sl-input
                    label="AWS Region"
                    data-field="bedrock_region"
                    .value=${this._bedrockRegion}
                    @sl-input=${(e: Event) => {
                      this._bedrockRegion = (
                        e.target as HTMLInputElement
                      ).value;
                      this.requestUpdate();
                    }}
                    placeholder=${BEDROCK_DEFAULT_REGION}
                    ?disabled=${this._isSubmitting}
                  >
                    <div slot="help-text">
                      Bedrock model availability is regional, e.g.
                      ${BEDROCK_DEFAULT_REGION} or eu-west-1.
                    </div>
                  </sl-input>
                `
              : html`
                  <sl-input
                    class="full-width"
                    label="API URL"
                    data-field="api_endpoint"
                    .value=${this._currentModel.api_endpoint || ''}
                    @sl-input=${(e: Event) => {
                      this._currentModel.api_endpoint = (
                        e.target as HTMLInputElement
                      ).value;
                      this.requestUpdate();
                    }}
                    ?disabled=${this._isSubmitting}
                  >
                    ${
                      this._currentModel.provider_name === 'qwen'
                        ? html`
                            <div slot="help-text">
                              Default is China (Beijing):
                              https://dashscope.aliyuncs.com/compatible-mode/v1.
                              International (Singapore):
                              https://dashscope-intl.aliyuncs.com/compatible-mode/v1.
                              US:
                              https://dashscope-us.aliyuncs.com/compatible-mode/v1.
                              Keys are not interchangeable across regions.
                            </div>
                          `
                        : ''
                    }
                  </sl-input>
                  <sl-input
                    class="full-width"
                    type="password"
                    label="API Key"
                    data-field="api_key"
                    .value=${this._currentModel.api_key || ''}
                    @sl-input=${(e: Event) => {
                      this._currentModel.api_key = (
                        e.target as HTMLInputElement
                      ).value;
                      this.requestUpdate();
                    }}
                    placeholder=${
                      this._isEditing ? 'Leave blank to keep existing key' : ''
                    }
                    ?disabled=${this._isSubmitting}
                  >
                    ${
                      !this._isEditing &&
                      this._getProviderKeyUrl(this._currentModel.provider_name)
                        ? html`
                            <div slot="help-text">
                              Enter your API key to fetch available models.
                              <a
                                href=${this._getProviderKeyUrl(
                                  this._currentModel.provider_name
                                )}
                                target="_blank"
                                rel="noopener noreferrer"
                                >Get your API key here.</a
                              >
                            </div>
                          `
                        : html`
                            <div slot="help-text">
                              ${
                                this._isEditing
                                  ? ''
                                  : 'Enter your API key to fetch available models'
                              }
                            </div>
                          `
                    }
                  </sl-input>
                `
          }

          <div class="full-width">
            <sl-checkbox
              .checked=${this._preloopGatewayEnabled}
              @sl-change=${(e: Event) => {
                const el = e.target as { checked: boolean };
                this._preloopGatewayEnabled = Boolean(el.checked);
                this.requestUpdate();
              }}
              ?disabled=${
                this._isSubmitting || this._currentModel.model_kind !== 'llm'
              }
            >
              Route inference through the Preloop gateway (OpenAI-compatible
              /openai/v1)
            </sl-checkbox>
            <div
              style="font-size: 0.875rem; color: var(--sl-color-neutral-600); margin-top: 0.35rem;"
            >
              ${
                this._currentModel.model_kind !== 'llm'
                  ? html`STT/TTS models are used directly for server audio
                    fallback.`
                  : this._currentModel.provider_name &&
                      this._currentModel.model_identifier
                    ? html`Gateway alias
                        <code
                          >${String(
                            this._currentModel.provider_name
                          ).toLowerCase()}/${
                            this._currentModel.model_identifier
                          }</code
                        >`
                    : html`Save provider and model id to show the gateway alias.`
              }
              ${
                this._currentModel.model_kind === 'llm' &&
                !this._canEnablePreloopGateway &&
                this._preloopGatewayEnabled
                  ? html`
                      <sl-alert
                        variant="warning"
                        open
                        style="margin-top: 0.5rem;"
                      >
                        Add an API key (or keep an existing one when editing) to
                        enable gateway routing.
                      </sl-alert>
                    `
                  : ''
              }
            </div>
          </div>

          <div class="full-width">
            <sl-button
              @click=${this._fetchModelsForCurrentProvider}
              ?loading=${this._isFetchingModels}
              ?disabled=${this._isSubmitting || this._isFetchingModels}
              style="width: 100%;"
            >
              ${
                this._modelSuggestions.length > 0
                  ? 'Refresh Models'
                  : 'Fetch Available Models'
              }
            </sl-button>
            ${
              this._modelsFetchError
                ? html`
                    <div
                      style="color: var(--sl-color-danger-600); font-size: 0.875rem; margin-top: 0.5rem;"
                    >
                      ${this._modelsFetchError}
                    </div>
                  `
                : ''
            }
            ${this._renderModelsProvenanceNotice()}
          </div>

          ${
            this._modelSuggestions.length > 0
              ? html`
                  <sl-select
                    class="full-width"
                    label="Model Name / ID"
                    .value=${
                      this._isOtherModel
                        ? 'other'
                        : this._currentModel.model_identifier || ''
                    }
                    @sl-change=${this._handleModelNameChange}
                    ?disabled=${this._isSubmitting}
                  >
                    ${repeat(
                      this._modelSuggestions,
                      (s) => s,
                      (s) => html`<sl-option value="${s}">${s}</sl-option>`
                    )}
                    <sl-option value="other">Other...</sl-option>
                  </sl-select>

                  ${when(
                    this._isOtherModel,
                    () => html`
                      <sl-input
                        class="full-width"
                        label="Custom Model Name / ID"
                        data-field="model_identifier"
                        placeholder="Enter custom model name"
                        .value=${this._currentModel.model_identifier || ''}
                        @sl-input=${this._handleCustomModelInput}
                        ?disabled=${this._isSubmitting}
                      ></sl-input>
                    `
                  )}
                  ${when(
                    !this._isEditing &&
                      !this._isOtherModel &&
                      this._additionalModelChoices.length > 0,
                    () => html`
                      <sl-select
                        class="full-width"
                        label="Also add (optional)"
                        multiple
                        clearable
                        .value=${this._additionalModelIds}
                        @sl-change=${this._handleAdditionalModelsChange}
                        ?disabled=${this._isSubmitting}
                        help-text="Add more models from this provider. They all reuse the same API key you entered above. Each is created as a separate model entry, managed on its own."
                      >
                        ${repeat(
                          this._additionalModelChoices,
                          (s) => s,
                          (s) => html`<sl-option value="${s}">${s}</sl-option>`
                        )}
                      </sl-select>
                    `
                  )}
                `
              : this._modelsFetchError
                ? html`
                    <sl-input
                      class="full-width"
                      label="Model Name / ID"
                      data-field="model_identifier"
                      placeholder="Enter model name manually"
                      .value=${this._currentModel.model_identifier || ''}
                      @sl-input=${this._handleCustomModelInput}
                      ?disabled=${this._isSubmitting}
                      help-text="Could not fetch models. You can enter the model name manually."
                    ></sl-input>
                  `
                : ''
          }
        </div>
        <sl-button
          slot="footer"
          @click=${this._handleClose}
          ?disabled=${this._isSubmitting}
          >Cancel</sl-button
        >
        <sl-button
          slot="footer"
          variant="primary"
          @click=${this._handleFormSubmit}
          ?loading=${this._isSubmitting}
          ?disabled=${this._isSubmitting}
          >Save</sl-button
        >
      </sl-dialog>
    `;
  }
}
