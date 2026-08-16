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
