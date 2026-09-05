import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import '../../components/view-header.ts';
import '../../components/single-issue-detail-view.ts';
import './tracker-issue-view';
import type { TrackerIssueView } from './tracker-issue-view';
import type { Issue, IssueListItem } from '../../types';

const tick = (ms = 200) => new Promise((r) => setTimeout(r, ms));

const trackerId = '11111111-1111-1111-1111-111111111111';
const issueId = '22222222-2222-2222-2222-222222222222';

describe('TrackerIssueView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    localStorage.clear();
  });

  it('renders key, title, status and Open in tracker link', async () => {
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        const json = (data: unknown) =>
          new Response(JSON.stringify(data), { status: 200 });
        if (url.includes(`/api/v1/issues/${issueId}`)) {
          return json({
            id: issueId,
            key: 'ALP-9',
            title: 'Broken search',
            status: 'open',
            description: 'Search returns 500',
            priority: 'High',
            assignee: 'Jane Doe',
            labels: ['bug', 'search'],
            project: 'Alpha',
            project_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            organization: 'Org',
            url: 'https://github.com/example/repo/issues/9',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-04T00:00:00Z',
          });
        }
        if (url.includes(`/api/v1/trackers/${trackerId}`)) {
          return json({
            id: trackerId,
            name: 'Example tracker',
            tracker_type: 'github',
          });
        }
        return json({});
      });

    const el = (await fixture(
      html`<tracker-issue-view></tracker-issue-view>`
    )) as TrackerIssueView;
    (
      el as unknown as {
        location: { params: { trackerId: string; issueId: string } };
      }
    ).location = { params: { trackerId, issueId } };
    (el as unknown as { _trackerId: string })._trackerId = trackerId;
    (el as unknown as { _issueId: string })._issueId = issueId;
    await (el as unknown as { _load: () => Promise<void> })._load();
    await el.updateComplete;
    await tick(50);

    const header = el.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('ALP-9');
    expect(header?.getAttribute('description')).to.equal('Broken search');
    expect(el.shadowRoot?.textContent).to.contain('open');
    const link = el.shadowRoot?.querySelector(
      'a[href="https://github.com/example/repo/issues/9"]'
    );
    expect(link).to.exist;
    expect(link?.textContent).to.contain('Open in GitHub');
    expect(el.shadowRoot?.textContent).to.not.contain('Run implementer');

    const mapped = (
      el as unknown as { _toIssue: (item: IssueListItem) => Issue }
    )._toIssue({
      id: issueId,
      external_id: '9',
      key: 'ALP-9',
      title: 'Broken search',
      description: 'Search returns 500',
      status: 'open',
      priority: 'High',
      assignee: 'Jane Doe',
      labels: ['bug', 'search'],
      organization: 'Org',
      project: 'Alpha',
      project_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
      url: 'https://github.com/example/repo/issues/9',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-04T00:00:00Z',
    });
    expect(mapped.priority).to.equal('High');
    expect(mapped.assignee).to.equal('Jane Doe');
    expect(mapped.labels).to.deep.equal(['bug', 'search']);

    const detail = el.shadowRoot?.querySelector('single-issue-detail-view');
    const detailText = detail?.shadowRoot?.textContent || '';
    expect(detailText).to.contain('Priority High');
    expect(detailText).to.contain('Assignee Jane Doe');
    expect(detailText).to.contain('bug');
    expect(detailText).to.contain('search');
  });

  it('falls back to Open in tracker when the tracker fetch fails', async () => {
    fetchStub = sinon
      .stub(window, 'fetch')
      .callsFake(async (input: RequestInfo | URL) => {
        const url = String(input);
        const json = (data: unknown, status = 200) =>
          new Response(JSON.stringify(data), { status });
        if (url.includes(`/api/v1/issues/${issueId}`)) {
          return json({
            id: issueId,
            key: 'ALP-9',
            title: 'Broken search',
            status: 'open',
            description: 'Search returns 500',
            project: 'Alpha',
            project_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
            organization: 'Org',
            url: 'https://github.com/example/repo/issues/9',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-04T00:00:00Z',
          });
        }
        if (url.includes(`/api/v1/trackers/${trackerId}`)) {
          return json({ detail: 'missing' }, 404);
        }
        return json({});
      });

    const el = (await fixture(
      html`<tracker-issue-view></tracker-issue-view>`
    )) as TrackerIssueView;
    await tick(50);
    (
      el as unknown as {
        location: { params: { trackerId: string; issueId: string } };
      }
    ).location = { params: { trackerId, issueId } };
    (el as unknown as { _trackerId: string })._trackerId = trackerId;
    (el as unknown as { _issueId: string })._issueId = issueId;
    await (el as unknown as { _load: () => Promise<void> })._load();
    await el.updateComplete;
    await tick(50);

    const link = el.shadowRoot?.querySelector(
      'a[href="https://github.com/example/repo/issues/9"]'
    );
    expect(link?.textContent).to.contain('Open in tracker');
    expect(link?.textContent).to.not.contain('Open in GitHub');
  });
});
