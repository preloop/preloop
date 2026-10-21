import { randomUuid } from '../utils/random-uuid';
import { LitElement, css, html, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/drawer/drawer.js';
import '@shoelace-style/shoelace/dist/components/textarea/textarea.js';
import { fetchWithAuth } from '../api';
import { LOCATION_CHANGED } from '../router';
import { mayPrompt, supportChoices } from '../support-engagement';

interface Config {
  operator_name: string;
  proactive: boolean;
  dwell_ms: number;
  idle_ms: number;
  disclosure: string;
  retention_days: number;
}
interface Message {
  id: string;
  sequence: number;
  role: string;
  text: string;
  citations: { title: string; url: string }[];
}
interface Credential {
  id: string;
  token: string;
  expires_at: string;
}
class SupportRequestError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
  }
}

const SESSION = 'preloop-support-conversation';
const DISMISSED = 'preloop-support-dismissed';
const PROMPTED = 'preloop-support-prompted';

@customElement('support-assistant')
export class SupportAssistant extends LitElement {
  @state() private config?: Config;
  @state() private open = false;
  @state() private prompt = false;
  @state() private text = '';
  @state() private contact = '';
  @state() private showContact = false;
  @state() private messages: Message[] = [];
  @state() private owner = 'Assistant';
  @state() private status = 'assistant';
  @state() private busy = false;
  @state() private error = '';
  @state() private deliveryProblem = false;
  @state() private operatorConnected = false;
  @state() private page = this.currentPage();
  private credential?: Credential;
  private pending?: {
    text: string;
    event_id: string;
    human_request: boolean;
    contact?: string;
  };
  private pollTimer?: ReturnType<typeof setTimeout>;
  private promptTimer?: ReturnType<typeof setInterval>;
  private lastActivity = performance.now();
  private lastTick = performance.now();
  private activeMs = 0;
  private converted = false;
  private generation = 0;
  private variant: 'quiet' | 'choices' = 'quiet';

  private currentPage(): string {
    const path = window.location.pathname;
    return path.startsWith('/console')
      ? 'help'
      : path.startsWith('/pricing')
        ? 'pricing'
        : path.startsWith('/register')
          ? 'signup'
          : 'home';
  }
  private stored(key: string): string | null {
    try {
      return sessionStorage.getItem(key);
    } catch {
      return null;
    }
  }
  private save(key: string, value: string): void {
    try {
      sessionStorage.setItem(key, value);
    } catch {
      /* Optional storage. */
    }
  }
  private activity = () => {
    this.lastActivity = performance.now();
  };
  private route = () => {
    this.page = this.currentPage();
    this.activeMs = 0;
    this.lastTick = this.lastActivity = performance.now();
    if (this.page === 'help' && this.credential) this.converted = true;
  };
  private resetIdentity = () => {
    this.generation++;
    this.credential = this.pending = undefined;
    this.messages = [];
    this.text = this.contact = this.error = '';
    this.showContact = false;
    this.open = this.busy = false;
    clearTimeout(this.pollTimer);
    try {
      sessionStorage.removeItem(SESSION);
    } catch {
      /* Optional storage. */
    }
  };
  private openHelp = () => this.show();
  private conversion = () => {
    if (this.converted) return;
    this.converted = true;
    this.prompt = false;
    void this.record('activation');
  };

  connectedCallback(): void {
    super.connectedCallback();
    window.addEventListener('preloop-open-support', this.openHelp);
    window.addEventListener('auth-change', this.resetIdentity);
    window.addEventListener('preloop-support-conversion', this.conversion);
    window.addEventListener(LOCATION_CHANGED, this.route);
    for (const event of ['pointerdown', 'keydown', 'input'])
      document.addEventListener(event, this.activity, { passive: true });
    const generation = this.generation;
    void this.request('/config')
      .then((config) => {
        if (!this.isConnected || generation !== this.generation) return;
        this.config = config;
        const variant = this.stored('preloop-support-variant');
        this.variant =
          config.proactive &&
          (variant === 'choices' ||
            (variant !== 'quiet' && Math.random() >= 0.5))
            ? 'choices'
            : 'quiet';
        this.save('preloop-support-variant', this.variant);
        try {
          const saved = JSON.parse(this.stored(SESSION) || 'null');
          if (saved && Date.parse(saved.expires_at) > Date.now())
            this.credential = saved;
        } catch {
          /* Invalid state starts a new conversation. */
        }
        this.promptTimer = setInterval(() => this.tick(), 1000);
      })
      .catch(() => {});
  }
  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.generation++;
    clearTimeout(this.pollTimer);
    clearInterval(this.promptTimer);
    window.removeEventListener('preloop-open-support', this.openHelp);
    window.removeEventListener('auth-change', this.resetIdentity);
    window.removeEventListener('preloop-support-conversion', this.conversion);
    window.removeEventListener(LOCATION_CHANGED, this.route);
    for (const event of ['pointerdown', 'keydown', 'input'])
      document.removeEventListener(event, this.activity);
  }
  private async request(path: string, body?: unknown) {
    const options = {
      method: body === undefined ? 'GET' : 'POST',
      cache: 'no-store' as RequestCache,
      headers: {
        'Content-Type': 'application/json',
        ...(this.credential
          ? { 'X-Support-Token': this.credential.token }
          : {}),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    };
    const response = await (localStorage.getItem('accessToken')
      ? fetchWithAuth(`/api/v1/support${path}`, { ...options, passive: true })
      : fetch(`/api/v1/support${path}`, options));
    if (!response.ok)
      throw new SupportRequestError(
        response.status === 404
          ? 'Support is unavailable or this conversation expired.'
          : response.status === 403
            ? 'Sign in to the account that started this conversation.'
            : response.status === 429
              ? 'Support request limit reached. Please try later.'
              : 'Support is unavailable. Your message can be retried.',
        response.status
      );
    return response.status === 204 ? undefined : response.json();
  }
  private async record(event: string) {
    try {
      await this.request('/events', {
        event,
        page: this.page,
        variant: this.variant,
      });
    } catch {
      /* Aggregate experiments never block chat. */
    }
  }
  private tick(): void {
    const now = performance.now();
    if (!document.hidden) this.activeMs += Math.min(1500, now - this.lastTick);
    this.lastTick = now;
    let focused = document.activeElement;
    while (focused?.shadowRoot?.activeElement)
      focused = focused.shadowRoot.activeElement;
    if (document.hidden) this.lastActivity = now;
    const focusedForm =
      !!focused &&
      (focused.matches('input,textarea,select,[contenteditable=true]') ||
        focused.closest('form') !== null ||
        focused.tagName.startsWith('SL-INPUT') ||
        focused.tagName.startsWith('SL-TEXTAREA'));
    if (
      this.config &&
      this.variant === 'choices' &&
      !this.open &&
      this.page !== 'help' &&
      mayPrompt(
        {
          activeMs: this.activeMs,
          idleMs: now - this.lastActivity,
          hidden: document.hidden,
          focusedForm,
          dismissed: !!this.stored(DISMISSED),
          converted: this.converted,
          prompted: !!this.stored(PROMPTED),
        },
        this.config.dwell_ms,
        this.config.idle_ms
      )
    ) {
      this.prompt = true;
      this.save(PROMPTED, '1');
      void this.record('impression');
    }
  }
  private show(): void {
    this.open = true;
    this.prompt = false;
    void this.history();
  }
  private dismiss(): void {
    this.prompt = false;
    this.save(DISMISSED, '1');
    void this.record('dismissal');
  }
  private forgetConversation(): void {
    this.generation++;
    this.credential = this.pending = undefined;
    this.messages = [];
    this.owner = 'Assistant';
    this.status = 'assistant';
    this.busy = false;
    clearTimeout(this.pollTimer);
    this.pollTimer = undefined;
    try {
      sessionStorage.removeItem(SESSION);
    } catch {
      /* Optional storage. */
    }
    this.error =
      'This conversation expired. Send a message to start a new one.';
  }
  private async history(): Promise<void> {
    clearTimeout(this.pollTimer);
    if (!this.credential || !this.open || !this.isConnected) return;
    const generation = this.generation;
    try {
      const result = await this.request(`/conversations/${this.credential.id}`);
      if (generation !== this.generation || !this.isConnected) return;
      this.messages = result.messages;
      this.owner = result.owner;
      this.status = result.status;
      this.deliveryProblem = result.delivery_problem;
      this.operatorConnected = result.operator_connected;
      this.error = '';
    } catch (error) {
      if (generation === this.generation) {
        if (
          error instanceof SupportRequestError &&
          [404, 410].includes(error.status)
        )
          this.forgetConversation();
        else
          this.error = error instanceof Error ? error.message : 'Reconnecting…';
      }
    }
    if (
      generation === this.generation &&
      this.credential &&
      this.open &&
      this.isConnected
    )
      this.pollTimer = setTimeout(() => void this.history(), 3000);
  }
  private async ensureConversation(): Promise<void> {
    if (!this.credential) {
      const generation = this.generation;
      const credential = await this.request('/conversations', {
        source_page: this.page,
      });
      if (generation !== this.generation) return;
      this.credential = credential;
      this.save(SESSION, JSON.stringify(credential));
    }
  }
  private async send(
    text = this.text,
    human = false,
    contact?: string
  ): Promise<void> {
    if (!text.trim() || this.busy) return;
    this.open = true;
    this.prompt = false;
    this.busy = true;
    const generation = this.generation;
    if (
      !this.pending ||
      this.pending.text !== text ||
      this.pending.human_request !== human ||
      this.pending.contact !== contact
    )
      this.pending = {
        text,
        event_id: randomUuid(),
        human_request: human,
        ...(contact ? { contact } : {}),
      };
    const pending = this.pending;
    try {
      await this.ensureConversation();
      if (generation !== this.generation) return;
      await this.request(
        `/conversations/${this.credential!.id}/messages`,
        pending
      );
      if (generation !== this.generation) return;
      this.pending = undefined;
      if (this.text === text) this.text = '';
      await this.record('engagement');
      await this.history();
    } catch (error) {
      if (generation === this.generation) {
        if (
          error instanceof SupportRequestError &&
          [404, 410].includes(error.status)
        )
          this.forgetConversation();
        else
          this.error =
            error instanceof Error
              ? error.message
              : 'Unable to send. Retry your message.';
      }
    } finally {
      if (generation === this.generation) this.busy = false;
    }
  }
  private async inspect(): Promise<void> {
    this.busy = true;
    const generation = this.generation;
    try {
      await this.ensureConversation();
      if (generation !== this.generation) return;
      await this.request(`/conversations/${this.credential!.id}/inspect`, {});
      if (generation === this.generation) await this.history();
    } catch (error) {
      if (generation === this.generation)
        this.error =
          error instanceof Error ? error.message : 'Inspection unavailable.';
    } finally {
      if (generation === this.generation) this.busy = false;
    }
  }
  render() {
    if (!this.config) return nothing;
    return html`<div class="launcher">
        ${
          this.prompt
            ? html`<aside aria-label="Optional help">
                <p>What would help?</p>
                ${supportChoices(this.page).map((choice) => html`<sl-button size="small" @click=${() => void this.send(choice, choice === 'Contact the team' || choice === 'Talk to a person')}>${choice}</sl-button>`)}<sl-button
                  size="small"
                  @click=${this.dismiss}
                  >Dismiss</sl-button
                >
              </aside>`
            : nothing
        }
        <sl-button variant="primary" @click=${this.show}>Ask Preloop</sl-button>
      </div>
      <sl-drawer
        label="Preloop support"
        .open=${this.open}
        @sl-request-close=${() => {
          this.open = false;
          clearTimeout(this.pollTimer);
          this.save(DISMISSED, '1');
        }}
      >
        <p>
          ${this.config.disclosure} Conversations are retained for
          ${this.config.retention_days} days.
        </p>
        <p role="status" aria-live="polite">
          ${this.status === 'closed' ? 'Conversation closed' : this.status === 'human' ? `${this.owner} · human assistance requested` : 'Assistant'}${this.status === 'human' && !this.operatorConnected ? ' · operator reconnecting; responses may be delayed' : ''}
        </p>
        ${this.deliveryProblem ? html`<p role="alert">Operator delivery needs attention. A human response may be delayed.</p>` : nothing}
        ${this.error ? html`<p role="alert">${this.error}</p>` : nothing}
        <div
          role="log"
          aria-live="polite"
          aria-relevant="additions"
          aria-label="Support messages"
        >
          ${this.messages.map(
            (message) =>
              html`<article>
                <strong
                  >${message.role === 'human' ? this.config!.operator_name : message.role}</strong
                >
                <p class="message">${message.text}</p>
                ${message.citations.map((citation) => html`<a href=${citation.url} target="_blank" rel="noopener noreferrer">${citation.title}</a>`)}
              </article>`
          )}
        </div>
        ${this.messages.length === 0 ? html`<div aria-label="Ways we can help">${supportChoices(this.page).map((choice) => html`<sl-button ?disabled=${this.busy} @click=${() => (choice === 'Inspect my setup' ? void this.inspect() : void this.send(choice, choice === 'Contact the team' || choice === 'Talk to a person'))}>${choice}</sl-button>`)}</div>` : nothing}
        <form
          @submit=${(event: Event) => {
            event.preventDefault();
            void this.send();
          }}
        >
          <sl-textarea
            label="Your question"
            maxlength="4000"
            resize="vertical"
            .value=${this.text}
            @sl-input=${(event: Event) => (this.text = (event.target as HTMLTextAreaElement).value)}
            @keydown=${(event: KeyboardEvent) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                void this.send();
              }
            }}
          ></sl-textarea>
          <sl-button
            type="submit"
            variant="primary"
            ?loading=${this.busy}
            ?disabled=${this.status === 'closed' || !this.text.trim()}
            >Send</sl-button
          >
          <sl-button
            ?disabled=${this.busy || this.status === 'closed'}
            @click=${() => {
              this.showContact = true;
              void this.send(
                `I'd like to talk to ${this.config!.operator_name}.`,
                true
              );
            }}
            >Talk to ${this.config.operator_name}</sl-button
          >
          ${this.showContact ? html`<label>Reply contact for optional offline follow-up<input type="text" maxlength="256" autocomplete="email" .value=${this.contact} @input=${(event: Event) => (this.contact = (event.target as HTMLInputElement).value)} /></label><sl-button ?disabled=${this.busy || !this.contact.trim()} @click=${() => void this.send('Offline follow-up requested.', true, this.contact)}>Add reply contact</sl-button>` : nothing}
          ${
            this.page === 'help'
              ? html`<sl-button
                    ?disabled=${this.busy || this.status === 'closed'}
                    @click=${this.inspect}
                    >Inspect my setup</sl-button
                  >
                  <p>
                    Inspection reads only setup status allowed by your
                    permissions.
                  </p>`
              : nothing
          }
          ${this.pending && this.error ? html`<sl-button @click=${() => void this.send(this.pending!.text, this.pending!.human_request, this.pending!.contact)}>Retry message</sl-button>` : nothing}
        </form>
      </sl-drawer>`;
  }
  static styles = css`
    :host {
      position: fixed;
      right: 1rem;
      bottom: 1rem;
      z-index: 1000;
    }
    .launcher {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 0.5rem;
    }
    aside {
      max-width: 22rem;
      background: var(--sl-panel-background-color);
      border: 1px solid var(--sl-color-neutral-300);
      padding: 1rem;
      border-radius: 8px;
      box-shadow: var(--sl-shadow-medium);
    }
    sl-button {
      margin: 0.25rem;
    }
    sl-drawer {
      --size: min(30rem, 100vw);
    }
    article {
      border-bottom: 1px solid var(--sl-color-neutral-200);
      padding: 0.5rem 0;
    }
    .message {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    a {
      display: block;
      color: var(--sl-color-primary-600);
    }
    [role='alert'] {
      color: var(--sl-color-danger-700);
    }
  `;
}
