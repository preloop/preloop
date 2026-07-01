import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { when } from 'lit/directives/when.js';
import { repeat } from 'lit/directives/repeat.js';
import {
  getAvailableModelsForProvider,
  createAIModel,
  updateAIModel,
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
  { value: 'google', label: 'Google', serviceKinds: ['llm', 'stt'] },
  { value: 'qwen', label: 'Qwen', serviceKinds: ['llm'] },
  { value: 'deepseek', label: 'DeepSeek', serviceKinds: ['llm'] },
  {
    value: 'openai-compatible',
    label: 'OpenAI-compatible',
    serviceKinds: ['llm', 'stt', 'tts'],
  },
  { value: 'custom', label: 'Custom', serviceKinds: ['llm', 'stt', 'tts'] },
];

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
  /** When true, register this model for Preloop gateway routing (requires upstream API key). */
  @state() private _preloopGatewayEnabled = true;

  private get _isEditing(): boolean {
    return !!this.model;
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
    this._formError = null;
    this._isSubmitting = false;
    this._isFetchingModels = false;
    this._modelsFetchError = null;
  }

  /** Merge gateway routing metadata; gateway.enabled only when upstream credentials exist. */
  private _buildMetaDataForSubmit(): Record<string, unknown> {
    const existing =
      this._isEditing &&
      this.model?.meta_data &&
      typeof this.model.meta_data === 'object'
        ? { ...this.model.meta_data }
        : {};
    const provider = this._currentModel.provider_name;
    const modelId = this._currentModel.model_identifier;
    const modelKind = this._currentModel.model_kind || 'llm';
    const baseMeta = {
      ...existing,
      service_kind: modelKind,
    };
    if (!provider || !modelId) {
      return baseMeta;
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

  /** Read current input values directly from shadow DOM elements. */
  private _syncFormFromDom() {
    const inputs = this.shadowRoot?.querySelectorAll('sl-input') ?? [];
    for (const input of inputs) {
      const label = input.getAttribute('label');
      const val = (input as any).value as string;
      if (label === 'Friendly Name') this._currentModel.name = val || undefined;
      else if (label === 'API URL' && val)
        this._currentModel.api_endpoint = val;
      else if (label === 'API Key' && val) this._currentModel.api_key = val;
      else if (label === 'Custom Model Name / ID')
        this._currentModel.model_identifier = val || undefined;
    }
    const serviceKindSelect = this.shadowRoot?.querySelector(
      'sl-select[label="Service Kind"]'
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

    const defaultUrls: Record<string, string> = {
      openai: 'https://api.openai.com/v1',
      anthropic: 'https://api.anthropic.com/v1',
      google: 'https://generativelanguage.googleapis.com/v1beta',
      qwen: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      deepseek: 'https://api.deepseek.com/v1',
      'openai-compatible': '',
      custom: '',
    };

    this._currentModel = {
      ...this._currentModel,
      provider_name: provider,
      api_endpoint: defaultUrls[provider] || '',
      model_identifier: '',
    };

    this._modelSuggestions = [];
    this._isOtherModel = false;
    this._modelsFetchError = null;
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
    this._modelsFetchError = null;
    this.requestUpdate();
    if (providerSupported && modelKind !== 'llm') {
      void this._fetchModelsForCurrentProvider();
    }
  }

  private async _fetchModelSuggestionsForProvider(
    provider: string,
    apiKey?: string
  ): Promise<string[]> {
    return await getAvailableModelsForProvider(
      provider,
      apiKey,
      this._selectedServiceKind
    );
  }

  private async _fetchModelsForCurrentProvider() {
    if (!this._currentModel.provider_name) {
      this._modelsFetchError = 'Select a provider first';
      return;
    }

    this._isFetchingModels = true;
    this._modelsFetchError = null;

    try {
      this._modelSuggestions = await this._fetchModelSuggestionsForProvider(
        this._currentModel.provider_name,
        this._currentModel.api_key
      );
      if (this._modelSuggestions.length === 0) {
        this._modelsFetchError = `No ${this._selectedServiceKind.toUpperCase()} models available for this provider`;
      }
    } catch (error) {
      console.error('Failed to fetch models:', error);
      this._modelSuggestions = [];
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
  }

  private _handleCustomModelInput(e: Event) {
    this._currentModel.model_identifier = (e.target as SlInput).value;
  }

  // ── submit ───────────────────────────────────────────

  private async _handleFormSubmit(e: Event) {
    e.preventDefault();
    this._formError = null;

    // Sync values from DOM in case event handlers missed a mutation
    this._syncFormFromDom();

    if (
      !this._currentModel.name ||
      !this._currentModel.provider_name ||
      !this._currentModel.model_identifier ||
      !this._currentModel.api_endpoint
    ) {
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
      const payload = {
        ...this._currentModel,
        meta_data: this._buildMetaDataForSubmit(),
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
        this.dispatchEvent(
          new CustomEvent('model-created', {
            detail: { model: created },
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
            .value=${this._currentModel.name || ''}
            @sl-input=${(e: Event) => {
              this._currentModel.name = (e.target as HTMLInputElement).value;
              this.requestUpdate();
            }}
            ?disabled=${this._isSubmitting}
          ></sl-input>
          <sl-select
            label="Service Kind"
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

          <sl-input
            class="full-width"
            label="API URL"
            .value=${this._currentModel.api_endpoint || ''}
            @sl-input=${(e: Event) => {
              this._currentModel.api_endpoint = (
                e.target as HTMLInputElement
              ).value;
              this.requestUpdate();
            }}
            ?disabled=${this._isSubmitting}
          ></sl-input>
          <sl-input
            class="full-width"
            type="password"
            label="API Key"
            .value=${this._currentModel.api_key || ''}
            @sl-input=${(e: Event) => {
              this._currentModel.api_key = (e.target as HTMLInputElement).value;
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
                        placeholder="Enter custom model name"
                        .value=${this._currentModel.model_identifier || ''}
                        @sl-input=${this._handleCustomModelInput}
                        ?disabled=${this._isSubmitting}
                      ></sl-input>
                    `
                  )}
                `
              : this._modelsFetchError
                ? html`
                    <sl-input
                      class="full-width"
                      label="Model Name / ID"
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
