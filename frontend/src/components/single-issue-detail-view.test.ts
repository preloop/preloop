import { html, fixture, expect } from '@open-wc/testing';
import './single-issue-detail-view';
import type { SingleIssueDetailView } from './single-issue-detail-view';
import type { Issue } from '../types';

function makeIssue(overrides: Partial<Issue> = {}): Issue {
  return {
    id: 'issue-1',
    title: 'Broken search',
    description: 'Search returns 500',
    status: 'open',
    status_id: '',
    priority: 'High',
    priority_id: '',
    project_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    project_name: 'Alpha',
    organization_id: 'org-1',
    organization_name: 'Org',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-04T00:00:00Z',
    key: 'ALP-9',
    source: '',
    url: 'https://example.com/9',
    labels: ['bug', 'search'],
    assignee: 'Jane Doe',
    ...overrides,
  };
}

describe('SingleIssueDetailView', () => {
  it('renders labels, priority and assignee', async () => {
    const el = (await fixture(
      html`<single-issue-detail-view
        .issue=${makeIssue()}
      ></single-issue-detail-view>`
    )) as SingleIssueDetailView;
    await el.updateComplete;
    const text = el.shadowRoot?.textContent || '';
    expect(text).to.contain('Priority High');
    expect(text).to.contain('Assignee Jane Doe');
    expect(text).to.contain('bug');
    expect(text).to.contain('search');
    expect(text).to.contain('Search returns 500');
  });

  it('omits the meta row when those fields are empty', async () => {
    const el = (await fixture(
      html`<single-issue-detail-view
        .issue=${makeIssue({
          priority: '',
          assignee: '',
          labels: [],
        })}
      ></single-issue-detail-view>`
    )) as SingleIssueDetailView;
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('.issue-meta')).to.not.exist;
    expect(el.shadowRoot?.textContent).to.not.contain('Priority');
    expect(el.shadowRoot?.textContent).to.not.contain('Assignee');
  });
});
