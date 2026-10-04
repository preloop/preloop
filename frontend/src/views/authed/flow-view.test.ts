import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './flow-view';
import type { FlowView } from './flow-view';
import { flowRuntimeLabel } from './flow-view';

describe('FlowView model selection', () => {
  function createElement(): FlowView {
    return document.createElement('flow-view') as FlowView;
  }

  it('does not filter AI models by selected agent type', () => {
    const element = createElement() as any;
    element.flow = { name: 'Test', agent_type: 'gemini' };
    element.models = [
      { id: 'openai-1', name: 'GPT', provider_name: 'openai' },
      { id: 'anthropic-1', name: 'Claude', provider_name: 'anthropic' },
      { id: 'google-1', name: 'Gemini', provider_name: 'google' },
    ];

    expect(
      element.getSelectableModels().map((model: any) => model.id)
    ).to.deep.equal(['openai-1', 'anthropic-1', 'google-1']);
  });

  it('describes the gateway protocol selected for each harness', () => {
    const element = createElement() as any;

    expect(element.getAgentProtocolLabel('gemini')).to.equal(
      'Gemini-compatible gateway endpoint'
    );
    expect(element.getAgentProtocolLabel('codex')).to.equal(
      'OpenAI-compatible gateway endpoint'
    );
    expect(element.getAgentProtocolLabel('opencode')).to.equal(
      'OpenAI-compatible gateway endpoint'
    );
  });
});

describe('FlowView trigger summary', () => {
  function createElement(): any {
    return document.createElement('flow-view') as FlowView as any;
  }

  it('describes webhook triggers', () => {
    const element = createElement();
    element.flow = { name: 'Test', trigger_event_source: 'webhook' };
    expect(element.getTriggerSummary().label).to.equal('Webhook');
  });

  it('describes schedule triggers with the backend summary', () => {
    const element = createElement();
    element.flow = {
      name: 'Test',
      trigger_event_source: 'schedule',
      schedule_config: {
        type: 'daily',
        at: '09:00',
        timezone: 'Europe/Athens',
      },
      schedule_state: {
        active: true,
        type: 'daily',
        description: 'Daily at 09:00 (Europe/Athens)',
        timezone: 'Europe/Athens',
        next_run_at: '2026-08-17T06:00:00+00:00',
      },
    };
    expect(element.getTriggerSummary().label).to.equal(
      'Daily at 09:00 (Europe/Athens)'
    );
  });

  it('falls back to a generic label when schedule state is missing', () => {
    const element = createElement();
    element.flow = {
      name: 'Test',
      trigger_event_source: 'schedule',
      schedule_config: { type: 'daily', at: '09:00', timezone: 'UTC' },
    };
    expect(element.getTriggerSummary().label).to.equal('Schedule');
  });

  it('humanises tracker event ids the way the list does', () => {
    // The list said "Pull request opened, Pull request updated" while this
    // page printed "pull_request_opened, pull_request_updated" for the same
    // flow. Both now read the trigger through one function.
    const element = createElement();
    element.trackers = [{ id: 'tracker-1', name: 'GitHub - preloop' }];
    element.flow = {
      name: 'Test',
      trigger_event_source: 'tracker-1',
      trigger_event_types: ['pull_request_opened', 'pull_request_updated'],
    };
    expect(element.getTriggerSummary().label).to.equal(
      'GitHub - preloop \u00b7 Pull request opened, Pull request updated'
    );
  });
});

describe('FlowView runtime label', () => {
  it('names a runtime as the flow form names it', () => {
    expect(flowRuntimeLabel('opencode')).to.equal('OpenCode');
    expect(flowRuntimeLabel('codex')).to.equal('Codex CLI');
    expect(flowRuntimeLabel('gemini')).to.equal('Gemini CLI');
    // An id nobody has a label for is still better than an empty chip.
    expect(flowRuntimeLabel('some_new_runtime')).to.equal('some_new_runtime');
    expect(flowRuntimeLabel('')).to.equal('Unknown');
  });
});

describe('FlowView recent executions', () => {
  it('names what each run was about, since they all share the flow name', async () => {
    // Every row on this page is the same flow: without a subject the table
    // is three timestamps and a status, and a reader cannot pick a run.
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.flow = {
      id: 'flow-1',
      name: 'PR Reviewer',
      agent_type: 'codex',
      trigger_event_source: 'webhook',
    };
    element.recentExecutions = [
      {
        id: 'dee1da93-6d1e-4c0e-9f3a-2b1d0c4e5f60',
        status: 'FAILED',
        start_time: '2026-03-09T10:00:00Z',
        end_time: '2026-03-09T10:02:00Z',
        trigger_subject: 'spacecode/preloop-ios !17 · Merge Request Updated',
        trigger_subject_url:
          'https://gitlab.com/spacecode/preloop-ios/-/merge_requests/17',
      },
    ];
    document.body.appendChild(element);
    await element.updateComplete;

    try {
      const headers = Array.from(
        element.shadowRoot!.querySelectorAll('.executions-table thead th')
      ).map((th: any) => (th.textContent || '').trim());
      // The Actions column is gone: the row itself opens the run, so the
      // View button only repeated a route the row already offers.
      expect(headers).to.eql(['Subject', 'Status', 'Started', 'Duration']);

      const link = element.shadowRoot!.querySelector(
        '.executions-table .subject-cell a'
      )!;
      expect(link.textContent).to.contain('spacecode/preloop-ios !17');
      expect(link.getAttribute('href')).to.equal(
        'https://gitlab.com/spacecode/preloop-ios/-/merge_requests/17'
      );
    } finally {
      element.remove();
    }
  });
});

describe('FlowView detail page language', () => {
  /** A rendered detail page for one flow, torn down by the caller. */
  async function renderDetail(overrides: Record<string, unknown> = {}) {
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.flowId = 'flow-1';
    element.trackers = [{ id: 'tracker-1', name: 'GitHub - preloop' }];
    element.flow = {
      id: 'flow-1',
      name: 'Pull Request Reviewer',
      agent_type: 'opencode',
      is_enabled: true,
      trigger_event_source: 'tracker-1',
      trigger_event_types: ['pull_request_opened', 'pull_request_updated'],
      execution_stats: { total_execs: 95, running_execs: 0 },
      ...overrides,
    };
    element.recentExecutions = [
      {
        id: 'dee1da93-6d1e-4c0e-9f3a-2b1d0c4e5f60',
        status: 'SUCCEEDED',
        start_time: new Date(Date.now() - 3 * 3600_000).toISOString(),
        end_time: new Date(Date.now() - 3 * 3600_000 + 60_000).toISOString(),
        trigger_subject: 'preloop/preloop #138',
      },
    ];
    document.body.appendChild(element);
    await element.updateComplete;
    return element;
  }

  it('shows effective saved publication policy without claiming verification passed', async () => {
    const element = await renderDetail({
      effective_publication_policy: {
        mode: 'isolated',
        reason: 'Tracker authorization and isolation are checked at execution.',
        configured_check_ids: ['lint', 'unit-tests'],
        blockers: [],
        runtime_validation_required: true,
      },
    });
    try {
      const card = element.shadowRoot.querySelector(
        '[data-publication-policy]'
      );
      expect(card.textContent).to.include('Isolated verification configured');
      expect(card.textContent).to.include('lint, unit-tests');
      expect(card.textContent).to.include('Each execution must validate');
      expect(card.textContent).not.to.include('Verification passed');
    } finally {
      element.remove();
    }
  });

  it('keeps future policy modes readable before a console update', async () => {
    const element = await renderDetail({
      effective_publication_policy: {
        mode: 'future_mode',
        reason: 'A newly supported publication policy.',
        configured_check_ids: [],
        blockers: [],
        runtime_validation_required: true,
      },
    });
    try {
      const card = element.shadowRoot.querySelector(
        '[data-publication-policy]'
      );
      expect(card.querySelector('strong').textContent).to.equal(
        'Publication policy'
      );
      expect(card.textContent).to.include(
        'A newly supported publication policy.'
      );
    } finally {
      element.remove();
    }
  });

  it('renders an ungated customized flow explicitly', async () => {
    const element = await renderDetail({
      effective_publication_policy: {
        mode: 'ungated',
        reason: 'Publication has no required verification gate.',
        configured_check_ids: [],
        blockers: [],
        runtime_validation_required: false,
      },
    });
    try {
      expect(
        element.shadowRoot.querySelector('[data-publication-policy]')
          .textContent
      ).to.include('No verification gate');
    } finally {
      element.remove();
    }
  });

  it('uses the list verbs in the header actions', async () => {
    // The header said "Edit Flow / Disable / Test Run" for the three commands
    // the list kebab calls Edit, Pause and Run now, and "Test Run" implied a
    // rehearsal of a run that spends real money. Both surfaces now read the
    // same registry, so the page can also delete the flow it is showing.
    const element = await renderDetail();
    try {
      const labels = element
        .getFlowActions()
        .map((action: any) => action.label);
      expect(labels).to.eql(['Run now', 'Edit', 'Pause', 'Delete']);

      const paused = await renderDetail({ is_enabled: false });
      try {
        const pausedActions = paused.getFlowActions();
        expect(pausedActions.map((action: any) => action.label)).to.eql([
          'Run now',
          'Edit',
          'Resume',
          'Delete',
        ]);
        // Run now stays on screen, disabled, and says what has to change.
        const run = pausedActions.find(
          (action: any) => action.id === 'run-now'
        );
        expect(run.disabled).to.equal(true);
        expect(run.tooltip).to.equal('Resume the flow before running it');
      } finally {
        paused.remove();
      }
    } finally {
      element.remove();
    }
  });

  it('states the trigger, the runtime and the labels in the console register', async () => {
    const element = await renderDetail();
    try {
      const text = (element.shadowRoot!.textContent || '').replace(/\s+/g, ' ');
      expect(text).to.contain(
        'GitHub - preloop · Pull request opened, Pull request updated'
      );
      expect(text).to.not.contain('pull_request_opened');
      expect(text).to.contain('OpenCode');
      expect(text).to.not.contain('opencode');

      // Meta register: no colons, no bold labels, sentence-case card titles.
      const labels = Array.from(
        element.shadowRoot!.querySelectorAll('.detail-label')
      ).map((node: any) => (node.textContent || '').trim());
      expect(labels).to.include('Agent type');
      expect(labels.some((label: string) => label.endsWith(':'))).to.equal(
        false
      );
      expect(text).to.contain('Flow details');
      expect(text).to.contain('Recent executions');
      expect(text).to.not.contain('Flow Details');
      expect(text).to.not.contain('Recent Executions');
    } finally {
      element.remove();
    }
  });

  it('says Succeeded, times a run relatively and links to all of them', async () => {
    const element = await renderDetail();
    try {
      const row = element.shadowRoot!.querySelector(
        '.executions-table tbody tr'
      )!;
      const cells = Array.from(row.querySelectorAll('td')).map((cell: any) =>
        (cell.textContent || '').trim()
      );
      expect(cells[1]).to.equal('Succeeded');
      expect(cells[1]).to.not.equal('SUCCEEDED');
      // Relative, with the absolute value in the title, as every other list.
      expect(cells[2]).to.contain('ago');
      expect(row.querySelectorAll('td')[2].getAttribute('title')).to.be.a(
        'string'
      );
      // The row click is a convenience on top of a real anchor: without it a
      // keyboard user and cmd-click have no route from this card to a run.
      const runLink = row.querySelector('td:nth-child(3) a.row-link')!;
      expect(runLink.getAttribute('href')).to.equal(
        '/console/flows/executions/dee1da93-6d1e-4c0e-9f3a-2b1d0c4e5f60'
      );

      const link = element.shadowRoot!.querySelector('.all-executions a')!;
      expect((link.textContent || '').trim()).to.equal(
        'View all 95 executions'
      );
      expect(link.getAttribute('href')).to.equal(
        '/console/flows/executions?flow_id=flow-1'
      );
    } finally {
      element.remove();
    }
  });
});

describe('FlowView production logging', () => {
  function createElement(): FlowView {
    return document.createElement('flow-view') as FlowView;
  }

  afterEach(() => {
    document.body.querySelectorAll('sl-alert').forEach((node) => node.remove());
    localStorage.clear();
  });

  it('confirms enablement in a toast and does not use console.log', async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    const fetchStub = sinon.stub(window, 'fetch').resolves(
      new Response(JSON.stringify({ id: 'flow-1', is_enabled: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    const log = sinon.spy(console, 'log');
    const element = createElement() as any;
    element.flowId = 'flow-1';
    element.flow = { name: 'Test', is_enabled: false };

    try {
      await element.toggleFlowEnabled();
      expect(log.called, 'no console.log').to.equal(false);
      expect(element.flow.is_enabled).to.equal(true);
      const alert = document.body.querySelector('sl-alert');
      expect(alert?.textContent).to.contain('Flow enabled successfully');
    } finally {
      log.restore();
      fetchStub.restore();
    }
  });

  it('toasts the server reason when enabling a flow fails', async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    const alertStub = sinon.stub(window, 'alert');
    const fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input, init) => {
        const url = String(input);
        const method = (init?.method || 'GET').toUpperCase();
        if (url.includes('/api/v1/flows/') && method === 'PUT') {
          return new Response(
            JSON.stringify({ detail: 'Schedule is invalid' }),
            {
              status: 400,
              headers: { 'Content-Type': 'application/json' },
            }
          );
        }
        return new Response('{}', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
    const element = createElement() as any;
    element.flowId = 'flow-1';
    element.flow = { name: 'Test', is_enabled: false };

    try {
      await element.toggleFlowEnabled();
      expect(alertStub.called).to.equal(false);
      expect(element.flow.is_enabled).to.equal(false);
      const toast = document.body.querySelector('sl-alert');
      expect(toast?.textContent).to.contain('Schedule is invalid');
    } finally {
      alertStub.restore();
      fetchStub.restore();
    }
  });

  it('keeps console.log and console.debug out of the flow view', async () => {
    const response = await fetch(new URL('./flow-view.ts', import.meta.url));
    const source = await response.text();
    expect(source).to.not.match(/console\.log\s*\(/);
    expect(source).to.not.match(/console\.debug\s*\(/);
  });
});

describe('FlowView review instructions', () => {
  async function renderDetail(overrides: Record<string, unknown> = {}) {
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.flowId = 'flow-1';
    element.flow = {
      id: 'flow-1',
      name: 'Pull Request Reviewer',
      agent_type: 'codex',
      is_enabled: true,
      trigger_event_source: 'webhook',
      ...overrides,
    };
    element.recentExecutions = [];
    document.body.appendChild(element);
    await element.updateComplete;
    return element;
  }

  it('shows review instructions read-only when they are set', async () => {
    const element = await renderDetail({
      review_instructions: 'Keep the declared runtime.',
    });
    try {
      const card = element.shadowRoot.querySelector(
        '[data-review-instructions]'
      );
      expect(card).to.exist;
      expect(card.textContent).to.include('Review instructions');
      expect(card.textContent).to.include('Keep the declared runtime.');
      expect(card.querySelector('sl-textarea')).to.equal(null);
    } finally {
      element.remove();
    }
  });

  it('hides review instructions when they are unset or blank', async () => {
    const unset = await renderDetail();
    const blank = await renderDetail({ review_instructions: '   ' });
    try {
      expect(
        unset.shadowRoot.querySelector('[data-review-instructions]')
      ).to.equal(null);
      expect(
        blank.shadowRoot.querySelector('[data-review-instructions]')
      ).to.equal(null);
    } finally {
      unset.remove();
      blank.remove();
    }
  });
});

describe('FlowView progressive detail', () => {
  it('shows flow details without waiting for executions and avoids editor-only catalogs', async () => {
    localStorage.setItem('accessToken', 'test-token');
    let release!: () => void;
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    const fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = input.toString();
        if (url.includes('/flows/executions')) await pending;
        const data = url.startsWith('/api/v1/account/governance/flows/flow-1')
          ? { config: {}, has_override: false, account_defaults: {} }
          : url.startsWith('/api/v1/flows/flow-1')
            ? {
                id: 'flow-1',
                name: 'Nightly sweep',
                agent_type: 'codex',
                trigger_event_source: 'webhook',
                allowed_mcp_servers: [],
                allowed_mcp_tools: [],
              }
            : url.endsWith('/auth/users/me')
              ? { id: 'user-1' }
              : [];
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
    try {
      const el = await fixture<FlowView>(
        html`<flow-view flowId="flow-1"></flow-view>`
      );
      await waitUntil(() => (el as any).flowReady);
      await el.updateComplete;
      expect(el.shadowRoot!.textContent).to.include('Nightly sweep');
      expect(
        fetchStub
          .getCalls()
          .filter((call) =>
            /\/(tools|mcp-servers|agents)(?:\?|$)/.test(call.args[0].toString())
          )
      ).to.have.length(0);
      expect(
        fetchStub
          .getCalls()
          .filter((call) => (call.args[1]?.method || 'GET') !== 'GET')
      ).to.have.length(0);
      expect(
        fetchStub
          .getCalls()
          .some((call) =>
            call.args[0].toString().includes('/account/governance/flows/')
          )
      ).to.equal(false);
      const disclosure = el.shadowRoot!.querySelector(
        '.flow-governance-disclosure'
      ) as HTMLDetailsElement;
      disclosure.open = true;
      await waitUntil(() =>
        fetchStub
          .getCalls()
          .some((call) => call.args[0].toString() === '/api/v1/tools')
      );
      expect(el.shadowRoot!.querySelector('flow-governance-card')).to.exist;
      const card = el.shadowRoot!.querySelector('flow-governance-card') as any;
      await waitUntil(() => !card.loading);
      const catalogCalls = fetchStub
        .getCalls()
        .filter((call) => call.args[0].toString() === '/api/v1/tools').length;
      disclosure.open = false;
      await el.updateComplete;
      disclosure.open = true;
      await el.updateComplete;
      expect(
        fetchStub
          .getCalls()
          .filter((call) => call.args[0].toString() === '/api/v1/tools')
      ).to.have.length(catalogCalls);
    } finally {
      release();
      fetchStub.restore();
      localStorage.clear();
    }
  });
});

describe('FlowView governance card', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    fetchStub = sinon
      .stub(window, 'fetch')
      .resolves(new Response('{}', { status: 404 }));
  });

  afterEach(() => {
    fetchStub.restore();
  });

  it('passes the flow allowed tools to the governance card', async () => {
    const { render } = await import('lit');
    const element = document.createElement('flow-view') as any;
    element.flowId = 'flow-1';
    element.flow = {
      name: 'Test',
      allowed_mcp_tools: [
        { server_name: 'preloop', tool_name: 'search_issues' },
      ],
    };
    const container = document.createElement('div');
    render(element.renderGovernanceCard(), container);
    const card = container.querySelector('flow-governance-card') as any;
    expect(card).to.exist;
    expect(card.flowId).to.equal('flow-1');
    expect(card.allowedToolNames).to.deep.equal(['search_issues']);
  });

  it('renders no governance card for an unsaved flow', () => {
    const element = document.createElement('flow-view') as any;
    element.flowId = undefined;
    expect(element.renderGovernanceCard()).to.equal('');
  });
});

describe('FlowView all-of labels filter', () => {
  it('round-trips labels_all through trigger_config', async () => {
    const { render } = await import('lit');
    const element = document.createElement('flow-view') as any;
    element.trackers = [
      { id: 'tracker-1', name: 'GitHub', tracker_type: 'github' },
    ];
    element.flow = {
      trigger_event_source: 'tracker-1',
      trigger_event_types: ['issue_labeled'],
      trigger_config: {
        labels: ['agent-ready'],
        labels_all: ['complexity:low'],
      },
    };
    element.requestUpdate = () => {};
    const host = document.createElement('div');
    document.body.appendChild(host);
    try {
      render(element.renderEventFilters(), host);
      const input = [...host.querySelectorAll('sl-input')].find(
        (el: any) =>
          el.getAttribute('label') ===
          'Issue must also carry all of these labels'
      ) as any;
      expect(input).to.exist;
      await input.updateComplete;
      expect(input.value).to.equal('complexity:low');

      input.value = ' complexity:medium, risk:low ';
      input.dispatchEvent(new CustomEvent('sl-input'));
      expect(element.flow.trigger_config).to.deep.equal({
        labels: ['agent-ready'],
        labels_all: ['complexity:medium', 'risk:low'],
      });

      input.value = '';
      input.dispatchEvent(new CustomEvent('sl-input'));
      expect(element.flow.trigger_config).to.deep.equal({
        labels: ['agent-ready'],
      });
    } finally {
      host.remove();
    }
  });
});

describe('FlowView saving from the form', () => {
  afterEach(() => {
    localStorage.clear();
  });

  it('hands the save back to the form and puts a server error on it', async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    const fetchStub = sinon.stub(window, 'fetch').callsFake(async () => {
      return new Response(JSON.stringify({ detail: 'Name already taken' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    const element = document.createElement('flow-view') as any;
    element.isNew = false;
    element.flowId = 'flow-1';

    // The form lives in the view's shadow root, so once dispatch ends the
    // event's target is cleared. The error must still reach the form.
    const host = document.createElement('div');
    const root = host.attachShadow({ mode: 'open' });
    const form = document.createElement('div') as HTMLElement & {
      formError?: string;
    };
    root.appendChild(form);
    document.body.appendChild(host);
    const waited: Promise<unknown>[] = [];
    form.addEventListener('flow-submit', (event) => {
      const save = element.saveFlowFromForm(event as CustomEvent);
      (event as CustomEvent).detail.waitUntil(save);
    });

    try {
      form.dispatchEvent(
        new CustomEvent('flow-submit', {
          bubbles: true,
          composed: true,
          detail: {
            flow: { name: 'Example flow' },
            waitUntil: (work: Promise<unknown>) => waited.push(work),
          },
        })
      );
      expect(waited).to.have.length(1);
      await waited[0];
      expect(form.formError).to.equal('Name already taken');
    } finally {
      host.remove();
      fetchStub.restore();
    }
  });
});
