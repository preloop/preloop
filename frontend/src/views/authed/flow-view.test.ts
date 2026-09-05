import { expect } from '@open-wc/testing';

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

  it('uses the list verbs in the header actions', async () => {
    // The header said "Edit Flow / Disable / Test Run" for the three commands
    // the list kebab calls Edit, Pause and Run now, and "Test Run" implied a
    // rehearsal of a run that spends real money.
    const element = await renderDetail();
    try {
      const labels = element
        .getFlowActions()
        .map((action: any) => action.label);
      expect(labels).to.eql(['Edit', 'Pause', 'Run now']);

      const paused = await renderDetail({ is_enabled: false });
      try {
        expect(
          paused.getFlowActions().map((action: any) => action.label)
        ).to.eql(['Edit', 'Resume', 'Run now']);
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
