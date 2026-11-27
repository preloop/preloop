import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import './flow-execution-view';
import type { FlowExecutionView } from './flow-execution-view';

describe('execution timeline expansion', () => {
  afterEach(() => sinon.restore());

  it('retains tool and log-group choices when earlier entries are inserted', async () => {
    sinon.stub(window, 'fetch').callsFake(async () => new Response('{}'));
    const view = await fixture<FlowExecutionView>(
      html`<flow-execution-view></flow-execution-view>`
    );
    const state = view as any;
    sinon.stub(state, 'fetchExecution').resolves();
    state.executionId = 'example-run';
    state.isLoading = false;
    state.execution = {
      id: 'example-run',
      flow_id: 'example-flow',
      status: 'SUCCEEDED',
      start_time: '2026-01-01T00:00:00Z',
      mcp_usage_logs: [
        {
          id: 'tool-1',
          timestamp: '2026-01-01T00:00:03Z',
          tool_name: 'example_tool',
          server_name: 'example',
          status: 'pending',
        },
      ],
    };
    const log = (id: string, second: number) => ({
      log_id: id,
      execution_id: 'example-run',
      type: 'agent_log_line',
      timestamp: `2026-01-01T00:00:0${second}Z`,
      payload: { line: id },
    });
    state.logs = [log('line-1', 1)];
    await view.updateComplete;
    const group = () =>
      view.shadowRoot!.querySelector<HTMLButtonElement>('.log-group-toggle')!;
    group().click();
    await view.updateComplete;
    const tool = () =>
      view.shadowRoot!.querySelector('.timeline-details') as any;
    await tool().updateComplete;
    tool().open = true;
    await new Promise(requestAnimationFrame);
    await view.updateComplete;
    state.logs = [log('line-0', 0), log('line-1', 1), log('line-2', 2)];
    state.execution = {
      ...state.execution,
      mcp_usage_logs: [
        { ...state.execution.mcp_usage_logs[0], status: 'success' },
      ],
    };
    await view.updateComplete;
    expect(group().getAttribute('aria-expanded')).to.equal('true');
    expect(tool().open).to.equal(true);
    // Remove the rows entirely, like a virtual window leaving this range.
    const logs = state.logs;
    const execution = state.execution;
    state.logs = [];
    state.execution = { ...execution, mcp_usage_logs: [] };
    await view.updateComplete;
    state.logs = structuredClone(logs);
    state.execution = structuredClone(execution);
    await view.updateComplete;
    expect(group().getAttribute('aria-expanded')).to.equal('true');
    expect(tool().open).to.equal(true);
  });

  it('keeps an expanded call through refresh, status, append and window removal', async () => {
    sinon.stub(window, 'fetch').callsFake(async () => new Response('{}'));
    const view = await fixture<FlowExecutionView>(
      html`<flow-execution-view></flow-execution-view>`
    );
    const state = view as any;
    sinon.stub(state, 'fetchExecution').resolves();
    sinon.stub(state, 'handleGatewayEventExpand').resolves();
    state.executionId = 'example-run';
    sinon.stub(state, 'scheduleRealtimeRecovery');
    state.isLoading = false;
    state.execution = {
      id: 'example-run',
      flow_id: 'example-flow',
      status: 'RUNNING',
      start_time: '2026-01-01T00:00:00Z',
    };
    const call = (id: string, second: number) => ({
      id,
      execution_id: 'example-run',
      type: 'model_gateway_call',
      timestamp: `2026-01-01T00:00:0${second}Z`,
      payload: { model_alias: 'example-model', api_usage_id: id },
    });
    state.gatewayEvents = [call('call-1', 1)];
    await view.updateComplete;
    state.timelineRowsShown = 2;
    await view.updateComplete;
    const entry = () =>
      [...view.shadowRoot!.querySelectorAll('preloop-gateway-event')].find(
        (el: any) => el.event.id === 'call-1'
      ) as any;
    await entry().updateComplete;
    const details = entry().shadowRoot.querySelector('sl-details');
    await details.updateComplete;
    details.open = true;
    await waitUntil(() => entry().expanded);
    await view.updateComplete;
    expect(entry().expanded).to.equal(true);
    state.gatewayEvents = [call('call-1', 1)];
    state.requestUpdate();
    await view.updateComplete;
    expect(entry().expanded).to.equal(true);
    state.gatewayEvents = [call('call-1', 1), call('call-2', 2)];
    state.execution = {
      ...state.execution,
      status: 'SUCCEEDED',
      end_time: '2026-01-01T00:00:03Z',
    };
    await view.updateComplete;
    expect(entry()).to.equal(undefined);
    state.timelineRowsShown = 10;
    await view.updateComplete;
    await entry().updateComplete;
    expect(entry().expanded).to.equal(true);
    expect(entry().shadowRoot.querySelector('sl-details').open).to.equal(true);
    const restored = entry().shadowRoot.querySelector('sl-details');
    await restored.updateComplete;
    restored.open = false;
    await waitUntil(() => !entry().expanded);
    state.gatewayEvents = [
      call('call-0', 0),
      call('call-1', 1),
      call('call-2', 2),
    ];
    await view.updateComplete;
    expect(entry().expanded).to.equal(false);
  });
});
