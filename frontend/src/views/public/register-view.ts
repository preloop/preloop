import { LitElement, html, css, nothing } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { Router } from '../../router';
import { consumeLoginReturn } from '../../utils/login-return';
import { post, getFeatures } from '../../api';
import { formStyles } from '../../styles/form-styles';
import { getBrandConfig } from '../../brand-config';
import { trackGoal } from '../../services/web-analytics';
import { cloudPlans, loadPricingContent } from '../../utils/pricing-content';
import type { PricingPlan } from '../../components/pricing-plans';

import '@shoelace-style/shoelace/dist/components/input/input.js';
import '@shoelace-style/shoelace/dist/components/button/button.js';
import '@shoelace-style/shoelace/dist/components/icon/icon.js';
import '../../components/logo-component';

const OAUTH_PROVIDER_CONFIG: Record<string, { label: string; icon: string }> = {
  github: { label: 'GitHub', icon: 'github' },
  google: { label: 'Google', icon: 'google' },
  gitlab: { label: 'GitLab', icon: 'gitlab' },
};

/**
 * Map raw server/validator error text to human copy. Pydantic validation
 * prose (e.g. "value is not a valid email address: The part after the @-sign
 * is a special-use or reserved name…") must never render verbatim on the very
 * first form a stranger touches. Email and password get first-class strings;
 * anything unmapped is wrapped in a plain-language fallback.
 */
export function humanizeRegisterError(message: string): string {
  const raw = (message || '').trim();
  if (!raw) {
    return 'Failed to create an account';
  }
  if (/not a valid email address/i.test(raw)) {
    return "That email address doesn't look deliverable — check the part after the @.";
  }
  if (
    /password.*at least 8 characters|String should have at least 8/i.test(raw)
  ) {
    return 'Passwords need at least 8 characters.';
  }
  return `That didn't work: ${raw}`;
}

/** Parse a bootstrap token out of a location hash like `#bootstrap=abc123`. */
export function parseBootstrapFragment(hash: string): string {
  const match = /^#bootstrap=([^&]+)/.exec(hash || '');
  return match ? decodeURIComponent(match[1]) : '';
}

/**
 * The plan this visitor already picked, from `?plan=` on the pricing page.
 *
 * Kept to a plan id shape, and silently dropped otherwise, because the value
 * comes from a link somebody may have mangled and a broken query string must
 * never be able to stop a signup. The server applies the same rule again; it
 * is repeated here so the request does not carry rubbish in the first place.
 *
 * This is a shape check only. Whether the id names a plan a signup can
 * actually land on is {@link isFreePlanId}'s question.
 */
export function parsePlanChoice(search: string): string {
  const raw = new URLSearchParams(search || '').get('plan') || '';
  const candidate = raw.trim().toLowerCase();
  return /^[a-z0-9][a-z0-9_-]{0,63}$/.test(candidate) ? candidate : '';
}

/**
 * Does this id name a plan somebody can finish signing up on?
 *
 * Only a free plan arrives on this form. Every paid plan on the pricing page
 * goes to Stripe first and comes back as an account that already exists, so a
 * paid id here did not come from our own link: it was edited, forwarded or
 * guessed. Recording it as "chose a plan" would leave that account on no plan
 * at all with the first-login choice suppressed for good, which is exactly
 * the state this whole change removes. Such an id is dropped instead, and the
 * console asks on first login.
 *
 * A plan with no price on either interval is quoted, not free, and cannot be
 * self-served either. Shape, not plan id, for the same reason the pricing
 * page uses shape: a brand may name these tiers anything.
 */
export function isFreePlanId(plans: PricingPlan[], planId: string): boolean {
  const plan = plans.find((p) => p.id === planId);
  if (!plan) return false;
  if (plan.price_monthly === null && plan.price_annually === null) return false;
  return (plan.price_monthly ?? 0) <= 0 && (plan.price_annually ?? 0) <= 0;
}

@customElement('register-view')
export class RegisterView extends LitElement {
  @state()
  private error = '';

  @state()
  private _loading = false;

  @state()
  private oauthProviders: string[] = [];

  // True while this instance has zero users and registration is open — the
  // account being created becomes the admin account.
  @state()
  private firstAccountPending = false;

  // True while the instance is unclaimed (zero users + bootstrap token
  // configured server-side): signups need the setup link from the install.
  @state()
  private bootstrapPending = false;

  // Backend said the instance already has its admin account (403 on a
  // signup that carried a setup-link token).
  @state()
  private alreadyClaimed = false;

  // Token from the setup link fragment (#bootstrap=<token>). Held in memory
  // only; the fragment is stripped from the URL immediately on load.
  private bootstrapToken = '';

  // The plan chosen on the pricing page, if this visitor came from there.
  // Empty for anyone who opened the signup page directly, which is exactly
  // the group the console asks to choose a plan on first login.
  private planChoice = '';

  // The check that the plan on the link is one a signup can land on. Held so
  // that submitting before it lands waits for it rather than racing it: a
  // form filled in faster than a static file loads is unlikely, and "the
  // stamp depends on typing speed" is not a rule anybody could debug.
  private planChoiceChecked: Promise<void> | null = null;

  static styles = [
    formStyles,
    css`
      .oauth-section {
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
      }

      .oauth-button {
        width: 100%;
      }

      .oauth-button::part(base) {
        width: 100%;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 0.5rem;
      }

      .divider {
        display: flex;
        align-items: center;
        margin: 1.5rem 0;
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-small);
      }

      .divider::before,
      .divider::after {
        content: '';
        flex: 1;
        border-bottom: 1px solid var(--sl-color-neutral-300);
      }

      .divider::before {
        margin-right: 0.75rem;
      }

      .divider::after {
        margin-left: 0.75rem;
      }

      .first-account-note {
        color: var(--sl-color-neutral-500);
        font-size: var(--sl-font-size-small);
        line-height: 1.5;
        margin: 0 0 1rem;
      }

      /* Setup-link notices: 4px left border, console-compact type
         (14px primary / 13px secondary). */
      .bootstrap-notice {
        border-radius: 4px;
        border-left: 4px solid #f2a93b;
        background: rgba(242, 169, 59, 0.12);
        padding: 12px 16px;
        margin: 0 0 1rem;
        font-size: 14px;
        line-height: 1.4;
        text-align: left;
      }

      .bootstrap-notice.claimed {
        border-left-color: #ff5d5d;
        background: rgba(255, 93, 93, 0.12);
      }

      .bootstrap-notice .notice-title {
        font-weight: 600;
      }

      .bootstrap-notice .notice-detail {
        font-size: 13px;
        opacity: 0.85;
        margin-top: 2px;
      }
    `,
  ];

  connectedCallback() {
    super.connectedCallback();
    // Setup link: read the bootstrap token out of the fragment, keep it in
    // memory only, and strip it from the URL immediately (history, referrer
    // and share safety).
    this.planChoice = parsePlanChoice(window.location.search);
    if (this.planChoice) {
      this.planChoiceChecked = this._confirmPlanChoiceIsFree();
    }
    this.bootstrapToken = parseBootstrapFragment(window.location.hash);
    if (this.bootstrapToken) {
      history.replaceState(
        null,
        '',
        window.location.pathname + window.location.search
      );
    }
    this._checkFeatures();
  }

  /**
   * Check the `?plan=` id against the published price list, and drop it
   * unless it names a free plan.
   *
   * Only runs when a plan arrived on the link, so a plain signup makes no
   * extra request. The list is the same public, cacheable file the pricing
   * page already reads.
   *
   * A file that will not load drops the plan too. The cost of dropping is
   * one extra screen on first login; the cost of keeping is an account
   * recorded as having chosen a plan nobody can name. The safe direction is
   * to ask.
   */
  private async _confirmPlanChoiceIsFree(): Promise<void> {
    try {
      const plans = cloudPlans(await loadPricingContent());
      if (!isFreePlanId(plans, this.planChoice)) {
        this.planChoice = '';
      }
    } catch {
      this.planChoice = '';
    }
  }

  private async _checkFeatures() {
    try {
      const features = await getFeatures();
      const providers = features.features['oauth_providers'];
      this.oauthProviders = Array.isArray(providers) ? providers : [];
      this.firstAccountPending =
        features.features['first_account_pending'] === true;
      this.bootstrapPending =
        features.features['registration_bootstrap_pending'] === true;
    } catch (error) {
      this.oauthProviders = [];
      this.firstAccountPending = false;
      this.bootstrapPending = false;
    }
  }

  private async handleRegister(event: SubmitEvent) {
    event.preventDefault();
    this._loading = true;
    this.alreadyClaimed = false;
    const form = event.target as HTMLFormElement;
    const formData = new FormData(form);
    const username = formData.get('username') as string;
    const email = formData.get('email') as string;
    const password = formData.get('password') as string;

    try {
      await this.planChoiceChecked;
      const payload: Record<string, unknown> = { username, email, password };
      if (this.bootstrapToken) {
        payload.bootstrap_token = this.bootstrapToken;
      }
      if (this.planChoice) {
        // Somebody who arrived from the pricing page already answered the
        // plan question. Sending it here records the answer on the new user,
        // which is what keeps the first-login plan choice off their screen
        // even after they follow a verification link in another browser.
        payload.plan_choice = this.planChoice;
      }
      const registerResult = await post('/api/v1/auth/register', payload);

      // If the backend returns an error in the payload instead of throwing an HTTP error
      if (registerResult && registerResult.error) {
        throw new Error(registerResult.error);
      }

      // Completed registration is the primary conversion goal.
      trackGoal('Signup');

      // No Stripe checkout here. A paid plan chosen on the pricing page goes
      // to Stripe first and never reaches this form; a signup that arrives
      // with no plan is asked to choose one on its first console visit.

      // Try to auto-log-in the user using the credentials they just submitted
      // and continue any pending flow (eg. CLI OAuth consent at
      // /console/authorize) instead of bouncing them back to the sign-in
      // page. This is the seamless path for new users running
      // `curl ... | sh` -> `preloop signup` -> sign up -> back to CLI.
      try {
        const authData = await post('/api/v1/auth/token/json', {
          username,
          password,
        });
        if (authData && authData.access_token) {
          localStorage.setItem('accessToken', authData.access_token);
          if (authData.refresh_token) {
            localStorage.setItem('refreshToken', authData.refresh_token);
          }
          window.dispatchEvent(
            new CustomEvent('auth-change', { bubbles: true, composed: true })
          );
          this._loading = false;
          const redirectPath = consumeLoginReturn();
          if (redirectPath) {
            Router.go(redirectPath);
          } else {
            Router.go('/console');
          }
          return;
        }
      } catch (autoLoginError) {
        // Silently fall back to the sign-in page below.
        console.warn(
          'Auto-login after registration failed, falling back to sign-in page',
          autoLoginError
        );
      }

      // Fallback: send the user to the sign-in page (loginRedirect, if set,
      // is preserved across this navigation).
      this._loading = false;
      Router.go('/login?registered=true');
    } catch (error) {
      this._loading = false;
      if (error instanceof Error) {
        const message = error.message || '';
        if (/setup link required/i.test(message)) {
          // Unclaimed instance and our token was missing or stale: show the
          // amber setup-link notice instead of a generic error. Drop the
          // stale token so the notice renders.
          this.bootstrapToken = '';
          this.bootstrapPending = true;
          this.error = '';
        } else if (
          this.bootstrapToken &&
          /registration is disabled/i.test(message)
        ) {
          // A setup-link signup on an instance that already has its admin:
          // the link is being reused after the instance was claimed.
          this.alreadyClaimed = true;
          this.error = '';
        } else {
          this.error = humanizeRegisterError(message);
        }
      } else {
        this.error = 'Failed to create an account';
      }
      console.error('Create account failed', error);
      // Ensure we don't proceed with checkout if registration failed
      return;
    }
  }

  private _renderBootstrapNotice() {
    if (this.alreadyClaimed) {
      return html`
        <div
          id="bootstrap-notice"
          class="bootstrap-notice claimed"
          role="alert"
          aria-live="assertive"
        >
          <div class="notice-title">
            This instance has already been claimed.
          </div>
          <div class="notice-detail">
            An admin account exists — ask them to invite you, or
            <a href="/login">sign in</a>.
          </div>
        </div>
      `;
    }
    if (this.bootstrapPending && !this.bootstrapToken) {
      return html`
        <div
          id="bootstrap-notice"
          class="bootstrap-notice"
          role="alert"
          aria-live="assertive"
        >
          <div class="notice-title">Setup link required</div>
          <div class="notice-detail">
            Use the link printed at the end of the install, or run
            create_first_user.py on the server.
          </div>
        </div>
      `;
    }
    return nothing;
  }

  private _renderOAuthButtons() {
    if (this.oauthProviders.length === 0) return nothing;

    return html`
      <div class="oauth-section">
        ${this.oauthProviders.map((provider) => {
          const config = OAUTH_PROVIDER_CONFIG[provider];
          if (!config) return nothing;
          return html`
            <sl-button
              class="oauth-button"
              variant="default"
              size="large"
              @click=${() => {
                window.location.href = `/api/v1/auth/oauth/${provider}/authorize`;
              }}
            >
              <sl-icon name="${config.icon}" slot="prefix"></sl-icon>
              Sign up with ${config.label}
            </sl-button>
          `;
        })}
      </div>
      <div class="divider">or sign up with email</div>
    `;
  }

  render() {
    return html`
      <div class="container">
        <div class="logo">
          <a href="/">
            <logo-component></logo-component>
          </a>
        </div>
        <div class="form-container">
          <h2>Create a ${getBrandConfig().name} account</h2>
          ${
            this.firstAccountPending
              ? html`<p class="first-account-note">
                  You're creating the first account on this instance — it
                  becomes the admin account.
                </p>`
              : ''
          }
          ${this._renderBootstrapNotice()}
          ${
            this.error
              ? html`<div class="error-message" role="alert">
                  ${this.error}
                </div>`
              : ''
          }
          ${this._renderOAuthButtons()}
          <form
            @submit=${this.handleRegister}
            aria-describedby=${
              this.alreadyClaimed ||
              (this.bootstrapPending && !this.bootstrapToken)
                ? 'bootstrap-notice'
                : nothing
            }
          >
            <div class="form-group">
              <sl-input
                label="Username"
                id="username"
                name="username"
                autocomplete="username"
                required
              ></sl-input>
            </div>
            <div class="form-group">
              <sl-input
                type="email"
                label="Email"
                id="email"
                name="email"
                autocomplete="email"
                required
              ></sl-input>
            </div>
            <div class="form-group">
              <sl-input
                type="password"
                label="Password"
                id="password"
                name="password"
                autocomplete="new-password"
                minlength="8"
                required
                password-toggle
                help-text="At least 8 characters."
              ></sl-input>
            </div>
            <div class="form-actions">
              <sl-button
                type="submit"
                variant="primary"
                ?loading=${this._loading}
                >Create account</sl-button
              >
            </div>
            <div class="form-links">
              <a href="/login">Already have an account? Sign in</a>
            </div>
          </form>
        </div>
      </div>
    `;
  }
}
