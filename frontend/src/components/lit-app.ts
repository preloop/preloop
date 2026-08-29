import { LitElement, html, css } from 'lit';
import { customElement } from 'lit/decorators.js';
import { router } from '../router';
import { Router } from '@vaadin/router';
import { isSaaS } from '../brand-config';
import { getFeatures } from '../api';
import '../views/public/landing-view';
import './static-view-wrapper';
import '../views/public/login-view';
import '../views/public/register-view';
import '../views/public/forgot-password-view';
import '../views/public/reset-password-view';
import '../views/public/verify-email-view';
import '../views/public/request-demo-view';
import '../views/public/delete-account-view';
import '../views/public/whatis-mcp-view';
import '../views/public/pricing-view';
import '../views/public/welcome-view';
import '../views/public/static-view';
import '../views/authed/console-shell';
import '../views/authed/oauth-consent-view';
import '../views/authed/dashboard-control-plane-view';
import '../views/authed/trackers-view';
import '../views/authed/tracker-detail-view';
import '../views/authed/tools-view';
import '../views/authed/issues-view';
import '../views/authed/issues-compliance-view';
import '../views/authed/issues-dependencies-view';
import '../views/authed/issues/duplicates-view';
import '../views/authed/issues/assignments-view';
import '../views/authed/api-usage-view';
import '../views/authed/cost-view';
import '../views/authed/settings-view';
import '../views/authed/settings/api-keys-view';
import '../views/authed/settings/api-key-view';
import '../views/authed/settings/ai-models-view';
import '../views/authed/settings/ai-model-detail-view';
import '../views/authed/settings/profile-view';
import '../views/authed/settings/security-view';
import '../views/authed/settings/appearance-view';
import '../views/authed/settings/account-view';
import '../views/authed/settings/user-management-view';
import '../views/authed/settings/team-management-view';
import '../views/authed/settings/invitation-management-view';
import '../views/authed/notification-preferences-view';
import '../components/settings-tabs';
import '../views/authed/flows-view';
import '../views/authed/runners-view';
import '../views/authed/flow-view';
import '../views/authed/flow-executions-view';
import '../views/authed/flow-execution-view';
import '../views/authed/runtime-sessions-view';
import '../views/authed/approval-view';
import '../views/authed/approvals-view';
import '../views/authed/policies-view';
import '../views/authed/audit-view';
import '../views/authed/agents-view';
import '../views/authed/agent-detail-view';
import './app-header';
import './app-footer';
import './update-banner';
import { unifiedWebSocketManager } from '../services/unified-websocket-manager';

@customElement('lit-app')
export class LitApp extends LitElement {
  private hasNavigated = false;

  static styles = css`
    :host {
      display: flex;
      flex-direction: column;
      height: 100vh;
    }
    main {
      flex: 1;
      overflow-y: auto;
    }
  `;

  firstUpdated() {
    this.setupEventListeners();

    // Defer WebSocket connection until after initial render
    // This ensures the landing page loads quickly without waiting for WebSocket
    requestAnimationFrame(() => {
      this.connectWebSocket();
    });

    const outlet = this.renderRoot.querySelector('main');
    const ssrRoute = this.getAttribute('data-ssr-route');
    // Normalize path: remove .html suffix for comparison
    const currentPath = window.location.pathname.replace(/\.html$/, '');

    // Check if SSR content matches current route
    const ssrContent = this.querySelector('landing-view, static-view-wrapper');

    if (ssrContent && ssrRoute === currentPath) {
      // SSR content matches current route - move it to outlet for router to use
      outlet?.appendChild(ssrContent);
    } else if (ssrContent) {
      // SSR content doesn't match current route - remove it
      // This happens when index.html is served for non-root routes
      ssrContent.remove();
    }

    // Always initialize router
    router.setOutlet(outlet);

    // Note: Page view tracking is handled in main.ts to avoid duplication
    // and ensure all navigation methods are tracked

    router.setRoutes([
      {
        path: '/',
        action: (context, commands) => {
          // Check if landing-view already exists in the outlet (from SSR moved in firstUpdated)
          const routerOutlet = this.renderRoot.querySelector('main');
          const existingLandingView =
            routerOutlet?.querySelector('landing-view');

          if (existingLandingView) {
            // Reuse existing SSR landing-view - it will load its own content
            return existingLandingView;
          }

          // No existing landing-view, create a new one
          return commands.component('landing-view');
        },
      },
      { path: '/login', component: 'login-view' },
      {
        path: '/register',
        action: async (context, commands) => {
          // Check if registration is enabled
          try {
            const features = await getFeatures();
            if (features.features['registration'] === false) {
              // Registration disabled, redirect to login
              return commands.redirect('/login');
            }
          } catch (error) {
            // If we can't check, allow registration (fail open)
          }
          return commands.component('register-view');
        },
      },
      { path: '/forgot-password', component: 'forgot-password-view' },
      { path: '/reset-password', component: 'reset-password-view' },
      { path: '/verify-email', component: 'verify-email-view' },
      { path: '/request-demo', component: 'request-demo-view' },
      { path: '/delete-account', component: 'delete-account-view' },
      {
        path: '/about',
        action: (context, commands) => {
          // Check if we have SSR content for this EXACT route on first load
          const outlet = this.renderRoot.querySelector('main');
          const existingWrapper = outlet?.querySelector('static-view-wrapper');
          const ssrRoute = this.getAttribute('data-ssr-route');

          if (existingWrapper && ssrRoute === '/about' && !this.hasNavigated) {
            // Reuse SSR content on first load only
            this.hasNavigated = true;
            return existingWrapper;
          }

          // Load markdown dynamically
          const view = commands.component('static-view') as any;
          view.src = '/content/about.md';
          return view;
        },
      },
      {
        path: '/whatis-mcp',
        action: (context, commands) => {
          // Check if we have SSR content for this EXACT route on first load
          const outlet = this.renderRoot.querySelector('main');
          const existingWrapper = outlet?.querySelector('static-view-wrapper');
          const ssrRoute = this.getAttribute('data-ssr-route');

          if (
            existingWrapper &&
            ssrRoute === '/whatis-mcp' &&
            !this.hasNavigated
          ) {
            // Reuse SSR content on first load only
            this.hasNavigated = true;
            return existingWrapper;
          }

          // Load markdown dynamically
          const view = commands.component('static-view') as any;
          view.src = '/content/whatis-mcp.md';
          return view;
        },
      },
      {
        path: '/docs',
        action: (context, commands) => {
          const view = commands.component('static-view') as any;
          view.src = '/content/docs.md';
          return view;
        },
      },
      {
        path: '/terms',
        action: (context, commands) => {
          // Check if we have SSR content for this EXACT route on first load
          const outlet = this.renderRoot.querySelector('main');
          const existingWrapper = outlet?.querySelector('static-view-wrapper');
          const ssrRoute = this.getAttribute('data-ssr-route');

          if (existingWrapper && ssrRoute === '/terms' && !this.hasNavigated) {
            // Reuse SSR content on first load only
            this.hasNavigated = true;
            return existingWrapper;
          }

          // Load markdown dynamically
          const view = commands.component('static-view') as any;
          view.src = '/content/terms.md';
          return view;
        },
      },
      {
        path: '/privacy',
        action: (context, commands) => {
          // Check if we have SSR content for this EXACT route on first load
          const outlet = this.renderRoot.querySelector('main');
          const existingWrapper = outlet?.querySelector('static-view-wrapper');
          const ssrRoute = this.getAttribute('data-ssr-route');

          if (
            existingWrapper &&
            ssrRoute === '/privacy' &&
            !this.hasNavigated
          ) {
            // Reuse SSR content on first load only
            this.hasNavigated = true;
            return existingWrapper;
          }

          // Load markdown dynamically
          const view = commands.component('static-view') as any;
          view.src = '/content/privacy.md';
          return view;
        },
      },
      {
        path: '/pricing',
        action: (context, commands) => {
          // Check if we have SSR content for this EXACT route on first load
          const outlet = this.renderRoot.querySelector('main');
          const existingWrapper = outlet?.querySelector('static-view-wrapper');
          const ssrRoute = this.getAttribute('data-ssr-route');

          if (
            existingWrapper &&
            ssrRoute === '/pricing' &&
            !this.hasNavigated
          ) {
            // Reuse SSR content on first load only
            this.hasNavigated = true;
            return existingWrapper;
          }

          // Load pricing view dynamically
          return commands.component('public-pricing-view');
        },
      },
      {
        path: '/ai-act-readiness',
        action: (context, commands) => {
          // Check if we have SSR content for this EXACT route on first load
          const outlet = this.renderRoot.querySelector('main');
          const existingWrapper = outlet?.querySelector('static-view-wrapper');
          const ssrRoute = this.getAttribute('data-ssr-route');

          if (
            existingWrapper &&
            ssrRoute === '/ai-act-readiness' &&
            !this.hasNavigated
          ) {
            // Reuse SSR content on first load only
            this.hasNavigated = true;
            return existingWrapper;
          }

          // Load markdown dynamically
          const view = commands.component('static-view') as any;
          view.src = '/content/ai-act-readiness.md';
          return view;
        },
      },
      {
        // Competitor comparison landing pages at /vs/<slug>. One dynamic
        // route handles every slug. Each slug is pre-rendered at build time
        // (dist/vs/<slug>.html); this action reuses the SSR'd content on
        // first load and falls back to fetching /content/vs/<slug>.md for
        // client-side navigation.
        path: '/vs/:slug',
        action: (context, commands) => {
          const outlet = this.renderRoot.querySelector('main');
          const existingWrapper = outlet?.querySelector('static-view-wrapper');
          const ssrRoute = this.getAttribute('data-ssr-route');
          const slug = (context.params?.slug as string) || '';

          // Reject obviously unsafe slug input (route params are strings but
          // Vaadin Router does not constrain characters). Allow standard
          // slug characters only.
          if (!/^[a-z0-9][a-z0-9-]*$/i.test(slug)) {
            return commands.redirect('/');
          }

          if (
            existingWrapper &&
            ssrRoute === `/vs/${slug}` &&
            !this.hasNavigated
          ) {
            this.hasNavigated = true;
            return existingWrapper;
          }

          const view = commands.component('static-view') as any;
          view.src = `/content/vs/${slug}.md`;
          return view;
        },
      },
      {
        // Blog index. Like /vs/<slug>, the page is prerendered at build time
        // (dist/blog/index.html); this action reuses the SSR'd markup on first
        // load and otherwise fetches the rendered fragment. <static-view>
        // serves .html fragments verbatim, so no blog-specific component is
        // needed and no landing content ends up trapped in a shadow root.
        path: '/blog',
        action: (context, commands) => {
          // The blog is Preloop Cloud only. A self-hosted install serves its
          // own landing page from this same bundle and must not surface
          // preloop.ai's marketing blog.
          if (!isSaaS()) {
            return commands.redirect('/');
          }
          const outlet = this.renderRoot.querySelector('main');
          const existingWrapper = outlet?.querySelector('static-view-wrapper');
          const ssrRoute = this.getAttribute('data-ssr-route');

          if (existingWrapper && ssrRoute === '/blog' && !this.hasNavigated) {
            this.hasNavigated = true;
            return existingWrapper;
          }

          const view = commands.component('static-view') as any;
          view.src = '/content/blog/index.html';
          return view;
        },
      },
      {
        path: '/blog/:slug',
        action: (context, commands) => {
          if (!isSaaS()) {
            return commands.redirect('/');
          }
          const outlet = this.renderRoot.querySelector('main');
          const existingWrapper = outlet?.querySelector('static-view-wrapper');
          const ssrRoute = this.getAttribute('data-ssr-route');
          const slug = (context.params?.slug as string) || '';

          // Route params are unconstrained strings; accept slug characters
          // only, matching the filenames the build is willing to emit.
          if (!/^[a-z0-9][a-z0-9-]*$/.test(slug)) {
            return commands.redirect('/blog');
          }

          if (
            existingWrapper &&
            ssrRoute === `/blog/${slug}` &&
            !this.hasNavigated
          ) {
            this.hasNavigated = true;
            return existingWrapper;
          }

          const view = commands.component('static-view') as any;
          view.src = `/content/blog/${slug}.html`;
          return view;
        },
      },
      {
        path: '/resources/ai-agent-control-plane-2026',
        action: (context, commands) => {
          // Check if we have SSR content for this EXACT route on first load
          const outlet = this.renderRoot.querySelector('main');
          const existingWrapper = outlet?.querySelector('static-view-wrapper');
          const ssrRoute = this.getAttribute('data-ssr-route');

          if (
            existingWrapper &&
            ssrRoute === '/resources/ai-agent-control-plane-2026' &&
            !this.hasNavigated
          ) {
            // Reuse SSR content on first load only
            this.hasNavigated = true;
            return existingWrapper;
          }

          // Load markdown dynamically
          const view = commands.component('static-view') as any;
          view.src = '/content/resources/ai-agent-control-plane-2026.md';
          return view;
        },
      },
      { path: '/welcome', component: 'welcome-view' },
      {
        path: '/console',
        component: 'console-shell',
        action: () => {
          // Handle OAuth callback tokens from URL fragment only.
          // Fragment-based delivery prevents tokens from appearing in browser
          // history, server access logs, and Referrer headers.
          const params = new URLSearchParams(
            window.location.hash.startsWith('#')
              ? window.location.hash.substring(1)
              : ''
          );
          const accessToken = params.get('access_token');
          const refreshToken = params.get('refresh_token');

          if (accessToken) {
            localStorage.setItem('accessToken', accessToken);
            if (refreshToken) {
              localStorage.setItem('refreshToken', refreshToken);
            }

            // Store onboarding hints for the dashboard
            if (params.get('new_user')) {
              sessionStorage.setItem('oauth_new_user', '1');
            }
            if (params.get('setup_tracker')) {
              sessionStorage.setItem(
                'oauth_setup_tracker',
                params.get('setup_tracker')!
              );
            }

            // Clean tokens from URL fragment
            const cleanUrl = new URL(window.location.href);
            cleanUrl.hash = '';
            window.history.replaceState({}, '', cleanUrl.pathname);

            // Notify components of auth change
            window.dispatchEvent(
              new CustomEvent('auth-change', {
                bubbles: true,
                composed: true,
              })
            );

            // Signup is card-free (T2 paywall move): new OAuth users go
            // straight into the console; premium features request the card
            // in-product via the upgrade modal.
            if (params.get('setup_tracker') === 'github') {
              // No billing — go straight to GitHub App installation
              this._autoStartGitHubAppInstall(accessToken);
            } else {
              // Standard OAuth entry point w/o setup blockers
              const redirectPath = localStorage.getItem('loginRedirect');
              if (redirectPath) {
                localStorage.removeItem('loginRedirect');
                setTimeout(() => {
                  Router.go(redirectPath);
                }, 0);
              }
            }
          } else if (window.location.pathname === '/console') {
            // Handled when returning from e.g. Stripe without an access token hash
            const redirectPath = localStorage.getItem('loginRedirect');
            if (redirectPath) {
              localStorage.removeItem('loginRedirect');
              setTimeout(() => {
                Router.go(redirectPath);
              }, 0);
            }
          }

          // After returning from Stripe, check if GitHub tracker setup is still pending
          if (
            !accessToken &&
            !window.location.pathname.includes('/trackers') &&
            sessionStorage.getItem('oauth_setup_tracker') === 'github'
          ) {
            const token = localStorage.getItem('accessToken');
            if (token) {
              this._autoStartGitHubAppInstall(token);
            }
          }
        },
        children: [
          { path: '', component: 'dashboard-view' },
          {
            path: 'trackers',
            children: [
              { path: '', component: 'trackers-view' },
              { path: ':trackerId', component: 'tracker-detail-view' },
            ],
          },
          { path: 'tools', component: 'tools-view' },
          { path: 'policies', component: 'policies-view' },
          {
            path: 'issues',
            children: [
              { path: '', component: 'issues-view' },
              { path: 'compliance', component: 'issues-compliance-view' },
              { path: 'dependencies', component: 'issues-dependencies-view' },
              { path: 'duplicates', component: 'duplicates-view' },
              { path: 'assignments', component: 'assignments-view' },
            ],
          },
          {
            path: 'flows',
            children: [
              { path: '', component: 'flows-view' },
              { path: 'new', component: 'flow-view' },
              { path: 'executions', component: 'flow-executions-view' },
              {
                path: 'executions/:executionId',
                component: 'flow-execution-view',
              },
              { path: ':flowId', component: 'flow-view' },
            ],
          },
          {
            path: 'runners',
            action: (_context, commands) => {
              return commands.redirect('/console/settings/runners');
            },
          },
          { path: '/runtime-sessions', component: 'runtime-sessions-view' },
          { path: '/agents', component: 'agents-view' },
          { path: '/agents/:agentId', component: 'agent-detail-view' },
          {
            path: '/onboarding',
            action: (_context, commands) => {
              // Legacy onboarding page removed; agents onboard from the
              // Agents page (or the dashboard get-started wizard).
              return commands.redirect('/console/agents');
            },
          },
          { path: 'cost', component: 'cost-view' },
          { path: '/api-usage', component: 'api-usage-view' },
          { path: 'settings', redirect: '/console/settings/profile' },
          { path: 'settings/profile', component: 'profile-view' },
          { path: 'settings/security', component: 'security-view' },
          { path: 'settings/api-keys', component: 'api-keys-view' },
          { path: 'settings/runners', component: 'runners-view' },
          {
            path: 'settings/api-keys/:keyId',
            component: 'api-key-view',
          },
          { path: 'ai-models', component: 'ai-models-view' },
          {
            path: 'ai-models/:modelId',
            component: 'ai-model-detail-view',
          },
          { path: 'settings/appearance', component: 'appearance-view' },
          { path: 'settings/account', component: 'account-view' },
          { path: 'settings/users', component: 'user-management-view' },
          { path: 'settings/teams', component: 'team-management-view' },
          {
            path: 'settings/invitations',
            component: 'invitation-management-view',
          },
          {
            path: 'settings/notification-preferences',
            component: 'notification-preferences-view',
          },
          { path: 'pricing', component: 'pricing-view' },
          { path: 'approvals', component: 'approvals-view' },
          { path: 'approval/:requestId', component: 'approval-view' },
          { path: 'authorize', component: 'oauth-consent-view' },
          {
            path: 'governance',
            action: (_context, commands) => {
              return commands.redirect('/console/policies');
            },
          },
          { path: 'audit', component: 'audit-view' },
        ],
      },
    ]);
  }

  /**
   * Auto-redirect to GitHub App installation page for new OAuth users.
   * Called after OAuth sign-in (or after Stripe checkout returns).
   */
  private _autoStartGitHubAppInstall(token: string) {
    // Clear the flag to prevent redirect loops
    sessionStorage.removeItem('oauth_setup_tracker');
    sessionStorage.removeItem('oauth_new_user');

    fetch('/api/v1/auth/github/authorize', {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => {
        if (!res.ok) throw new Error('GitHub App not configured');
        return res.json();
      })
      .then((data) => {
        if (data.authorization_url) {
          sessionStorage.setItem('github_oauth_state', data.state);
          window.location.href = data.authorization_url;
        }
      })
      .catch((err) => {
        console.error('Failed to start GitHub App install:', err);
        // Fall back — user can add tracker manually from the trackers page
      });
  }

  render() {
    // The banner renders empty except for superusers on outdated
    // self-hosted instances (see update-banner.ts).
    return html`<update-banner></update-banner>
      <main></main> `;
  }

  connectWebSocket() {
    // Connect unified WebSocket manager when app initializes
    // This establishes a single persistent connection that all views can subscribe to
    unifiedWebSocketManager.connect().catch((error) => {
      console.error('Failed to connect unified WebSocket:', error);
    });
  }

  setupEventListeners() {
    // Event listeners can be added here as needed
  }
}
