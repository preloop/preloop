import { fixture, html, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './flow-execution-view';
import type { FlowExecutionView } from './flow-execution-view';

/**
 * A run parked on a human decision, on the execution page.
 *
 * The staging failure this comes from: a question went to a human, the
 * platform held a container for five minutes, then reported FAILED
 * (cra_result_missing) 21 seconds before the human answered. A parked run is
 * neither running nor failed, and the page has to say so in one line: who it
 * is waiting for, since when, and when the window closes.
 */
describe('FlowExecutionView, parked on a human decision', () => {
  let fetchStub: sinon.SinonStub;

  const parkedFixture = () => {
    const now = Date.now();
    return {
      id: 'exec-parked',
      flow_id: 'flow-1',
      flow_name: 'Release security audit',
      status: 'WAITING_FOR_HUMAN',
      start_time: new Date(now - 5 * 60 * 1000).toISOString(),
      end_time: null,
      mcp_usage_logs: [],
      park: {
        request_id: 'req-1',
        since: new Date(now - 2 * 60 * 1000).toISOString(),
        expires_at: new Date(now + 60 * 60 * 1000).toISOString(),
        waiting_for: 'Security approvers',
        tool_name: 'ask_user',
        question: 'Waive CVE-2026-1234 for 90 days?',
      },
    };
  };
  let PARKED: ReturnType<typeof parkedFixture>;

  beforeEach(() => {
    PARKED = parkedFixture();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/flows/executions/exec-parked/logs')) {
        return new Response(JSON.stringify({ logs: [] }), { status: 200 });
      }
      if (url.includes('/flows/executions/exec-parked')) {
        return new Response(JSON.stringify(PARKED), { status: 200 });
      }
      if (url.includes('/flows/flow-1')) {
        return new Response(
          JSON.stringify({ id: 'flow-1', name: 'Release security audit' }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify({ logs: [], items: [] }), {
        status: 200,
      });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  async function loadParked(): Promise<FlowExecutionView> {
    const element = (await fixture(
      html`<flow-execution-view></flow-execution-view>`
    )) as FlowExecutionView;
    element.executionId = 'exec-parked';
    await element.updateComplete;
    await waitUntil(
      () => (element as any).execution?.id === 'exec-parked',
      'Parked execution did not load'
    );
    await element.updateComplete;
    return element;
  }

  it('names who the run is waiting for and when the window closes', async () => {
    const element = await loadParked();
    const line = element.shadowRoot?.querySelector(
      '[data-testid="waiting-line"]'
    );

    expect(line, 'waiting line is rendered').to.exist;
    const text = line?.textContent || '';
    expect(text).to.contain('Waiting for Security approvers');
    expect(text).to.contain('since');
    expect(text).to.contain('expires in');
  });

  it('shows the question the human is being asked', async () => {
    const element = await loadParked();
    const text =
      element.shadowRoot?.querySelector('[data-testid="waiting-line"]')
        ?.textContent || '';

    expect(text).to.contain('Waive CVE-2026-1234 for 90 days?');
  });

  it('presents the status as waiting, not as running or failed', async () => {
    const element = await loadParked();
    const badge = element.shadowRoot?.querySelector('.status-pill sl-badge');

    expect(badge?.textContent?.trim()).to.equal('Waiting for human');
    expect(badge?.getAttribute('variant')).to.equal('warning');
    expect(
      element.shadowRoot?.querySelector('.status-pill .status-dot'),
      'a parked run shows no live dot'
    ).to.not.exist;
  });

  it('shows no error line: waiting is not failing', async () => {
    const element = await loadParked();

    expect(element.shadowRoot?.querySelector('[data-testid="error-line"]')).to
      .not.exist;
  });

  it('renders nothing extra for a run that is not parked', async () => {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.includes('/flows/executions/exec-parked/logs')) {
        return new Response(JSON.stringify({ logs: [] }), { status: 200 });
      }
      if (url.includes('/flows/executions/exec-parked')) {
        return new Response(
          JSON.stringify({ ...PARKED, status: 'RUNNING', park: null }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify({ logs: [], items: [] }), {
        status: 200,
      });
    });
    const element = await loadParked();

    expect(element.shadowRoot?.querySelector('[data-testid="waiting-line"]')).to
      .not.exist;
  });
});
