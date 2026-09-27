import { expect } from '@open-wc/testing';

import {
  getTrackerEventOptions,
  GITHUB_TRACKER_EVENTS,
  GITLAB_TRACKER_EVENTS,
  JIRA_TRACKER_EVENTS,
} from './tracker-event-types';

describe('getTrackerEventOptions', () => {
  it('returns full GitLab event list including merge request updated', () => {
    const events = getTrackerEventOptions('gitlab');
    expect(events).to.deep.equal(GITLAB_TRACKER_EVENTS);
    expect(events.some((event) => event.value === 'merge_request_updated')).to
      .be.true;
    expect(events.some((event) => event.value === 'issue_updated')).to.be.true;
    expect(events.some((event) => event.value === 'deployment')).to.be.true;
  });

  it('offers the GitHub CI events a flow can retrigger on', () => {
    const events = getTrackerEventOptions('github');
    expect(events).to.deep.equal(GITHUB_TRACKER_EVENTS);
    expect(events.some((event) => event.value === 'check_run')).to.be.true;
    expect(events.some((event) => event.value === 'check_suite')).to.be.true;
    expect(events.some((event) => event.value === 'workflow_run')).to.be.true;
  });

  it('offers the Jira changelog-derived label and status events', () => {
    const events = getTrackerEventOptions('jira');
    expect(events).to.deep.equal(JIRA_TRACKER_EVENTS);
    const values = events.map((event) => event.value);
    expect(values).to.include('issue_labeled');
    expect(values).to.include('issue_unlabeled');
    expect(values).to.include('issue_status_changed');
    expect(
      events.find((event) => event.value === 'issue_status_changed')?.name
    ).to.equal('Issue Status Changed');
  });
});
