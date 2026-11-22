import { expect } from '@open-wc/testing';

import {
  AZURE_DEVOPS_TRACKER_EVENTS,
  BITBUCKET_TRACKER_EVENTS,
  getTrackerEventOptions,
  GITHUB_TRACKER_EVENTS,
  GITLAB_TRACKER_EVENTS,
  JIRA_TRACKER_EVENTS,
  trackerHasIssuePriority,
  trackerHasMilestones,
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
    expect(
      events.find((event) => event.value === 'pull_request_ready_for_review')
        ?.name
    ).to.equal('Pull Request Ready for Review');
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

  it('offers the Bitbucket pull request review events', () => {
    const events = getTrackerEventOptions('bitbucket');
    expect(events).to.deep.equal(BITBUCKET_TRACKER_EVENTS);
    const values = events.map((event) => event.value);
    expect(values).to.include.members([
      'pull_request_opened',
      'pull_request_updated',
      'pull_request_merged',
      'pull_request_closed',
      'pull_request_approved',
      'pull_request_unapproved',
      'pull_request_changes_requested',
      'pull_request_changes_request_removed',
      'comment_created',
      'comment_updated',
      'comment_deleted',
      'push',
    ]);
    expect(values.some((value) => value.startsWith('issue_'))).to.be.false;
  });

  it('offers only the Azure DevOps pull request opened and updated events', () => {
    const events = getTrackerEventOptions('azure_devops');
    expect(events).to.deep.equal(AZURE_DEVOPS_TRACKER_EVENTS);
    expect(events.map((event) => event.value)).to.deep.equal([
      'pull_request_opened',
      'pull_request_updated',
      'issue_opened',
      'issue_updated',
      'issue_labeled',
      'issue_unlabeled',
      'issue_status_changed',
      'comment_created',
    ]);
  });
});

describe('issue filter capabilities', () => {
  it('offers priority for Jira and Azure Boards, milestones elsewhere', () => {
    expect(trackerHasIssuePriority('jira')).to.equal(true);
    expect(trackerHasIssuePriority('azure_devops')).to.equal(true);
    expect(trackerHasIssuePriority('github')).to.equal(false);
    expect(trackerHasMilestones('azure_devops')).to.equal(false);
    expect(trackerHasMilestones('gitlab')).to.equal(true);
    expect(trackerHasMilestones(undefined)).to.equal(true);
  });
});
