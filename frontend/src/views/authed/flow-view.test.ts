import { expect } from '@open-wc/testing';

import './flow-view';
import type { FlowView } from './flow-view';

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
    expect(element.getTriggerSummary()).to.equal('Webhook');
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
    expect(element.getTriggerSummary()).to.equal(
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
    expect(element.getTriggerSummary()).to.equal('Schedule');
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
      expect(headers).to.eql([
        'Subject',
        'Status',
        'Started',
        'Duration',
        'Actions',
      ]);

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
