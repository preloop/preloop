import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header.ts';
import './flows-view';
import './approvals-view';
import './settings/account-view';
import './audit-view';
import './settings/user-management-view';
import { invalidateApiCaches } from '../../api';
import { loadShoelaceTokens } from '../../utils/test-shoelace-theme';
import { unifiedWebSocketManager } from '../../services/unified-websocket-manager';

/**
 * The page box (styles/console-styles.css, "The page box").
 *
 * Every console page starts its content on the same x. The shell centres one
 * column and pays the side inset, so a view adds none of its own: a `:host`
 * padding or a narrower `max-width` with `margin: 0 auto` is what used to move
 * the title of Approvals two rem in from the title of Flows.
 *
 * The test measures the inset a view adds by itself, which is the part the
 * shell cannot fix: the distance from the view's own left edge to the left
 * edge of its title.
 *
 * The list is deliberately mixed: Flows, Approvals and Settings account were
 * already flush with the shell, Audit (`:host { padding: 1.5rem; max-width:
 * 1200px }`) and Users (`:host { padding: 2rem }`) were not. Without a
 * repaired page in here the test passes on a tree where every override is
 * back, which is the only failure it exists to catch.
 */
describe('Console page width', () => {
  let fetchStub: sinon.SinonStub;
  let wsSubscribeStub: sinon.SinonStub;
  let wsConnectStub: sinon.SinonStub;

  const PAGES = [
    'flows-view',
    'approvals-view',
    'account-view',
    'audit-view',
    'user-management-view',
  ] as const;

  beforeEach(async () => {
    await loadShoelaceTokens();
    // Audit opens a live feed on connect; the geometry does not depend on it.
    wsSubscribeStub = sinon
      .stub(unifiedWebSocketManager, 'subscribe')
      .callsFake(() => () => undefined);
    wsConnectStub = sinon
      .stub(unifiedWebSocketManager, 'connect')
      .resolves(undefined as never);
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      // Every page here lists something, and an empty list still draws the
      // title, which is all this test measures.
      let body: unknown = [];
      if (url.includes('/api/v1/features')) {
        // Users draws its title only where user management is on.
        body = { features: { user_management: true }, limits: {} };
      } else if (url.includes('/api/v1/account')) {
        body = { id: 'acc-1', organization_name: 'Acme' };
      } else if (url.includes('/api/v1/kill-switch')) {
        body = { active: false, scopes: [] };
      } else if (url.includes('/api/v1/audit-logs/grouped')) {
        body = { groups: [], total: 0 };
      } else if (url.includes('/api/v1/users')) {
        body = { users: [], total: 0 };
      } else if (url.includes('/api/v1/roles')) {
        body = { roles: [] };
      }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  });

  afterEach(() => {
    fetchStub?.restore();
    wsSubscribeStub?.restore();
    wsConnectStub?.restore();
    localStorage.clear();
    invalidateApiCaches();
  });

  /** Renders one page at a given viewport column width and returns the inset
   *  between the view's left edge and the left edge of its title. */
  async function contentInset(tag: string, width: number): Promise<number> {
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

    // A page titles itself either with `view-header` or, on the older
    // settings pages, with its own `h1`. Both are the first content edge.
    let title: HTMLElement | null = null;
    await waitUntil(() => {
      const header = view.shadowRoot?.querySelector('view-header');
      title = (header?.shadowRoot?.querySelector('h1') ??
        view.shadowRoot?.querySelector('h1') ??
        null) as HTMLElement | null;
      return title !== null;
    }, `${tag} never drew its title`);

    return (
      (title as unknown as HTMLElement).getBoundingClientRect().left -
      view.getBoundingClientRect().left
    );
  }

  for (const width of [1440, 1024]) {
    it(`starts every page on the same x at ${width}`, async () => {
      const insets: Record<string, number> = {};
      for (const tag of PAGES) {
        insets[tag] = await contentInset(tag, width);
      }

      const measured = Object.values(insets);
      expect(
        new Set(measured).size,
        `pages disagree on where content starts: ${JSON.stringify(insets)}`
      ).to.equal(1);
      // And the agreed number is zero: the page box is the shell's to pay,
      // so a view that pads itself is padding twice.
      expect(measured[0]).to.equal(0);
    });
  }
});
