import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './tracker-list';
import {
  filterTrackers,
  trackerKindLabel,
  trackerLastCheckedAt,
  trackerProjectsCount,
  type TrackerList,
} from './tracker-list';
import type { Tracker } from './tracker-item';

describe('filterTrackers', () => {
  const trackers: Tracker[] = [
    {
      id: 'tracker-1',
      name: 'Jira Production',
      tracker_type: 'jira',
      created: '2024-01-01T00:00:00Z',
      is_valid: true,
      url: 'https://jira.example.com',
    },
    {
      id: 'tracker-2',
      name: 'GitHub Repos',
      tracker_type: 'github',
      created: '2024-01-02T00:00:00Z',
      is_valid: true,
      url: 'https://github.com/example',
    },
  ];

  it('filters by name, kind, and url', () => {
    expect(filterTrackers(trackers, 'Jira', '').map((t) => t.id)).to.deep.equal(
      ['tracker-1']
    );
    expect(
      filterTrackers(trackers, 'github', '').map((t) => t.id)
    ).to.deep.equal(['tracker-2']);
    expect(
      filterTrackers(trackers, 'jira.example.com', '').map((t) => t.id)
    ).to.deep.equal(['tracker-1']);
  });

  it('filters by tracker kind', () => {
    expect(
      filterTrackers(trackers, '', 'github').map((t) => t.id)
    ).to.deep.equal(['tracker-2']);
  });

  it('labels known tracker kinds', () => {
    expect(trackerKindLabel('github')).to.equal('GitHub');
    expect(trackerKindLabel('gitlab')).to.equal('GitLab');
    expect(trackerKindLabel('jira')).to.equal('Jira');
  });

  it('counts included projects from scope rules', () => {
    expect(
      trackerProjectsCount({
        ...trackers[0],
        scope_rules: [
          {
            scope_type: 'PROJECT',
            rule_type: 'INCLUDE',
            identifier: 'ONE',
          },
          {
            scope_type: 'PROJECT',
            rule_type: 'INCLUDE',
            identifier: 'TWO',
          },
        ],
      })
    ).to.equal(2);
    expect(trackerProjectsCount(trackers[0])).to.equal('all');
    expect(
      trackerProjectsCount({
        ...trackers[0],
        scope_rules: [
          {
            scope_type: 'ORGANIZATION',
            rule_type: 'INCLUDE',
            identifier: 'ORG',
          },
        ],
      })
    ).to.equal('all');
  });

  it('uses last_validation only for last checked', () => {
    expect(trackerLastCheckedAt(trackers[0])).to.equal(null);
    expect(
      trackerLastCheckedAt({
        ...trackers[0],
        last_updated: '2024-06-01T00:00:00Z',
        created: '2024-01-01T00:00:00Z',
      })
    ).to.equal(null);
    expect(
      trackerLastCheckedAt({
        ...trackers[0],
        last_validation: '2024-03-15T12:00:00Z',
        last_updated: '2024-06-01T00:00:00Z',
      })
    ).to.equal('2024-03-15T12:00:00Z');
  });
});

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
    localStorage.removeItem('preloop.trackers.view_mode');
    localStorage.setItem('accessToken', 'test-access-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.removeItem('preloop.trackers.view_mode');
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
      () => el.shadowRoot?.querySelector('.tracker-row') !== null,
      'Tracker list did not render'
    );

    const rows = el.shadowRoot?.querySelectorAll('.tracker-row');
    expect(rows).to.have.lengthOf(2);
    expect(el.shadowRoot?.querySelector('list-toolbar')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain('Last checked');
    expect(el.shadowRoot?.textContent).to.contain('All');
    expect(el.shadowRoot?.textContent).to.not.contain('None');
    expect(el.shadowRoot?.textContent).to.not.contain('Never');
    const kindSelect = el.shadowRoot?.querySelector('sl-select.kind-filter');
    expect(kindSelect?.getAttribute('label')).to.equal('Kind');
  });

  it('keeps the toolbar while refetching existing trackers', async () => {
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
      () => el.shadowRoot?.querySelector('.tracker-row') !== null,
      'Tracker list did not render'
    );

    let resolveFetch!: (value: Response) => void;
    fetchStub.resetBehavior();
    fetchStub.returns(
      new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      })
    );

    const refetch = el.fetchTrackers();
    await el.updateComplete;

    expect(el.shadowRoot?.querySelector('list-toolbar')).to.exist;
    expect(el.shadowRoot?.querySelector('sl-spinner')).to.exist;

    resolveFetch(
      new Response(JSON.stringify(mockTrackers), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    await refetch;
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('.tracker-row')).to.exist;
  });

  it('switches to the existing cards template', async () => {
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
      () => el.shadowRoot?.querySelector('.tracker-row') !== null,
      'Tracker list did not render'
    );

    const toolbar = el.shadowRoot?.querySelector('list-toolbar');
    const cards = toolbar?.shadowRoot?.querySelector(
      'sl-button[data-view="cards"]'
    ) as HTMLElement;
    cards.click();
    await el.updateComplete;

    expect(el.shadowRoot?.querySelector('.tracker-grid')).to.exist;
    expect(el.shadowRoot?.querySelectorAll('tracker-item')).to.have.lengthOf(2);
    expect(el.shadowRoot?.querySelector('.tracker-row')).to.equal(null);
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
