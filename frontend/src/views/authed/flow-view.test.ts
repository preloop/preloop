import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import { resetTalkWindowsForTests } from '../../utils/talk-window';
import { Router } from '../../router';
import sinon from 'sinon';

import './flow-view';
import type { FlowView } from './flow-view';
import { flowRuntimeLabel, referenceListsSentence } from './flow-view';

describe('FlowView reference list names', () => {
  it('names one, two or more failed lists as a sentence start', () => {
    expect(referenceListsSentence(['projects'])).to.equal('Projects');
    expect(referenceListsSentence(['trackers', 'projects'])).to.equal(
      'Trackers and projects'
    );
    expect(
      referenceListsSentence(['models', 'mcpServers', 'organizations'])
    ).to.equal('Models, MCP servers and organizations');
  });
});

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
  it('says how long the flow has been failing above its runs', async () => {
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.flow = {
      id: 'flow-1',
      name: 'Merge Request Reviewer',
      agent_type: 'codex',
      trigger_event_source: 'webhook',
    };
    element.recentExecutions = [
      {
        id: 'e-2',
        status: 'FAILED',
        failure_category: 'repository_access',
        start_time: new Date(Date.now() - 3600_000).toISOString(),
      },
      {
        id: 'e-1',
        status: 'FAILED',
        failure_category: 'repository_access',
        start_time: new Date(Date.now() - 3 * 86400_000).toISOString(),
      },
    ];
    document.body.appendChild(element);
    await element.updateComplete;
    try {
      const banner = element.shadowRoot!.querySelector(
        '[data-testid="flow-failing-since"]'
      ) as HTMLElement;
      expect(banner, 'failing banner').to.exist;
      const text = banner.textContent!.replace(/\s+/g, ' ');
      expect(text).to.contain('The last 2 runs failed');
      expect(text).to.contain('repository access');
      expect(text).to.contain('failing since 3d ago');
      // Ten loaded failures: the streak may be longer than the window.
      element.recentExecutions = Array.from({ length: 10 }, (_, i) => ({
        id: `e-${i}`,
        status: 'FAILED',
        failure_category: 'repository_access',
        start_time: new Date(Date.now() - i * 86400_000).toISOString(),
      }));
      await element.updateComplete;
      const longer = element
        .shadowRoot!.querySelector('[data-testid="flow-failing-since"]')!
        .textContent!.replace(/\s+/g, ' ');
      expect(longer).to.contain('At least the last 10 runs failed');
      expect(longer).to.contain('failing since at least');
    } finally {
      element.remove();
    }
  });

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
      // A failed run is the solid danger pill here too, as on the
      // executions list and the flows list (02-8).
      const chip = element.shadowRoot!.querySelector(
        '.executions-table sl-badge.chip'
      )!;
      expect(chip.classList.contains('solid')).to.be.true;

      expect(link.textContent).to.contain('spacecode/preloop-ios !17');
      expect(link.getAttribute('href')).to.equal(
        'https://gitlab.com/spacecode/preloop-ios/-/merge_requests/17'
      );
    } finally {
      element.remove();
    }
  });
});

describe('FlowView interactive conversation rows', () => {
  it('says a live conversation is waiting and links its Talk window', async () => {
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.flow = {
      id: 'flow-1',
      name: 'Example flow',
      agent_type: 'opencode',
      trigger_event_source: 'webhook',
      agent_config: {},
    };
    element.recentExecutions = [
      {
        id: 'execution-live',
        status: 'RUNNING',
        start_time: '2026-03-09T10:00:00Z',
      },
      {
        id: 'execution-done',
        status: 'RUNNING',
        start_time: '2026-03-09T09:00:00Z',
      },
    ];
    const conversation = (id: string, execution: string, state: string) => ({
      id,
      flow_id: 'flow-1',
      owner_user_id: 'owner-1',
      execution_id: execution,
      state,
      notice: '',
      can_write: true,
      turns: 1,
      max_turns: 30,
      idle_deadline: null,
      messages: [],
    });
    element.conversationsByExecution = new Map([
      [
        'execution-live',
        conversation('conversation-live', 'execution-live', 'waiting'),
      ],
      [
        'execution-done',
        conversation('conversation-done', 'execution-done', 'ended'),
      ],
    ]);
    document.body.appendChild(element);
    await element.updateComplete;
    try {
      const rows = element.shadowRoot!.querySelectorAll(
        '.executions-table tbody tr'
      );
      expect(rows[0].textContent).to.contain('Conversation · Waiting for you');
      expect(rows[0].textContent).not.to.contain('Running');
      const talk = [...rows[0].querySelectorAll('a')].find(
        (a: any) => a.textContent.trim() === 'Open Talk'
      ) as HTMLAnchorElement;
      expect(talk.getAttribute('href')).to.equal(
        '/console/flow-conversations/conversation-live/talk'
      );
      // A finished conversation falls back to the execution's own status.
      expect(rows[1].textContent).not.to.contain('Conversation ·');
    } finally {
      element.remove();
    }
  });
});

describe('FlowView conversation row refresh', () => {
  afterEach(() => sinon.restore());

  it('loads conversations for an interactive flow and lets a finished run fall back', async () => {
    localStorage.setItem('accessToken', 'test-token');
    const conversation = {
      id: 'conversation-live',
      flow_id: 'flow-1',
      owner_user_id: 'owner-1',
      execution_id: 'execution-live',
      state: 'working',
      notice: '',
      can_write: true,
      turns: 1,
      max_turns: 30,
      idle_deadline: null,
      messages: [],
    };
    const fetch = sinon.stub(window, 'fetch').callsFake(
      async () =>
        new Response(JSON.stringify([conversation]), {
          headers: { 'Content-Type': 'application/json' },
        })
    );
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.flowId = 'flow-1';
    element.flow = {
      id: 'flow-1',
      name: 'Example flow',
      agent_type: 'opencode',
      trigger_event_source: 'webhook',
      agent_config: { interactive: { enabled: true } },
    };
    element.recentExecutions = [
      { id: 'execution-live', status: 'RUNNING', start_time: '2026-03-09T10:00:00Z' },
    ];
    document.body.appendChild(element);
    try {
      await element.loadExecutionConversations(element.flowLoadGeneration);
      await element.updateComplete;
      const url = String(
        fetch.getCalls().find((call) =>
          String(call.args[0]).includes('flow-conversations')
        )!.args[0]
      );
      expect(url).to.contain('flow_id=flow-1');
      const row = () =>
        element.shadowRoot!.querySelector('.executions-table tbody tr')!;
      expect(row().textContent).to.contain('Conversation · Working');

      // A stale generation is dropped.
      fetch.callsFake(
        async () =>
          new Response(JSON.stringify([{ ...conversation, state: 'waiting' }]), {
            headers: { 'Content-Type': 'application/json' },
          })
      );
      await element.loadExecutionConversations(element.flowLoadGeneration - 1);
      await element.updateComplete;
      expect(row().textContent).to.contain('Conversation · Working');

      // The socket marks the run finished before the snapshot is refreshed.
      element.recentExecutions = [
        {
          id: 'execution-live',
          status: 'SUCCEEDED',
          start_time: '2026-03-09T10:00:00Z',
          end_time: '2026-03-09T10:05:00Z',
        },
      ];
      await element.updateComplete;
      expect(row().textContent).not.to.contain('Conversation ·');
      expect(row().textContent).not.to.contain('Open Talk');
    } finally {
      element.remove();
      localStorage.clear();
    }
  });

  it('does not ask for conversations when the flow is not interactive', async () => {
    const fetch = sinon.stub(window, 'fetch');
    const element = document.createElement('flow-view') as any;
    element.flowId = 'flow-1';
    element.flow = { id: 'flow-1', agent_config: {} };
    await element.loadExecutionConversations(element.flowLoadGeneration);
    expect(
      fetch.getCalls().some((call) =>
        String(call.args[0]).includes('flow-conversations')
      )
    ).to.equal(false);
  });
});

describe('FlowView compact counts (U-01)', () => {
  it('states the executions total compactly with the exact count in the title', async () => {
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.flowId = 'flow-1';
    element.flow = {
      id: 'flow-1',
      name: 'PR Reviewer',
      agent_type: 'codex',
      execution_stats: { total_execs: 1998, running_execs: 0 },
    };
    element.recentExecutions = [
      {
        id: 'dee1da93-6d1e-4c0e-9f3a-2b1d0c4e5f60',
        status: 'SUCCEEDED',
        start_time: '2026-03-09T10:00:00Z',
        end_time: '2026-03-09T10:02:00Z',
      },
    ];
    document.body.appendChild(element);
    await element.updateComplete;
    try {
      const link = element.shadowRoot!.querySelector('.all-executions a')!;
      expect((link.textContent || '').trim()).to.equal(
        'View all 2K executions'
      );
      expect(link.getAttribute('title')).to.equal('1,998 executions');
    } finally {
      element.remove();
    }
  });
});

describe('FlowView summary strip (F-02)', () => {
  it('states runs, spend and how the recent runs went, sample named', async () => {
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.flowId = 'flow-1';
    element.flow = {
      id: 'flow-1',
      name: 'PR Reviewer',
      agent_type: 'codex',
      execution_stats: {
        total_execs: 1998,
        running_execs: 1,
        estimated_cost: 42.5,
      },
    };
    const minutesAgo = (m: number) =>
      new Date(Date.now() - m * 60_000).toISOString();
    element.recentExecutions = [
      {
        id: 'e1',
        status: 'FAILED',
        start_time: minutesAgo(10),
        end_time: minutesAgo(8),
      },
      {
        id: 'e2',
        status: 'SUCCEEDED',
        start_time: minutesAgo(60),
        end_time: minutesAgo(56),
      },
      {
        id: 'e3',
        status: 'SUCCEEDED',
        start_time: minutesAgo(120),
        end_time: minutesAgo(118),
      },
      { id: 'e4', status: 'RUNNING', start_time: minutesAgo(1) },
      // A legacy terminal run with no end time has no duration to average.
      { id: 'e5', status: 'SUCCEEDED', start_time: minutesAgo(9000) },
    ];
    document.body.appendChild(element);
    await element.updateComplete;
    try {
      const strip = element.shadowRoot!.querySelector(
        '[data-testid="flow-summary-strip"]'
      ) as HTMLElement;
      expect(strip, 'summary strip').to.exist;
      const text = (strip.textContent || '').replace(/\s+/g, ' ').trim();
      expect(text).to.contain('2K runs');
      expect(text).to.contain('1 running');
      expect(text).to.contain('$42.50 est.');
      // Four finished runs in the sample: one failed.
      expect(text).to.contain('25% failed');
      expect(text).to.contain('avg 2m 40s');
      expect(text).to.contain('last success 1h ago');
      const sample = strip.querySelector(
        '[data-testid="flow-summary-sample"]'
      )!;
      expect(sample.getAttribute('title')).to.contain('last 4 finished runs');
    } finally {
      element.remove();
    }
  });
});

describe('FlowView recent executions width (B-09)', () => {
  it('gives the subject the slack instead of a quarter of the table', async () => {
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.flow = { id: 'flow-1', name: 'PR Reviewer', agent_type: 'codex' };
    element.recentExecutions = [
      {
        id: 'dee1da93-6d1e-4c0e-9f3a-2b1d0c4e5f60',
        status: 'SUCCEEDED',
        start_time: '2026-03-09T10:00:00Z',
        end_time: '2026-03-09T10:02:00Z',
        trigger_subject:
          'example/example #1203 · Pull request synchronize · 949d625b',
        trigger_subject_url: 'https://example.com/example/example/pull/1203',
      },
    ];
    const host = document.createElement('div');
    host.style.width = '760px';
    host.appendChild(element);
    document.body.appendChild(host);
    await element.updateComplete;
    try {
      const table = element.shadowRoot!.querySelector(
        '.executions-table'
      ) as HTMLElement;
      const subject = table.querySelector('td.subject-cell') as HTMLElement;
      expect(subject.clientWidth).to.be.at.least(table.clientWidth * 0.5);
    } finally {
      host.remove();
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

  it('collapses a long prompt to 10 lines with a toggle (#1692)', async () => {
    const prompt = Array.from({ length: 400 }, (_, i) => `line ${i}`).join(
      '\n'
    );
    const element = await renderDetail({ prompt_template: prompt });
    try {
      const pre = element.shadowRoot.querySelector(
        '[data-prompt-template]'
      ) as HTMLElement;
      expect(pre.classList.contains('collapsed')).to.equal(true);
      const lineHeight = parseFloat(getComputedStyle(pre).lineHeight);
      // Exactly ten visible lines: the box minus its padding is 10 lines.
      const style = getComputedStyle(pre);
      const visible =
        pre.getBoundingClientRect().height -
        parseFloat(style.paddingTop) -
        parseFloat(style.paddingBottom);
      expect(visible / lineHeight).to.be.closeTo(10, 0.05);
      const toggle = element.shadowRoot.querySelector(
        '[data-prompt-toggle]'
      ) as HTMLElement;
      expect(toggle.textContent).to.include('Show full prompt');
      toggle.click();
      await element.updateComplete;
      expect(pre.classList.contains('collapsed')).to.equal(false);
      expect(pre.getBoundingClientRect().height).to.be.above(lineHeight * 100);
    } finally {
      element.remove();
    }
  });

  it('offers no toggle for a short prompt', async () => {
    const element = await renderDetail({ prompt_template: 'one\ntwo' });
    try {
      expect(element.shadowRoot.querySelector('[data-prompt-toggle]')).to.not
        .exist;
    } finally {
      element.remove();
    }
  });

  it('puts recent executions before the prompt (#1692)', async () => {
    const element = await renderDetail({ prompt_template: 'Review it' });
    try {
      const runs = element.shadowRoot.querySelector('[data-recent-executions]');
      const pre = element.shadowRoot.querySelector('[data-prompt-template]');
      expect(runs).to.exist;
      expect(
        runs.compareDocumentPosition(pre) & Node.DOCUMENT_POSITION_FOLLOWING
      ).to.not.equal(0);
    } finally {
      element.remove();
    }
  });

  it('puts recent executions before the flow details (#1692)', async () => {
    const element = await renderDetail({ prompt_template: 'Review it' });
    try {
      const runs = element.shadowRoot.querySelector('[data-recent-executions]');
      const details = element.shadowRoot.querySelector('[data-flow-details]');
      expect(runs).to.exist;
      expect(details).to.exist;
      expect(
        runs.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING
      ).to.not.equal(0);
    } finally {
      element.remove();
    }
  });

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

  it('shows no run limits with a hint to set one', async () => {
    const element = await renderDetail({ effective_limits: {} });
    try {
      const cell = element.shadowRoot.querySelector('[data-run-limits]');
      const text = (cell.textContent || '').replace(/\s+/g, ' ').trim();
      expect(text).to.contain('None.');
      expect(cell.querySelector('[data-run-limits-hint]')).to.exist;
    } finally {
      element.remove();
    }
  });

  it('shows the effective run limits and marks the defaults', async () => {
    const element = await renderDetail({
      effective_limits: {
        max_total_tokens: { value: 5000000, source: 'flow' },
        max_turns: { value: 250, source: 'environment' },
        max_usd: { value: 4, source: 'flow' },
      },
    });
    try {
      const cell = element.shadowRoot.querySelector('[data-run-limits]');
      const text = (cell.textContent || '').replace(/\s+/g, ' ').trim();
      expect(text).to.contain('5M tokens');
      expect(text).to.contain('250 turns (instance default)');
      expect(text).to.contain('$4.00 est.');
      expect(text).to.not.contain('$4.00 est. (');
    } finally {
      element.remove();
    }
  });

  it('disables Run now on a read-only review flow and says why', async () => {
    const element = await renderDetail({
      git_clone_config: { enabled: true, publication_mode: 'read_only' },
    });
    try {
      const run = element
        .getFlowActions()
        .find((action: any) => action.id === 'run-now');
      expect(run.disabled).to.equal(true);
      expect(run.tooltip).to.contain('CI identity');
      expect(run.tooltip).to.contain('POST /api/v1/flows/flow-1/trigger');
      expect(run.tooltip).to.contain('pr_number');
      expect(run.tooltip).to.contain('head_sha');
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

describe('FlowView webhook URL', () => {
  let element: any;
  let fetchStub: sinon.SinonStub;
  let toastStub: sinon.SinonStub;

  beforeEach(async () => {
    toastStub = sinon
      .stub(customElements.get('sl-alert')!.prototype, 'toast')
      .resolves();
    localStorage.setItem('accessToken', 'test-token');
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        if (input.toString().endsWith('/webhook-secret/rotate')) {
          return new Response(
            JSON.stringify({
              webhook_secret: 'new-example-secret',
              webhook_secret_hint: 'cret',
              webhook_secret_set: true,
              webhook_secret_rotated_at: '2026-11-16T12:00:00Z',
              execution_stats: null,
              revision: 8,
            }),
            { status: 200 }
          );
        }
        return new Response('{}', { status: 200 });
      });
    element = document.createElement('flow-view') as any;
    Object.assign(element, {
      flowReady: true,
      isNew: false,
      isEditing: false,
      initialized: true,
      flowId: 'flow-1',
      canEditFlows: true,
      flow: {
        id: 'flow-1',
        name: 'Inbound hook',
        agent_type: 'codex',
        trigger_event_source: 'webhook',
        webhook_secret_set: true,
        webhook_secret_hint: 'abcd',
        webhook_config: {},
        execution_stats: { total_execs: 7 },
        revision: 7,
      },
    });
    document.body.appendChild(element);
    await element.updateComplete;
    await new Promise((resolve) => setTimeout(resolve, 0));
    element.canEditFlows = true;
    await element.updateComplete;
  });

  afterEach(() => {
    element.remove();
    localStorage.clear();
    fetchStub.restore();
    toastStub.restore();
    document.querySelector('confirm-dialog')?.remove();
    document.querySelectorAll('sl-alert').forEach((alert) => alert.remove());
  });

  it('keeps a URL template and status visible to editors without a read secret', () => {
    const input = element.shadowRoot.querySelector(
      'sl-input[label="Webhook URL"]'
    );
    expect(input?.value).to.contain('/flow-1/{webhook_secret}');
    expect(element.shadowRoot.textContent).to.contain('abcd');
    expect(element.shadowRoot.querySelector('[data-rotate-webhook-secret]')).to
      .exist;
  });

  it('hides the URL and rotation action from readers', async () => {
    element.canEditFlows = false;
    await element.updateComplete;
    expect(element.shadowRoot.querySelector('[data-rotate-webhook-secret]')).not
      .to.exist;
    expect(element.shadowRoot.querySelector('sl-input[label="Webhook URL"]'))
      .not.to.exist;
  });

  it('cancels rotation without sending a request', async () => {
    const pending = element.rotateWebhookSecret();
    const dialog = document.querySelector('confirm-dialog') as any;
    await dialog.updateComplete;
    dialog.shadowRoot.querySelector('sl-button[slot="footer"]').click();
    await pending;
    expect(
      fetchStub.getCalls().some((call) => call.args[1]?.method === 'POST')
    ).to.equal(false);
    expect(element.newWebhookSecret).to.equal(null);
  });

  it('keeps the secret hidden when rotation fails', async () => {
    fetchStub.callsFake(
      async () =>
        new Response(JSON.stringify({ detail: 'edit_flows required' }), {
          status: 403,
        })
    );
    const pending = element.rotateWebhookSecret();
    const dialog = document.querySelector('confirm-dialog') as any;
    await dialog.updateComplete;
    dialog.shadowRoot
      .querySelector('[data-testid="confirm-dialog-confirm"]')
      .click();
    await pending;
    expect(element.newWebhookSecret).to.equal(null);
    expect(element.rotatingWebhookSecret).to.equal(false);
  });

  it('shows the new URL once after confirmed rotation and clears it on navigation', async () => {
    element.shadowRoot.querySelector('[data-rotate-webhook-secret]').click();
    const dialog = document.querySelector('confirm-dialog') as any;
    await dialog.updateComplete;
    dialog.shadowRoot
      .querySelector('[data-testid="confirm-dialog-confirm"]')
      .click();
    await waitUntil(() =>
      element.shadowRoot
        .querySelector('sl-input[label="Webhook URL"]')
        ?.value.includes('new-example-secret')
    );
    const call = fetchStub
      .getCalls()
      .find((call) =>
        call.args[0].toString().endsWith('/webhook-secret/rotate')
      );
    expect(call?.args[1]?.method).to.equal('POST');
    expect(element.flow.webhook_secret).to.be.undefined;
    expect(element.flow.execution_stats).to.deep.equal({ total_execs: 7 });
    // Rotation moved the revision; the next save must not send the old one.
    expect(element.flow.revision).to.equal(8);
    expect(element.shadowRoot.textContent).to.contain('shown once');
    element.disconnectedCallback();
    expect(element.newWebhookSecret).to.equal(null);
  });

  for (const change of ['disconnect', 'reload', 'another-flow']) {
    it(`warns when a confirmed rotation completes after ${change}`, async () => {
      let finish!: (response: Response) => void;
      fetchStub.callsFake((input: RequestInfo | URL) => {
        if (input.toString().endsWith('/webhook-secret/rotate')) {
          return new Promise<Response>((resolve) => {
            finish = resolve;
          });
        }
        return Promise.resolve(new Response('{}', { status: 200 }));
      });
      const pending = element.rotateWebhookSecret();
      const dialog = document.querySelector('confirm-dialog') as any;
      await dialog.updateComplete;
      dialog.shadowRoot
        .querySelector('[data-testid="confirm-dialog-confirm"]')
        .click();
      await waitUntil(() => typeof finish === 'function');
      if (change === 'disconnect') element.remove();
      else if (change === 'reload') ++element.flowLoadGeneration;
      else element.flowId = 'flow-2';
      finish(
        new Response(
          JSON.stringify({ webhook_secret: 'late-example-secret' }),
          { status: 200 }
        )
      );
      await pending;
      const warning = [...document.querySelectorAll('sl-alert')].find((alert) =>
        alert.textContent?.includes('Webhook secret rotated')
      ) as any;
      expect(warning).to.exist;
      expect(warning.textContent).to.contain('Rotate again');
      expect(warning.duration).to.equal(Infinity);
      expect(warning.textContent).not.to.contain('late-example-secret');
      expect(element.newWebhookSecret).to.equal(null);
    });
  }
});

describe('FlowView load failure', () => {
  afterEach(() => {
    localStorage.clear();
  });

  it('shows an error with Back to flows and Try again instead of spinning', async () => {
    localStorage.setItem('accessToken', 'test-token');
    let flowFails = true;
    const fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = input.toString();
        if (url.startsWith('/api/v1/flows/flow-1')) {
          return flowFails
            ? new Response(JSON.stringify({ detail: 'Flow not found' }), {
                status: 404,
                headers: { 'Content-Type': 'application/json' },
              })
            : new Response(
                JSON.stringify({
                  id: 'flow-1',
                  name: 'Nightly sweep',
                  agent_type: 'codex',
                  trigger_event_source: 'webhook',
                  allowed_mcp_servers: [],
                  allowed_mcp_tools: [],
                }),
                { status: 200, headers: { 'Content-Type': 'application/json' } }
              );
        }
        return new Response('[]', {
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
      const root = el.shadowRoot!;
      expect(root.querySelector('sl-spinner')).to.equal(null);
      const alert = root.querySelector('[data-flow-load-error]');
      expect(alert?.getAttribute('role')).to.equal('alert');
      expect(alert?.textContent).to.include('Could not load this flow');
      const back = root.querySelector('sl-button[href="/console/flows"]');
      expect(back?.textContent).to.include('Back to flows');

      flowFails = false;
      const retry = [...alert!.querySelectorAll('sl-button')].find((b) =>
        b.textContent?.includes('Try again')
      ) as HTMLElement;
      retry.click();
      await waitUntil(() => (el as any).flowReady && !(el as any).loadError);
      await el.updateComplete;
      expect(root.textContent).to.include('Nightly sweep');
    } finally {
      fetchStub.restore();
    }
  });

  it('warns inline when trackers and models could not be loaded', async () => {
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.referenceListsFailed = ['trackers', 'models'];
    element.flow = {
      id: 'flow-1',
      name: 'PR Reviewer',
      agent_type: 'codex',
      trigger_event_source: 'webhook',
    };
    document.body.appendChild(element);
    try {
      await element.updateComplete;
      const warning = element.shadowRoot.querySelector(
        '[data-reference-data-warning]'
      );
      expect(warning?.textContent).to.include(
        'Trackers and models could not be loaded'
      );
    } finally {
      element.remove();
    }
  });

  it('names the lists that failed and retries only those', async () => {
    localStorage.setItem('accessToken', 'test-token');
    const failing = new Set(['/api/v1/trackers', '/api/v1/projects']);
    const requested: string[] = [];
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    const fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = input.toString();
        requested.push(url);
        if (url.startsWith('/api/v1/flows/flow-1/executions')) {
          return json([]);
        }
        if (url.startsWith('/api/v1/flows/flow-1')) {
          return json({
            id: 'flow-1',
            name: 'Nightly sweep',
            agent_type: 'codex',
            trigger_event_source: 'webhook',
            allowed_mcp_servers: [],
            allowed_mcp_tools: [],
          });
        }
        const path = url.split('?')[0];
        if (failing.has(path)) {
          return json({ detail: 'Unavailable' }, 503);
        }
        if (path === '/api/v1/trackers') {
          return json([{ id: 'tracker-1', name: 'Example tracker' }]);
        }
        return json([]);
      });
    try {
      const el = await fixture<FlowView>(
        html`<flow-view flowId="flow-1"></flow-view>`
      );
      const view = el as any;
      await waitUntil(() => view.flowReady && !view._loadingReferenceData);
      await el.updateComplete;
      const warning = () =>
        el.shadowRoot!.querySelector('[data-reference-data-warning]');
      expect(warning()?.textContent).to.include(
        'Trackers and projects could not be loaded.'
      );
      expect(warning()?.textContent).not.to.include('models');

      // Trackers come back; projects are still down.
      failing.delete('/api/v1/trackers');
      requested.length = 0;
      const retry = () =>
        [...warning()!.querySelectorAll('sl-button')].find((b) =>
          b.textContent?.includes('Try again')
        ) as HTMLElement;
      retry().click();
      await waitUntil(
        () => requested.length > 0 && !view.retryingReferenceLists
      );
      await el.updateComplete;
      expect(requested.map((url) => url.split('?')[0]).sort()).to.deep.equal([
        '/api/v1/projects',
        '/api/v1/trackers',
      ]);
      expect(view.trackers.map((t: any) => t.id)).to.deep.equal(['tracker-1']);
      expect(warning()?.textContent).to.include(
        'Projects could not be loaded.'
      );

      failing.clear();
      requested.length = 0;
      retry().click();
      await waitUntil(
        () => requested.length > 0 && !view.retryingReferenceLists
      );
      await el.updateComplete;
      expect(requested.map((url) => url.split('?')[0])).to.deep.equal([
        '/api/v1/projects',
      ]);
      expect(warning()).to.equal(null);
    } finally {
      fetchStub.restore();
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

describe('FlowView saving a flow someone else changed', () => {
  afterEach(() => {
    localStorage.clear();
  });

  it('sends If-Match, keeps the edit on 412 and offers a reload', async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    const sent: RequestInit[] = [];
    const fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (_input, init) => {
        sent.push(init || {});
        return new Response(
          JSON.stringify({
            detail: {
              code: 'flow_revision_mismatch',
              message: 'The flow changed since it was read.',
              current_revision: 4,
            },
          }),
          { status: 412, headers: { 'Content-Type': 'application/json' } }
        );
      });
    const routed = sinon.stub(Router, 'go');
    const element = document.createElement('flow-view') as any;
    element.isNew = false;
    element.flowId = 'flow-1';
    element.flow = { id: 'flow-1', name: 'Example flow', revision: 3 };

    const host = document.createElement('div');
    const root = host.attachShadow({ mode: 'open' });
    const form = document.createElement('div') as HTMLElement & {
      formError?: string;
      revisionConflict?: boolean;
    };
    root.appendChild(form);
    document.body.appendChild(host);
    const waited: Promise<unknown>[] = [];
    const markSaved = sinon.spy();
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
            flow: { name: 'Example flow', description: 'My unsaved edit' },
            markSaved,
            waitUntil: (work: Promise<unknown>) => waited.push(work),
          },
        })
      );
      await waited[0];
      expect(new Headers(sent[0].headers).get('If-Match')).to.equal('"3"');
      expect(form.revisionConflict).to.equal(true);
      expect(form.formError).to.include('changed after you opened it');
      // Nothing navigates away and the form is not marked saved, so the
      // typed edit stays on screen until the user chooses to reload.
      expect(routed.called).to.equal(false);
      expect(markSaved.called).to.equal(false);
    } finally {
      host.remove();
      routed.restore();
      fetchStub.restore();
    }
  });
});

describe('FlowView leave guard', () => {
  it('prevents route leave when the mounted form declines discard', async () => {
    const element = document.createElement('flow-view') as FlowView;
    const root = element.shadowRoot || element.attachShadow({ mode: 'open' });
    const form = document.createElement('preloop-flow-form') as any;
    root.append(form);
    const confirm = sinon.stub(form, 'confirmLeave').resolves(false);
    const prevent = sinon.stub().returns({ prevented: true });
    try {
      expect(
        await element.onBeforeLeave({} as any, { prevent } as any)
      ).to.deep.equal({ prevented: true });
      expect(prevent.calledOnce).to.equal(true);
      confirm.resolves(true);
      expect(
        await element.onBeforeLeave({} as any, { prevent } as any)
      ).to.equal(undefined);
      expect(prevent.calledOnce).to.equal(true);
    } finally {
      confirm.restore();
    }
  });
});

describe('FlowView invalid native results', () => {
  for (const validate of [true, false]) {
    it(`shows a saved configuration problem (validation ${validate})`, async () => {
      const element = document.createElement('flow-view') as any;
      element.flowReady = true;
      element.isNew = false;
      element.isEditing = false;
      element.initialized = true;
      element.flow = {
        id: 'flow-1',
        name: 'Review',
        agent_type: 'codex',
        agent_config: { native_result: true },
        result_schema: validate ? null : { type: 'object' },
        validate_result: validate,
      };
      document.body.appendChild(element);
      await element.updateComplete;
      try {
        const warning = element.shadowRoot.querySelector(
          '[data-native-result-error]'
        );
        expect(warning).not.to.equal(null);
        expect(warning.textContent).to.contain(
          'Native results require a result schema'
        );
      } finally {
        element.remove();
      }
    });
  }
});

describe('FlowView configuration warnings', () => {
  const WARNING = {
    code: 'openrouter_unpinned_structured_result',
    message:
      'Structured results on OpenRouter depend on the upstream provider that serves each request.',
    ai_model_id: 'model-1',
    docs_url:
      'https://docs.preloop.ai/guide/providers/openrouter/#structured-results-and-upstream-providers',
  };

  async function mount(warnings: unknown[]) {
    const element = document.createElement('flow-view') as any;
    element.flowReady = true;
    element.isNew = false;
    element.isEditing = false;
    element.initialized = true;
    element.flowId = 'flow-1';
    element.flow = {
      id: 'flow-1',
      name: 'Native review',
      agent_type: 'codex',
      config_warnings: warnings,
    };
    document.body.appendChild(element);
    await element.updateComplete;
    return element;
  }

  it('shows each server warning with its guide link, without blocking', async () => {
    const element = await mount([WARNING]);
    try {
      const alert = element.shadowRoot!.querySelector(
        '[data-testid="flow-config-warning"]'
      ) as HTMLElement;
      expect(alert).to.exist;
      expect(alert.getAttribute('variant')).to.equal('warning');
      expect(alert.textContent).to.contain('depend on the upstream provider');
      expect(alert.querySelector('a')!.getAttribute('href')).to.equal(
        WARNING.docs_url
      );
    } finally {
      element.remove();
    }
  });

  it('shows nothing when the flow has no warnings', async () => {
    const element = await mount([]);
    try {
      expect(
        element.shadowRoot!.querySelector('[data-testid="flow-config-warning"]')
      ).to.not.exist;
    } finally {
      element.remove();
    }
  });
});

describe('FlowView conversation start', () => {
  let element: any;
  let fetch: sinon.SinonStub;
  let popup: any;
  let open: sinon.SinonStub;
  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-token');
    resetTalkWindowsForTests();
    sinon.stub(window, 'innerWidth').value(1200);
    popup = {
      closed: false,
      focus: sinon.stub(),
      close: sinon.stub(),
      location: { replace: sinon.stub() },
    };
    open = sinon.stub(window, 'open').returns(popup);
    fetch = sinon.stub(window, 'fetch');
    element = document.createElement('flow-view');
    element.flowId = 'flow-example';
    element.flow = { name: 'Repository questions' };
    element.conversationFirstMessage = 'Inspect the README.';
  });
  afterEach(() => {
    sinon.restore();
    resetTalkWindowsForTests();
    localStorage.clear();
  });
  it('reuses the start identity after a lost response, and renews it after success', async () => {
    fetch.onFirstCall().rejects(new TypeError('Response lost'));
    fetch.callsFake(
      async (_url, init) =>
        new Response(JSON.stringify({ id: JSON.parse(init.body).request_id }))
    );
    await element.startConversation();
    await element.startConversation();
    const first = JSON.parse(fetch.firstCall.args[1].body);
    const retry = JSON.parse(fetch.secondCall.args[1].body);
    expect(retry).to.deep.equal(first);
    expect(retry.message).to.equal('Inspect the README.');
    await element.startConversation();
    expect(JSON.parse(fetch.thirdCall.args[1].body).request_id).not.to.equal(
      first.request_id
    );
  });
  it('scopes retry identity to the flow after navigation following a lost response', async () => {
    fetch.rejects(new TypeError('Response lost'));
    await element.startConversation();
    const first = JSON.parse(fetch.firstCall.args[1].body);
    element.flowId = 'another-flow';
    await element.startConversation();
    const next = JSON.parse(fetch.secondCall.args[1].body);
    expect(String(fetch.secondCall.args[0])).to.contain(
      '/flows/another-flow/conversations'
    );
    expect(next.request_id).not.to.equal(first.request_id);
    expect(next.message).to.equal(first.message);
    expect(Object.keys(next).sort()).to.deep.equal(['message', 'request_id']);
    await element.startConversation();
    expect(JSON.parse(fetch.thirdCall.args[1].body)).to.deep.equal(next);
  });
  it('reserves a blank popup synchronously and navigates only after the start succeeds', async () => {
    let respond!: (value: Response) => void;
    fetch.returns(
      new Promise<Response>((resolve) => {
        respond = resolve;
      })
    );
    const pending = element.startConversation();
    expect(open.calledOnce).to.equal(true);
    expect(open.firstCall.args[0]).to.equal('about:blank');
    expect(popup.location.replace.called).to.equal(false);
    await waitUntil(() => fetch.called);
    const id = JSON.parse(fetch.firstCall.args[1].body).request_id;
    respond(new Response(JSON.stringify({ id })));
    await pending;
    expect(popup.location.replace.firstCall.args[0]).to.equal(
      `/console/flow-conversations/${id}/talk?window=1`
    );
  });
  it('navigates on phones only after the conversation exists', async () => {
    sinon.restore();
    sinon.stub(window, 'innerWidth').value(390);
    open = sinon.stub(window, 'open');
    const navigate = sinon.stub(Router, 'go');
    let respond!: (value: Response) => void;
    fetch = sinon.stub(window, 'fetch').returns(
      new Promise<Response>((resolve) => {
        respond = resolve;
      })
    );
    const pending = element.startConversation();
    expect(navigate.called).to.equal(false);
    expect(open.called).to.equal(false);
    await waitUntil(() => fetch.called);
    const id = JSON.parse(fetch.firstCall.args[1].body).request_id;
    respond(new Response(JSON.stringify({ id })));
    await pending;
    expect(navigate.firstCall.args[0]).to.equal(
      `/console/flow-conversations/${id}/talk`
    );
  });
  it('assigns a new identity when the first message changes after failure', async () => {
    fetch.rejects(new TypeError('Response lost'));
    await element.startConversation();
    element.conversationFirstMessage = 'Inspect the tests instead.';
    await element.startConversation();
    const first = JSON.parse(fetch.firstCall.args[1].body);
    const changed = JSON.parse(fetch.secondCall.args[1].body);
    expect(changed.request_id).not.to.equal(first.request_id);
    expect(changed.message).to.equal('Inspect the tests instead.');
  });
  it('closes the reserved popup if starting fails', async () => {
    fetch.rejects(new TypeError('Response lost'));
    await element.startConversation();
    expect(popup.close.calledOnce).to.equal(true);
  });
});
