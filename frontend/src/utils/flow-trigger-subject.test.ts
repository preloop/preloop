import { expect } from '@open-wc/testing';

import {
  GITHUB_TRACKER_EVENTS,
  GITLAB_TRACKER_EVENTS,
  JIRA_TRACKER_EVENTS,
} from '../constants/tracker-event-types';
import {
  isIssueSubjectEventType,
  triggerIsAboutIssue,
} from './flow-trigger-subject';

describe('flow-trigger-subject', () => {
  it('classifies every event the tracker pickers offer', () => {
    const issueSubject = [
      'issue_opened',
      'issue_updated',
      'issue_closed',
      'issue_reopened',
      'issue_labeled',
      'issue_unlabeled',
      'issue_deleted',
      'comment_created',
      'comment_updated',
      'comment_deleted',
    ];
    const allEvents = [
      ...GITHUB_TRACKER_EVENTS,
      ...GITLAB_TRACKER_EVENTS,
      ...JIRA_TRACKER_EVENTS,
    ].map((option) => option.value);
    for (const value of allEvents) {
      expect(isIssueSubjectEventType(value), value).to.equal(
        issueSubject.includes(value)
      );
    }
    // Every issue-subject name above is offered by at least one tracker.
    for (const value of issueSubject) {
      expect(allEvents, value).to.include(value);
    }
  });

  it('keeps code and review events out', () => {
    for (const value of [
      'pull_request_opened',
      'pull_request_merged',
      'merge_request_opened',
      'merge_request_approved',
      'push',
      'tag_push',
      'release',
      'deployment',
      'check_run',
      'check_suite',
      'workflow_run',
      'pipeline',
      'job',
      'webhook',
      'schedule',
    ]) {
      expect(isIssueSubjectEventType(value), value).to.equal(false);
    }
  });

  it('matches provider spellings and ignores junk', () => {
    expect(isIssueSubjectEventType('issues.opened')).to.equal(true);
    expect(isIssueSubjectEventType('  ISSUE_OPENED ')).to.equal(true);
    expect(isIssueSubjectEventType('')).to.equal(false);
    expect(isIssueSubjectEventType('   ')).to.equal(false);
    expect(isIssueSubjectEventType(undefined)).to.equal(false);
    expect(isIssueSubjectEventType(42)).to.equal(false);
  });

  it('is false for every non tracker trigger, whatever it stored', () => {
    expect(triggerIsAboutIssue('webhook', ['webhook'])).to.equal(false);
    expect(triggerIsAboutIssue('schedule', ['schedule'])).to.equal(false);
    // A trigger switched away from a tracker can still carry stale event types.
    expect(triggerIsAboutIssue('webhook', ['issue_opened'])).to.equal(false);
    expect(triggerIsAboutIssue('schedule', ['issue_opened'])).to.equal(false);
    expect(triggerIsAboutIssue(undefined, ['issue_opened'])).to.equal(false);
  });

  it('is true for a tracker trigger with at least one issue event', () => {
    expect(triggerIsAboutIssue('tracker', ['issue_opened'])).to.equal(true);
    expect(
      triggerIsAboutIssue('tracker', ['pull_request_opened', 'issue_labeled'])
    ).to.equal(true);
    expect(triggerIsAboutIssue('tracker', ['comment_created'])).to.equal(true);
  });

  it('is false for a tracker trigger with no issue event', () => {
    expect(triggerIsAboutIssue('tracker', ['pull_request_opened'])).to.equal(
      false
    );
    expect(
      triggerIsAboutIssue('tracker', ['merge_request_merged', 'push'])
    ).to.equal(false);
    expect(triggerIsAboutIssue('tracker', [])).to.equal(false);
    expect(triggerIsAboutIssue('tracker', undefined)).to.equal(false);
    expect(triggerIsAboutIssue('tracker', 'issue_opened')).to.equal(false);
  });
});
