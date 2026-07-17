import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './tracker-list';
import type { TrackerList } from './tracker-list';
import type { Tracker } from './tracker-item';

describe('TrackerList', () => {
  let fetchStub: sinon.SinonStub;

  const mockTrackers: Tracker[] = [
    {
      id: 'tracker-1',
      name: 'Jira Production',
      tracker_type: 'jira',
      created: '2024-01-01T00:00:00Z',
      is_valid: true,
    },
    {
      id: 'tracker-2',
      name: 'GitHub Repos',
      tracker_type: 'github',
      created: '2024-01-02T00:00:00Z',
      is_valid: true,
    },
  ];

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  it('renders loading state initially', async () => {
    let resolveFetch!: (value: Response) => void;
    const fetchPromise = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    fetchStub.returns(fetchPromise);

    const el = (await fixture(
      html`<tracker-list></tracker-list>`
    )) as TrackerList;

    // Check spinner while fetch is still pending
    await el.updateComplete;
    const spinner = el.shadowRoot?.querySelector('sl-spinner');
    expect(spinner).to.exist;

    resolveFetch(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    await el.updateComplete;
  });

  it('renders tracker items when data is loaded', async () => {
    fetchStub.resolves(
      new Response(JSON.stringify(mockTrackers), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const el = (await fixture(
      html`<tracker-list></tracker-list>`
    )) as TrackerList;

    await waitUntil(
      () => el.shadowRoot?.querySelector('.tracker-grid') !== null,
      'Tracker grid did not render'
    );

    const trackerItems = el.shadowRoot?.querySelectorAll('tracker-item');
    expect(trackerItems).to.have.lengthOf(2);
  });

  it('renders an informative empty state when no trackers', async () => {
    fetchStub.resolves(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );

    const el = (await fixture(
      html`<tracker-list></tracker-list>`
    )) as TrackerList;

    await waitUntil(
      () => el.shadowRoot?.querySelector('.empty-state') !== null,
      'Empty state did not render'
    );

    const trackerItems = el.shadowRoot?.querySelectorAll('tracker-item');
    expect(trackerItems).to.have.lengthOf(0);

    const emptyState = el.shadowRoot?.querySelector('.empty-state');
    expect(emptyState?.textContent).to.contain('No trackers connected.');
    expect(emptyState?.textContent).to.contain(
      'Connect GitHub, GitLab, or Jira'
    );

    // The CTA asks the parent view to open the add-tracker form.
    const addRequested = new Promise<boolean>((resolve) => {
      el.addEventListener('tracker-add-request', () => resolve(true), {
        once: true,
      });
    });
    const cta = emptyState?.querySelector('sl-button') as HTMLElement;
    expect(cta).to.exist;
    expect(cta.textContent).to.contain('Add New Tracker');
    cta.click();
    expect(await addRequested).to.equal(true);
  });

  it('renders error state when fetch fails', async () => {
    fetchStub.rejects(new Error('Failed to fetch trackers'));

    const el = (await fixture(
      html`<tracker-list></tracker-list>`
    )) as TrackerList;

    await waitUntil(
      () => el.shadowRoot?.querySelector('sl-alert[variant="danger"]') !== null,
      'Error alert did not appear'
    );

    const alert = el.shadowRoot?.querySelector('sl-alert');
    expect(alert).to.exist;
    expect(alert?.textContent).to.include('Failed to fetch trackers');
  });
});
