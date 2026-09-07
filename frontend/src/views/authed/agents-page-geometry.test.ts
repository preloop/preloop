import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './agents-view.ts';
import './flows-view.ts';
import { invalidateApiCaches } from '../../api';
import { loadShoelaceTokens } from '../../utils/test-shoelace-theme';

/**
 * Agents sits in the same page box as the rest of the console.
 *
 * The shell centres one column and pays the side inset, so a view adds none
 * of its own (styles/console-styles.css, "The page box"). Agents used to
 * carry `.console-page` in every mode, which is right only for the canvas:
 * the canvas takes the whole window and has no shell padding to inherit. In
 * the default list it meant a second 2rem inset and a page 64px narrower
 * than Flows.
 */
/** One row each, so both pages draw a real list card rather than an empty state. */
const AGENT = {
  id: 'agent-1',
  runtime_session_id: 'runtime-session-agent-1',
  display_name: 'Claude Code Workspace',
  agent_kind: 'claude_code',
  session_source_type: 'claude_code',
  session_source_id: 'workspace-1',
  session_reference: 'session-1',
  lifecycle_state: 'active',
  activity_status: 'active_now',
  is_active_now: true,
  last_seen_at: '2026-09-06T10:00:00Z',
  total_requests: 3,
  estimated_cost: 0.42,
  latest_model_alias: 'openai/gpt-5',
};

const FLOW = {
  id: 'flow-1',
  name: 'Nightly sweep',
  is_enabled: true,
};

describe('Agents page geometry', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(async () => {
    await loadShoelaceTokens();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      let body: unknown = [];
      if (url.startsWith('/api/v1/agents')) {
        body = { items: [AGENT], total: 1, limit: 50, offset: 0 };
      } else if (url.includes('/api/v1/flows/presets')) {
        body = [];
      } else if (url.includes('/api/v1/flows/executions')) {
        body = [];
      } else if (url.startsWith('/api/v1/flows')) {
        body = [FLOW];
      } else if (url.includes('gateway-usage/summary')) {
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
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  /**
   * Mounts a view in a column of the given width and reports where its title
   * and its list card sit relative to the view's own left edge.
   */
  async function pageBox(
    tag: 'agents-view' | 'flows-view',
    width: number
  ): Promise<{ titleLeft: number; cardLeft: number; cardWidth: number }> {
    const wrapper = (await fixture(html`
      <div style="width: ${width}px; display: flex; flex-direction: column">
        ${document.createElement(tag)}
      </div>
    `)) as HTMLElement;
    const view = wrapper.firstElementChild as HTMLElement & {
      updateComplete?: Promise<unknown>;
    };
    view.style.width = '100%';
    await view.updateComplete;

    let title: HTMLElement | null = null;
    let card: HTMLElement | null = null;
    await waitUntil(() => {
      title =
        (view.shadowRoot
          ?.querySelector('view-header')
          ?.shadowRoot?.querySelector('h1') as HTMLElement | null) ?? null;
      card = view.shadowRoot?.querySelector(
        'sl-card.table-card'
      ) as HTMLElement | null;
      return title !== null && card !== null;
    }, `${tag} never drew its title and list card`);

    const origin = view.getBoundingClientRect().left;
    const titleBox = (title as unknown as HTMLElement).getBoundingClientRect();
    const cardBox = (card as unknown as HTMLElement).getBoundingClientRect();
    return {
      titleLeft: titleBox.left - origin,
      cardLeft: cardBox.left - origin,
      cardWidth: cardBox.width,
    };
  }

  it('keeps the page box on the canvas, which the shell hands the full window', async () => {
    localStorage.setItem('preloop.agents.view_mode', 'canvas');
    const wrapper = (await fixture(html`
      <div style="width: 1440px; display: flex; flex-direction: column">
        <agents-view></agents-view>
      </div>
    `)) as HTMLElement;
    const view = wrapper.firstElementChild as HTMLElement & {
      updateComplete: Promise<unknown>;
    };
    await view.updateComplete;
    const band = view.shadowRoot!.querySelector('.content-bounds')!;
    // Full bleed means no shell padding to inherit, so the canvas page draws
    // the box itself: the 1280px column centred in 1440 (80px each side)
    // plus the 2rem side inset.
    expect(band.classList.contains('console-page')).to.equal(true);
    const header = band.querySelector('view-header')!;
    expect(
      Math.round(
        header.getBoundingClientRect().left - view.getBoundingClientRect().left
      ),
      'canvas header inset'
    ).to.equal(112);
  });

  for (const width of [1440, 1280]) {
    it(`puts the agents header and list on the same x and width as flows at ${width}`, async () => {
      const flows = await pageBox('flows-view', width);
      const agents = await pageBox('agents-view', width);

      expect(
        Math.abs(agents.titleLeft - flows.titleLeft),
        `title x: agents ${agents.titleLeft}, flows ${flows.titleLeft}`
      ).to.be.at.most(1);
      expect(
        Math.abs(agents.cardLeft - flows.cardLeft),
        `list card x: agents ${agents.cardLeft}, flows ${flows.cardLeft}`
      ).to.be.at.most(1);
      expect(
        Math.abs(agents.cardWidth - flows.cardWidth),
        `list card width: agents ${agents.cardWidth}, flows ${flows.cardWidth}`
      ).to.be.at.most(1);
      // The shell pays the inset, so a view adds none of its own.
      expect(agents.titleLeft, 'agents adds no inset of its own').to.equal(0);
    });
  }
});
