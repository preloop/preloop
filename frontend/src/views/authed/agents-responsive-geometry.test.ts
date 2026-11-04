import { expect, fixture, html, nextFrame, waitUntil } from '@open-wc/testing';
import { sendKeys, setViewport } from '@web/test-runner-commands';
import sinon from 'sinon';
import './agents-view.ts';
import type { AgentsView } from './agents-view';
import { loadShoelaceTokens } from '../../utils/test-shoelace-theme';

function makeAgent(id: string, display_name: string, agent_kind: string) {
  return {
    id,
    display_name,
    agent_kind,
    session_source_type: agent_kind,
    lifecycle_state: 'active',
    activity_status: 'idle',
    is_active_now: false,
    last_seen_at: '2026-11-04T10:00:00Z',
    total_requests: 123,
    estimated_cost: 0.42,
  };
}

async function waitForAgents(el: AgentsView): Promise<void> {
  await waitUntil(() =>
    Boolean(el.shadowRoot?.querySelector('.cards .agent-card'))
  );
  await el.updateComplete;
}

describe('Responsive agent cards', () => {
  let fetchStub: sinon.SinonStub;
  let agentItems: Array<Record<string, unknown>>;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    agentItems = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = new URL(String(input), window.location.origin);
      let body: unknown = [];
      if (url.pathname === '/api/v1/agents') {
        body = {
          items: agentItems,
          total: agentItems.length,
          limit: 50,
          offset: 0,
        };
      } else if (url.pathname.endsWith('/discovery-candidates')) {
        body = { items: [], total: 0, truncated: false };
      } else if (url.pathname.endsWith('/profile')) {
        body = { is_superuser: true };
      } else if (url.pathname.includes('gateway-usage/summary')) {
        body = {
          token_usage: { total_tokens: 0, input_tokens: 0, output_tokens: 0 },
          estimated_cost: 0,
          total_requests: 0,
          requests_by_day: [],
          top_models: [],
          top_agents: [],
          top_flows: [],
        };
      }
      return new Response(JSON.stringify(body), {
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  for (const width of [320, 360, 390, 640, 1440]) {
    it(`keeps responsive card identity and actions within their card at ${width}px`, async () => {
      await loadShoelaceTokens();
      localStorage.setItem('preloop.agents.view_mode', 'cards');
      agentItems = [
        {
          ...makeAgent(
            'agent-1',
            'releasevalidationorchestrator'.repeat(3),
            'claude_code'
          ),
          owner_username: 'longownername'.repeat(5),
          tags: {
            environment: 'europewestproductionprimary'.repeat(3),
            ['longtagkey'.repeat(4)]: 'platformoperations'.repeat(3),
          },
          control_state: 'plugin_connected',
          control_enabled: true,
          control_online: true,
          control_capabilities: ['send_text_prompt'],
        },
      ];
      const el = await fixture<AgentsView>(
        html`<agents-view style="width: ${width - 32}px"></agents-view>`
      );
      await waitForAgents(el);
      await nextFrame();
      const card =
        el.shadowRoot!.querySelector<HTMLElement>('.cards .agent-card')!;
      const bounds = card.getBoundingClientRect();
      expect(bounds.right).to.be.at.most(el.getBoundingClientRect().right + 1);
      for (const selector of [
        '.card-select',
        '.identity-stack',
        '.identity-badges sl-badge',
        'talk-button',
        '.card-actions',
      ]) {
        for (const item of card.querySelectorAll<HTMLElement>(selector)) {
          const box = item.getBoundingClientRect();
          expect(box.width, selector).to.be.greaterThan(0);
          expect(box.left, selector).to.be.at.least(bounds.left);
          expect(box.right, selector).to.be.at.most(bounds.right);
          expect(item.scrollWidth, selector).to.be.at.most(
            item.clientWidth + 1
          );
        }
      }
      const identity = card
        .querySelector('.identity-stack')!
        .getBoundingClientRect();
      const talk = card.querySelector('talk-button')!.getBoundingClientRect();
      const menu = card.querySelector('.card-actions')!.getBoundingClientRect();
      expect(talk.top).to.be.at.least(identity.bottom);
      expect(menu.left).to.be.at.least(talk.right);
      if (width === 390) {
        card.focus();
        await sendKeys({ press: 'Tab' });
        expect(el.shadowRoot!.activeElement?.tagName).to.equal(
          'LIST-SELECT-CHECKBOX'
        );
        await sendKeys({ press: 'Tab' });
        expect(el.shadowRoot!.activeElement).to.equal(
          card.querySelector('a.agent-name')
        );
        await sendKeys({ press: 'Tab' });
        expect(el.shadowRoot!.activeElement).to.equal(
          card.querySelector('talk-button sl-button')
        );
        await sendKeys({ press: 'Tab' });
        expect(el.shadowRoot!.activeElement).to.equal(
          card.querySelector('resource-actions')
        );
      }
    });
  }

  it('uses the full phone card width and separates tag icons from text', async () => {
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    await setViewport({ width: 390, height: 844 });
    try {
      await loadShoelaceTokens();
      localStorage.setItem('preloop.agents.view_mode', 'cards');
      agentItems = [
        {
          ...makeAgent('agent-1', 'Example agent', 'claude_code'),
          owner_username: 'jane',
        },
      ];
      const el = await fixture<AgentsView>(
        html`<agents-view style="width: 358px"></agents-view>`
      );
      await waitForAgents(el);
      await nextFrame();
      const card = el.shadowRoot!.querySelector('.cards .agent-card')!;
      const row = card.querySelector('.title-row')!.getBoundingClientRect();
      const menu = card.querySelector('.card-actions')!.getBoundingClientRect();
      expect(menu.right, 'actions use the available row width').to.be.closeTo(
        row.right,
        1
      );
      const owner = card.querySelector('sl-badge[title="Owner"]')!;
      const icon = owner.querySelector('sl-icon')!.getBoundingClientRect();
      const text = [...owner.childNodes].find(
        (node) =>
          node.nodeType === Node.TEXT_NODE && node.textContent?.includes('jane')
      )!;
      const start = text.textContent!.indexOf('jane');
      const range = document.createRange();
      range.setStart(text, start);
      range.setEnd(text, start + 1);
      expect(
        range.getBoundingClientRect().left - icon.right,
        'visible icon/label gap'
      ).to.be.at.least(3);
      el.remove();
    } finally {
      await setViewport(viewport);
    }
  });
});
