import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './agent-manual-setup.ts';
import type { AgentManualSetup } from './agent-manual-setup';
import { substituteAgentKey, toToml } from './agent-manual-setup';
import { formatAbsoluteTime } from '../utils/date';

const SYNTHETIC_KEY = 'agt_SYNTHETIC_0000000000_abcdefghijklmnopqrst';
const SYNTHETIC_LAST_20 = SYNTHETIC_KEY.slice(-20);
const DOCS_URL = 'https://docs.preloop.ai/guide/clients/sandboxed-agents/';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const claudeInstructions = {
  files: [
    {
      path: '/home/agent/.claude/settings.json',
      format: 'json',
      exists: true,
      merge: {
        env: {
          ANTHROPIC_BASE_URL: 'https://preloop.example.com/anthropic',
          ANTHROPIC_API_KEY: '<AGENT_KEY>',
        },
      },
      remove: ['env.ANTHROPIC_AUTH_TOKEN'],
      host_source: '/srv/agent-config/claude/settings.json',
    },
    {
      path: '/home/agent/.claude.json',
      format: 'json',
      exists: false,
      merge: {
        customApiKeyResponses: { approved: ['<AGENT_KEY_LAST_20>'] },
        mcpServers: {
          preloop: {
            type: 'http',
            url: 'https://preloop.example.com/mcp/v1',
            headers: { Authorization: 'Bearer <AGENT_KEY>' },
          },
        },
      },
      remove: [],
      host_source: null,
    },
  ],
  env: {
    ANTHROPIC_BASE_URL: 'https://preloop.example.com/anthropic',
    ANTHROPIC_API_KEY: '<AGENT_KEY>',
  },
  settings_flag: 'claude --settings <file>',
  limitations: [
    'The environment-variable form routes model traffic only.',
    'The --settings form applies only to sessions started with the flag.',
  ],
};

const codexInstructions = {
  files: [
    {
      path: '~/.codex/config.toml',
      format: 'toml',
      exists: false,
      merge: {
        mcp_servers: {
          preloop: {
            url: 'https://preloop.example.com/mcp/v1',
            http_headers: { Authorization: 'Bearer <AGENT_KEY>' },
          },
        },
      },
      remove: [],
      host_source: null,
    },
  ],
  env: {},
  settings_flag: null,
  limitations: ['This change adds the managed Preloop MCP server only.'],
};

function setupResponse(overrides: Record<string, unknown> = {}) {
  return {
    agent_id: 'agent-1',
    agent_kind: 'claude_code',
    display_name: 'Build agent',
    pending: null,
    instructions: claudeInstructions,
    resume_command: 'preloop agents onboard --resume "Build agent"',
    ...overrides,
  };
}

const pending = {
  enrollment_id: 'enrollment-1',
  status: 'pending_local_config',
  created_at: '2026-03-10T10:00:00Z',
  expires_at: '2026-03-17T10:00:00Z',
};

describe('toToml', () => {
  it('writes nested tables, strings, booleans, numbers and arrays', () => {
    const toml = toToml({
      model: 'example-model',
      retries: 3,
      enabled: true,
      args: ['--flag', 'two words'],
      mcp_servers: {
        preloop: {
          url: 'https://preloop.example.com/mcp/v1',
          http_headers: { Authorization: 'Bearer <AGENT_KEY>' },
        },
      },
    });
    expect(toml).to.equal(
      [
        'model = "example-model"',
        'retries = 3',
        'enabled = true',
        'args = ["--flag", "two words"]',
        '',
        '[mcp_servers.preloop]',
        'url = "https://preloop.example.com/mcp/v1"',
        '',
        '[mcp_servers.preloop.http_headers]',
        'Authorization = "Bearer <AGENT_KEY>"',
        '',
      ].join('\n')
    );
  });

  it('quotes keys that are not bare and escapes string values', () => {
    const toml = toToml({
      servers: { 'my.server': { 'x y': 'a "quoted" \\ value\nnext' } },
    });
    expect(toml).to.equal(
      [
        '[servers."my.server"]',
        '"x y" = "a \\"quoted\\" \\\\ value\\nnext"',
        '',
      ].join('\n')
    );
  });

  it('writes arrays of tables and skips null values', () => {
    const toml = toToml({
      gone: null,
      profiles: [{ name: 'one' }, { name: 'two', nested: { on: false } }],
    });
    expect(toml).to.equal(
      [
        '[[profiles]]',
        'name = "one"',
        '',
        '[[profiles]]',
        'name = "two"',
        '',
        '[profiles.nested]',
        'on = false',
        '',
      ].join('\n')
    );
  });
});

describe('substituteAgentKey', () => {
  it('replaces both placeholders in nested values and keys', () => {
    const result = substituteAgentKey(
      {
        a: 'Bearer <AGENT_KEY>',
        b: ['<AGENT_KEY_LAST_20>', 7, true],
        '<AGENT_KEY_LAST_20>': { c: '<AGENT_KEY>' },
      },
      SYNTHETIC_KEY
    );
    expect(result).to.deep.equal({
      a: `Bearer ${SYNTHETIC_KEY}`,
      b: [SYNTHETIC_LAST_20, 7, true],
      [SYNTHETIC_LAST_20]: { c: SYNTHETIC_KEY },
    });
  });

  it('returns the value untouched without a key', () => {
    const value = { a: '<AGENT_KEY>' };
    expect(substituteAgentKey(value, null)).to.equal(value);
  });
});

describe('AgentManualSetup', () => {
  let fetchStub: sinon.SinonStub;
  let setup: Record<string, unknown>;
  let cancelResponse: () => Response;
  let credentialResponse: () => Response;

  const text = (el: AgentManualSetup): string =>
    el.shadowRoot?.textContent?.replace(/\s+/g, ' ') ?? '';

  const copyValues = (el: AgentManualSetup): string[] =>
    Array.from(el.shadowRoot!.querySelectorAll('sl-copy-button')).map(
      (button) => (button as HTMLElement & { value: string }).value
    );

  const calls = (method: string, fragment: string): sinon.SinonSpyCall[] =>
    fetchStub
      .getCalls()
      .filter(
        (call) =>
          String(call.args[0]).includes(fragment) &&
          (call.args[1]?.method ?? 'GET') === method
      );

  async function mount(): Promise<AgentManualSetup> {
    const el = await fixture<AgentManualSetup>(
      html`<agent-manual-setup
        agent-id="agent-1"
        display-name="Build agent"
      ></agent-manual-setup>`
    );
    await waitUntil(
      () => !el.shadowRoot!.querySelector('[data-manual-setup-loading]'),
      'manual setup loaded'
    );
    await el.updateComplete;
    return el;
  }

  async function click(el: AgentManualSetup, selector: string): Promise<void> {
    const target = el.shadowRoot!.querySelector<HTMLElement>(selector);
    expect(target, selector).to.exist;
    target!.click();
    await el.updateComplete;
  }

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    setup = setupResponse();
    cancelResponse = () =>
      jsonResponse({
        enrollment: { id: 'enrollment-1', status: 'cancelled' },
        removed: { credentials: 1, ai_models: 2, agent: 'decommissioned' },
      });
    credentialResponse = () =>
      jsonResponse(
        {
          credential: { id: 'credential-1', name: 'manual-setup', scopes: [] },
          token: SYNTHETIC_KEY,
        },
        201
      );
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = init?.method ?? 'GET';
        if (url.endsWith('/api/v1/agents/agent-1/manual-setup')) {
          return jsonResponse(setup);
        }
        if (
          url.endsWith('/api/v1/agents/agent-1/credentials') &&
          method === 'POST'
        ) {
          return credentialResponse();
        }
        if (
          url.endsWith(
            '/api/v1/agents/agent-1/enrollments/enrollment-1/cancel'
          ) &&
          method === 'POST'
        ) {
          return cancelResponse();
        }
        return jsonResponse({ detail: `Unhandled request: ${url}` }, 500);
      }
    );
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    sessionStorage.clear();
  });

  it('renders the pending state with expiry, resume command and instructions', async () => {
    setup = setupResponse({ pending });
    const el = await mount();

    const notice = el.shadowRoot!.querySelector('[data-manual-setup-pending]');
    expect(notice).to.exist;
    const title = notice!.querySelector('[data-manual-setup-pending-title]');
    expect(title!.textContent!.trim()).to.equal(
      'Waiting for local configuration'
    );
    const body = text(el);
    expect(body).to.include('is not onboarded until');
    expect(body).to.include(formatAbsoluteTime(pending.expires_at));
    expect(body).to.include(
      'Preloop removes the key and what onboarding created'
    );
    expect(body).to.include('preloop agents onboard --resume "Build agent"');
    expect(copyValues(el)).to.include(
      'preloop agents onboard --resume "Build agent"'
    );
    expect(el.shadowRoot!.querySelector('[data-manual-setup-cancel]')).to.exist;

    // Instructions: path, host source, merge or create, remove, env, flag.
    expect(body).to.include('/home/agent/.claude/settings.json');
    expect(body).to.include(
      'On the host this is probably /srv/agent-config/claude/settings.json'
    );
    expect(body).to.include('Merge these keys and keep everything else');
    expect(body).to.include('Create the file with exactly this content');
    expect(body).to.include('env.ANTHROPIC_AUTH_TOKEN');
    expect(body).to.include(
      "export ANTHROPIC_BASE_URL='https://preloop.example.com/anthropic'"
    );
    expect(body).to.include('claude --settings <file>');
    expect(body).to.include('What each form does not enable');
    expect(body).to.include(
      'The --settings form applies only to sessions started with the flag.'
    );
    // The CLI's key is the expected one; issuing here is optional.
    expect(body).to.include('The key the CLI printed is the one');
    expect(body).to.include('A key issued here also works');

    const docs = el.shadowRoot!.querySelector<HTMLAnchorElement>(
      '[data-manual-setup-docs]'
    );
    expect(docs!.getAttribute('href')).to.equal(DOCS_URL);
    expect(docs!.textContent!.trim()).to.equal(
      'Sandboxed and containerised agents guide'
    );
  });

  it('shows the placeholder until a key is issued', async () => {
    const el = await mount();

    expect(el.shadowRoot!.querySelector('[data-manual-setup-pending]')).to.not
      .exist;
    const body = text(el);
    expect(body).to.include('"ANTHROPIC_API_KEY": "<AGENT_KEY>"');
    expect(body).to.include("export ANTHROPIC_API_KEY='<AGENT_KEY>'");
    expect(body).to.include('<AGENT_KEY_LAST_20>');
    expect(body).to.not.include('agt_');
    expect(el.shadowRoot!.querySelector('[data-manual-setup-key-notice]')).to
      .not.exist;
    expect(calls('POST', '/credentials')).to.have.length(0);
    const issue = el.shadowRoot!.querySelector('[data-manual-setup-issue]');
    expect(issue!.textContent!.trim()).to.equal('Issue a key and fill it in');
  });

  it('issues a key, fills it in everywhere and says it is shown once', async () => {
    const el = await mount();
    const issued = sinon.spy();
    el.addEventListener('manual-setup-key-issued', issued);
    const setItem = sinon.spy(Storage.prototype, 'setItem');

    try {
      await click(el, '[data-manual-setup-issue]');
      await waitUntil(
        () => el.shadowRoot!.querySelector('[data-manual-setup-key-notice]'),
        'shown-once notice'
      );
      await el.updateComplete;

      const [call] = calls('POST', '/credentials');
      const payload = JSON.parse(String(call.args[1].body));
      expect(payload.name).to.match(/^manual-setup-\d{14}$/);
      expect(payload.description).to.equal(
        'Issued from the console for manual setup'
      );

      const body = text(el);
      expect(body).to.include(`"ANTHROPIC_API_KEY": "${SYNTHETIC_KEY}"`);
      expect(body).to.include(`"Authorization": "Bearer ${SYNTHETIC_KEY}"`);
      expect(body).to.include(`export ANTHROPIC_API_KEY='${SYNTHETIC_KEY}'`);
      expect(body).to.include(`"${SYNTHETIC_LAST_20}"`);
      expect(body).to.not.include('<AGENT_KEY>');
      expect(body).to.not.include('<AGENT_KEY_LAST_20>');
      const notice = el
        .shadowRoot!.querySelector('[data-manual-setup-key-notice]')!
        .textContent!.replace(/\s+/g, ' ');
      expect(notice).to.include('shown once');
      expect(notice).to.include('cannot be shown again');

      const copies = copyValues(el).join('\n');
      expect(copies).to.include(SYNTHETIC_KEY);
      expect(copies).to.not.include('<AGENT_KEY>');

      // The key stays in component state only.
      expect(
        setItem
          .getCalls()
          .flatMap((c) => c.args)
          .join(' ')
      ).to.not.include(SYNTHETIC_KEY);
      for (const store of [localStorage, sessionStorage]) {
        for (let i = 0; i < store.length; i += 1) {
          expect(store.getItem(store.key(i)!)).to.not.include(SYNTHETIC_KEY);
        }
      }
      expect(window.location.href).to.not.include(SYNTHETIC_KEY);
      expect(issued.calledOnce).to.be.true;
      expect(JSON.stringify(issued.firstCall.args[0].detail)).to.not.include(
        SYNTHETIC_KEY
      );
      // A second key is not issued while one is on screen.
      expect(el.shadowRoot!.querySelector('[data-manual-setup-issue]')).to.not
        .exist;
    } finally {
      setItem.restore();
    }
  });

  it('forgets the key when it leaves the page', async () => {
    const el = await mount();
    await click(el, '[data-manual-setup-issue]');
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-manual-setup-key-notice]')
    );
    const parent = el.parentElement!;
    el.remove();
    parent.appendChild(el);
    await waitUntil(
      () => !el.shadowRoot!.querySelector('[data-manual-setup-loading]')
    );
    await el.updateComplete;
    expect(text(el)).to.not.include(SYNTHETIC_KEY);
    expect(text(el)).to.include('<AGENT_KEY>');
  });

  it('reports a failed key issue without filling anything in', async () => {
    credentialResponse = () =>
      jsonResponse({ detail: 'Agent credential limit reached' }, 403);
    const el = await mount();
    await click(el, '[data-manual-setup-issue]');
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-manual-setup-action-error]')
    );
    expect(text(el)).to.include('Agent credential limit reached');
    expect(text(el)).to.include('<AGENT_KEY>');
  });

  it('cancels a pending setup after a confirm step and emits the event', async () => {
    setup = setupResponse({ pending });
    const el = await mount();
    const cancelled = sinon.spy();
    // Listen on the document to prove the event bubbles and is composed.
    document.addEventListener('manual-setup-cancelled', cancelled);

    try {
      await click(el, '[data-manual-setup-cancel]');
      expect(calls('POST', '/cancel')).to.have.length(0);
      expect(text(el)).to.include('Cancel this setup?');

      await click(el, '[data-manual-setup-cancel-confirm]');
      await waitUntil(() => cancelled.calledOnce, 'cancel event');
      await el.updateComplete;

      expect(calls('POST', '/enrollments/enrollment-1/cancel')).to.have.length(
        1
      );
      expect(cancelled.firstCall.args[0].detail.removed).to.deep.equal({
        credentials: 1,
        ai_models: 2,
        agent: 'decommissioned',
      });
      expect(cancelled.firstCall.args[0].detail.agentId).to.equal('agent-1');
      const result = el
        .shadowRoot!.querySelector('[data-manual-setup-cancelled]')!
        .textContent!.replace(/\s+/g, ' ');
      expect(result).to.include('1 key');
      expect(result).to.include('2 models');
      expect(result).to.include('decommissioned');
      expect(el.shadowRoot!.querySelector('[data-manual-setup-pending]')).to.not
        .exist;
    } finally {
      document.removeEventListener('manual-setup-cancelled', cancelled);
    }
  });

  it('keeps the setup when the confirm step is declined', async () => {
    setup = setupResponse({ pending });
    const el = await mount();
    await click(el, '[data-manual-setup-cancel]');
    await click(el, '[data-manual-setup-cancel-keep]');
    expect(calls('POST', '/cancel')).to.have.length(0);
    expect(el.shadowRoot!.querySelector('[data-manual-setup-cancel]')).to.exist;
  });

  it('says plainly when the setup is no longer pending (409)', async () => {
    setup = setupResponse({ pending });
    cancelResponse = () =>
      jsonResponse(
        {
          detail:
            'Only a setup that is waiting for local config can be cancelled',
        },
        409
      );
    const el = await mount();
    const cancelled = sinon.spy();
    el.addEventListener('manual-setup-cancelled', cancelled);

    await click(el, '[data-manual-setup-cancel]');
    // After the 409 the panel reloads and finds nothing pending.
    setup = setupResponse();
    await click(el, '[data-manual-setup-cancel-confirm]');
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-manual-setup-action-error]')
    );
    await waitUntil(
      () => !el.shadowRoot!.querySelector('[data-manual-setup-pending]')
    );
    expect(text(el)).to.include(
      'This setup is no longer pending, so there is nothing to cancel.'
    );
    expect(cancelled.called).to.be.false;
  });

  it('says when manual setup is not available for the agent type', async () => {
    setup = setupResponse({ agent_kind: 'custom', instructions: null });
    const el = await mount();
    const body = text(el);
    expect(body).to.include(
      'Manual setup is not available for this agent type yet'
    );
    expect(el.shadowRoot!.querySelector('[data-manual-setup-issue]')).to.not
      .exist;
    expect(
      el
        .shadowRoot!.querySelector('[data-manual-setup-docs]')!
        .getAttribute('href')
    ).to.equal(DOCS_URL);
  });

  it('renders a TOML file as TOML', async () => {
    setup = setupResponse({
      agent_kind: 'codex',
      instructions: codexInstructions,
    });
    const el = await mount();
    const block = el.shadowRoot!.querySelector(
      '[data-manual-setup-file] pre'
    )!.textContent!;
    expect(block).to.include('[mcp_servers.preloop]');
    expect(block).to.include('url = "https://preloop.example.com/mcp/v1"');
    expect(block).to.include('[mcp_servers.preloop.http_headers]');
    expect(block).to.include('Authorization = "Bearer <AGENT_KEY>"');
    // No env form for an empty env object.
    expect(el.shadowRoot!.querySelector('[data-manual-setup-env]')).to.not
      .exist;
  });

  it('hides the actions for a read-only agent', async () => {
    setup = setupResponse({ pending });
    const el = await fixture<AgentManualSetup>(
      html`<agent-manual-setup
        agent-id="agent-1"
        read-only
      ></agent-manual-setup>`
    );
    await waitUntil(
      () => !el.shadowRoot!.querySelector('[data-manual-setup-loading]')
    );
    expect(el.shadowRoot!.querySelector('[data-manual-setup-pending]')).to
      .exist;
    expect(el.shadowRoot!.querySelector('[data-manual-setup-cancel]')).to.not
      .exist;
    expect(el.shadowRoot!.querySelector('[data-manual-setup-issue]')).to.not
      .exist;
  });

  it('shows an error state with a retry and the docs link', async () => {
    fetchStub.callsFake(async () =>
      jsonResponse({ detail: 'Managed agent not found' }, 404)
    );
    const el = await mount();
    const error = el.shadowRoot!.querySelector('[data-manual-setup-error]');
    expect(error).to.exist;
    expect(error!.textContent).to.include('Managed agent not found');
    expect(
      el
        .shadowRoot!.querySelector('[data-manual-setup-docs]')!
        .getAttribute('href')
    ).to.equal(DOCS_URL);

    fetchStub.callsFake(async () => jsonResponse(setupResponse()));
    await click(el, '[data-manual-setup-retry]');
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-manual-setup-file]')
    );
    expect(el.shadowRoot!.querySelector('[data-manual-setup-error]')).to.not
      .exist;
  });
});
