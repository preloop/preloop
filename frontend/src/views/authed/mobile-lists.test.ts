import { expect, fixture, html, nextFrame, waitUntil } from '@open-wc/testing';
import { sendKeys, setViewport } from '@web/test-runner-commands';
import sinon from 'sinon';
import './tools-view';
import './flow-executions-view';
import type { ToolsView } from './tools-view';
import type { FlowExecutionsView } from './flow-executions-view';
import type { ResourceActions } from '../../components/resource-actions';
import type SlButton from '@shoelace-style/shoelace/dist/components/button/button.js';
import type SlSelect from '@shoelace-style/shoelace/dist/components/select/select.js';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';
import { loadShoelaceTokens } from '../../utils/test-shoelace-theme';

const tools = [true, false].map((enabled) => ({
  name: enabled ? 'search_documents' : 'update_issue',
  description: 'Example project tool',
  source: 'builtin',
  source_name: 'Preloop',
  schema: {},
  is_enabled: enabled,
  is_supported: true,
}));
const executions = ['RUNNING', 'FAILED', 'WAITING_FOR_HUMAN'].map(
  (status, i) => ({
    id: `execution-${i}`,
    flow_id: 'flow-example',
    flow_name: 'documentationreview'.repeat(3),
    trigger_subject: 'Review documentation changes and verify the examples',
    status,
    start_time: '2026-11-04T10:00:00Z',
    tool_calls_count: 3,
    estimated_cost: 0.04,
  })
);

describe('Mobile tools and execution lists', () => {
  beforeEach(async () => {
    localStorage.clear();
    localStorage.setItem('accessToken', 'test-token');
    window.history.replaceState({}, '', '/');
    await loadShoelaceTokens();
    sinon.stub(unifiedWebSocketManager, 'subscribe').returns(() => {});
    sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = new URL(String(input), window.location.origin);
      let body: unknown = [];
      if (url.pathname.endsWith('/tools/summary')) body = tools;
      if (url.pathname.endsWith('/features')) body = { features: {} };
      if (url.pathname.endsWith('/profile')) body = { is_superuser: true };
      if (url.pathname.endsWith('/flows/executions')) body = executions;
      return new Response(JSON.stringify(body), {
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(async () => {
    sinon.restore();
    localStorage.clear();
    await setViewport({ width: 1024, height: 768 });
  });

  for (const width of [360, 390, 640, 1440]) {
    it(`keeps tool search and results visible with discoverable filters at ${width}px`, async () => {
      await setViewport({ width, height: 844 });
      const el = await fixture<ToolsView>(html`<tools-view></tools-view>`);
      await waitUntil(() =>
        Boolean(el.shadowRoot!.querySelector('tools-editor-component'))
      );
      await nextFrame();
      const root = el.shadowRoot!;
      const toggle =
        root.querySelector<HTMLButtonElement>('.mcp-filter-toggle')!;
      const controls = root.querySelector<HTMLElement>('.mcp-filter-controls')!;
      const editor = root.querySelector<HTMLElement>('tools-editor-component')!;
      if (width <= 640) {
        expect(getComputedStyle(controls).display).to.equal('none');
        expect(editor.getBoundingClientRect().top).to.be.lessThan(650);
        expect(toggle.getAttribute('aria-expanded')).to.equal('false');
        expect(toggle.getBoundingClientRect().height).to.be.at.least(44);
        toggle.focus();
        await sendKeys({ press: 'Enter' });
        await el.updateComplete;
        expect(toggle.getAttribute('aria-expanded')).to.equal('true');
        for (const select of controls.querySelectorAll<SlSelect>('sl-select')) {
          const bounds = select.getBoundingClientRect();
          expect(bounds.width).to.be.greaterThan(100);
          expect(bounds.right).to.be.at.most(width);
          expect(select.label).to.not.equal('');
        }
      } else {
        expect(getComputedStyle(toggle).display).to.equal('none');
        expect(
          root.querySelector('.status-filter')!.getBoundingClientRect().width
        ).to.be.greaterThan(0);
      }
      const status = root.querySelector<SlSelect>('.status-filter')!;
      status.value = 'disabled';
      status.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
      await el.updateComplete;
      expect(
        (editor as HTMLElement & { tools: typeof tools }).tools.map(
          (t) => t.name
        )
      ).to.deep.equal(['update_issue']);
      expect(toggle.textContent).to.contain('1 active');
      if (width <= 640) {
        toggle.click();
        await el.updateComplete;
        expect(getComputedStyle(controls).display).to.equal('none');
        expect(toggle.textContent).to.contain('1 active');
        toggle.click();
        await el.updateComplete;
      }
      status.value = '';
      status.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
      await el.updateComplete;
      expect(
        (editor as HTMLElement & { tools: typeof tools }).tools
      ).to.have.length(2);
      expect(toggle.textContent).to.not.contain('active');
    });

    it(`keeps execution status and actions reachable at ${width}px`, async () => {
      await setViewport({ width, height: 844 });
      const el = await fixture<FlowExecutionsView>(
        html`<flow-executions-view></flow-executions-view>`
      );
      await waitUntil(
        () =>
          el.shadowRoot!.querySelectorAll(
            width <= 640 ? '.execution-summary' : 'tbody tr'
          ).length === 3
      );
      await nextFrame();
      const root = el.shadowRoot!;
      const cards = [
        ...root.querySelectorAll<HTMLElement>('.execution-summary'),
      ];
      const wrapper = root.querySelector<HTMLElement>('.table-wrapper')!;
      expect(root.querySelectorAll('resource-actions')).to.have.length(3);
      if (width <= 640) {
        expect(getComputedStyle(wrapper).display).to.equal('none');
        expect(root.querySelector('table')).to.not.exist;
        const disclosure = root.querySelector<HTMLButtonElement>(
          '.mobile-table-toggle'
        )!;
        expect(disclosure.getBoundingClientRect().height).to.be.at.least(44);
        disclosure.focus();
        await sendKeys({ press: 'Tab' });
        expect(root.activeElement).to.equal(
          cards[0].querySelector('.row-link')
        );
        for (const card of cards) {
          for (const selector of [
            '.row-link',
            '.status-cell',
            'resource-actions',
            '.execution-subject',
          ]) {
            const item = card.querySelector<HTMLElement>(selector)!;
            const bounds = item.getBoundingClientRect();
            expect(bounds.width, selector).to.be.greaterThan(0);
            expect(bounds.left, selector).to.be.at.least(0);
            expect(bounds.right, selector).to.be.at.most(width);
          }
          const link = card.querySelector<HTMLAnchorElement>('.row-link')!;
          expect(link.scrollWidth).to.be.at.most(link.clientWidth + 1);
          link.focus();
          expect(root.activeElement).to.equal(link);
        }
        expect(cards[0].textContent).to.contain('Running');
        expect(cards[1].textContent).to.contain('Failed');
        expect(cards[2].querySelector('[data-testid="waiting-chip"]')).to.exist;
        const running =
          cards[0].querySelector<ResourceActions>('resource-actions')!;
        const failed =
          cards[1].querySelector<ResourceActions>('resource-actions')!;
        expect(running.actions.map((a) => a.label)).to.include('Cancel run');
        expect(failed.actions.map((a) => a.label)).to.include('Retry run');
        expect(running.actions.find((a) => a.id === 'open')?.href).to.contain(
          'execution-0'
        );
        const trigger = running.shadowRoot!.querySelector<SlButton>(
          'sl-button[slot="trigger"]'
        )!;
        expect(trigger.getAttribute('aria-label')).to.equal('Resource actions');
        trigger.focus();
        await sendKeys({ press: 'Enter' });
        await waitUntil(() =>
          Boolean(running.shadowRoot!.querySelector('sl-dropdown')?.open)
        );
        expect(running.shadowRoot!.textContent).to.contain('Cancel run');
        const cancel = sinon
          .stub(
            el as unknown as {
              cancelExecution: (execution: unknown) => Promise<void>;
            },
            'cancelExecution'
          )
          .resolves();
        const cancelItem = [
          ...running.shadowRoot!.querySelectorAll<HTMLElement>('sl-menu-item'),
        ].find((item) => item.textContent?.includes('Cancel run'))!;
        cancelItem.click();
        expect(cancel.calledOnce).to.equal(true);
        expect(cancel.firstCall.args[0]).to.include({ id: 'execution-0' });
        el.handleWebSocketMessage({
          type: 'status_update',
          execution_id: 'execution-0',
          payload: { status: 'SUCCEEDED' },
        });
        await el.updateComplete;
        await running.updateComplete;
        expect(cards[0].textContent).to.contain('Succeeded');
        expect(running.actions.map((a) => a.label)).to.not.include(
          'Cancel run'
        );
        await sendKeys({ press: 'Escape' });
        const toggle = root.querySelector<HTMLButtonElement>(
          '.mobile-table-toggle'
        )!;
        toggle.focus();
        await sendKeys({ press: 'Enter' });
        await el.updateComplete;
        expect(getComputedStyle(wrapper).display).to.not.equal('none');
        expect(toggle.getAttribute('aria-expanded')).to.equal('true');
        expect(root.querySelector('.execution-summaries')).to.not.exist;
        expect(root.querySelectorAll('resource-actions')).to.have.length(3);
        await sendKeys({ press: 'Tab' });
        expect(wrapper.contains(root.activeElement)).to.equal(true);
        expect(root.querySelector('column-picker')).to.exist;
        expect(root.querySelectorAll('tbody tr')).to.have.length(3);
      } else {
        expect(root.querySelector('.execution-summaries')).to.not.exist;
        expect(getComputedStyle(wrapper).display).to.not.equal('none');
        await setViewport({ width: 390, height: 844 });
        await waitUntil(
          () => root.querySelectorAll('.execution-summary').length === 3
        );
        expect(root.querySelector('table')).to.not.exist;
        expect(root.querySelectorAll('resource-actions')).to.have.length(3);
        await setViewport({ width: 1440, height: 844 });
        await waitUntil(() => root.querySelectorAll('tbody tr').length === 3);
        expect(root.querySelector('.execution-summaries')).to.not.exist;
        expect(root.querySelectorAll('resource-actions')).to.have.length(3);
      }
    });
  }
});
