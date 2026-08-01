import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import '../../components/view-header';
import '../../components/tracker-list';
import '../../components/add-tracker-modal';
import '../../components/unlocked-tools-review-dialog';
import './trackers-view';
import type { TrackersView } from './trackers-view';
import type { UnlockedToolsReviewDialog } from '../../components/unlocked-tools-review-dialog';

describe('TrackersView', () => {
  let element: TrackersView;
  let fetchStub: sinon.SinonStub;

  function createFetchStub() {
    return sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = (init?.method || 'GET').toUpperCase();

        const json = (data: unknown) =>
          new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });

        if (url.includes('/api/v1/trackers') && method === 'GET') {
          return json([]);
        }
        if (url.includes('/api/v1/tools') && method === 'GET') {
          return json([
            {
              name: 'get_issue',
              description: 'Get an issue',
              source: 'builtin',
              source_id: null,
              is_enabled: true,
              is_supported: true,
              config_id: null,
              schema_tokens_estimate: 120,
            },
            {
              name: 'add_comment',
              description: 'Add a comment',
              source: 'builtin',
              source_id: null,
              is_enabled: true,
              is_supported: true,
              config_id: null,
              schema_tokens_estimate: 80,
            },
          ]);
        }

        return json({ detail: `Unhandled: ${method} ${url}` });
      });
  }

  beforeEach(async () => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');

    fetchStub = createFetchStub();
    element = await fixture(html`<trackers-view></trackers-view>`);
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
    sessionStorage.clear();
    // Reset URL
    window.history.replaceState({}, '', window.location.pathname);
  });

  it('renders the view with header', async () => {
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header).to.exist;
    const h1 = header?.shadowRoot?.querySelector('h1');
    expect(h1?.textContent?.trim()).to.equal('Trackers');
  });

  it('renders Add New Tracker button', async () => {
    await element.updateComplete;

    const addButton = element.shadowRoot?.querySelector(
      'sl-button[variant="primary"]'
    );
    expect(addButton).to.exist;
    expect(addButton?.textContent?.trim()).to.include('Add New Tracker');
  });

  it('renders tracker-list', async () => {
    await element.updateComplete;

    const trackerList = element.shadowRoot?.querySelector('tracker-list');
    expect(trackerList).to.exist;
  });

  it('opens add tracker modal when Add New Tracker button is clicked', async () => {
    await element.updateComplete;

    const addButton = element.shadowRoot?.querySelector(
      'sl-button[variant="primary"]'
    );
    expect(addButton).to.exist;
    addButton!.click();
    await element.updateComplete;

    const addModal = element.shadowRoot?.querySelector('add-tracker-modal');
    expect(addModal).to.exist;
  });

  it('fetches trackers on load', async () => {
    await waitUntil(() => fetchStub.called, 'Fetch was not called');

    const urls = fetchStub.getCalls().map((c) => String(c.args[0]));
    expect(urls.some((u) => u.includes('/api/v1/trackers'))).to.be.true;
  });

  it('opens unlock review dialog when tracker-added has unlocked tools', async () => {
    await element.updateComplete;
    const addButton = element.shadowRoot?.querySelector(
      'sl-button[variant="primary"]'
    );
    addButton!.click();
    await element.updateComplete;

    const addModal = element.shadowRoot?.querySelector('add-tracker-modal');
    expect(addModal).to.exist;

    addModal!.dispatchEvent(
      new CustomEvent('tracker-added', {
        detail: {
          tracker: {
            id: 't-1',
            unlocked_tool_names: ['get_issue', 'add_comment'],
          },
        },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    const review = element.shadowRoot?.querySelector(
      'unlocked-tools-review-dialog'
    ) as UnlockedToolsReviewDialog | null;
    expect(review).to.exist;
    expect(review!.open).to.be.true;
    expect(review!.toolNames).to.deep.equal(['get_issue', 'add_comment']);

    await waitUntil(
      () => review!.shadowRoot?.querySelector('.tool-row') !== null,
      'Review dialog rows did not load'
    );
    const tax = review!.shadowRoot?.querySelector('.context-tax');
    expect(tax?.textContent).to.include('~200');
  });

  it('does not open unlock review when unlocked_tool_names is empty', async () => {
    await element.updateComplete;
    element.shadowRoot?.querySelector('sl-button[variant="primary"]')!.click();
    await element.updateComplete;

    const addModal = element.shadowRoot?.querySelector('add-tracker-modal');
    addModal!.dispatchEvent(
      new CustomEvent('tracker-added', {
        detail: { tracker: { id: 't-1', unlocked_tool_names: [] } },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    const review = element.shadowRoot?.querySelector(
      'unlocked-tools-review-dialog'
    ) as UnlockedToolsReviewDialog;
    expect(review.open).to.be.false;
  });

  it('does not open unlock review when unlocked_tool_names is missing', async () => {
    await element.updateComplete;
    element.shadowRoot?.querySelector('sl-button[variant="primary"]')!.click();
    await element.updateComplete;

    const addModal = element.shadowRoot?.querySelector('add-tracker-modal');
    addModal!.dispatchEvent(
      new CustomEvent('tracker-added', {
        detail: { tracker: { id: 't-1' } },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    const review = element.shadowRoot?.querySelector(
      'unlocked-tools-review-dialog'
    ) as UnlockedToolsReviewDialog;
    expect(review.open).to.be.false;
  });

  it('queues unlock review until warnings modal is dismissed', async () => {
    await element.updateComplete;
    element.shadowRoot?.querySelector('sl-button[variant="primary"]')!.click();
    await element.updateComplete;

    const addModal = element.shadowRoot?.querySelector('add-tracker-modal');
    addModal!.dispatchEvent(
      new CustomEvent('tracker-added', {
        detail: {
          tracker: {
            id: 't-1',
            warnings: ['missing scope'],
            unlocked_tool_names: ['get_issue'],
          },
          hasWarnings: true,
        },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    let review = element.shadowRoot?.querySelector(
      'unlocked-tools-review-dialog'
    ) as UnlockedToolsReviewDialog;
    expect(review.open).to.be.false;
    // Add modal still present while warnings are shown
    expect(element.shadowRoot?.querySelector('add-tracker-modal')).to.exist;

    addModal!.dispatchEvent(
      new CustomEvent('close-modal', {
        detail: { success: true },
        bubbles: true,
        composed: true,
      })
    );
    await element.updateComplete;

    review = element.shadowRoot?.querySelector(
      'unlocked-tools-review-dialog'
    ) as UnlockedToolsReviewDialog;
    expect(review.open).to.be.true;
    expect(review.toolNames).to.deep.equal(['get_issue']);
  });
});
