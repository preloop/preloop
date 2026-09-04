export interface TrackerEventOption {
  name: string;
  value: string;
}

export const GITHUB_TRACKER_EVENTS: TrackerEventOption[] = [
  { name: 'Issue Opened', value: 'issue_opened' },
  { name: 'Issue Updated', value: 'issue_updated' },
  { name: 'Issue Closed', value: 'issue_closed' },
  { name: 'Issue Reopened', value: 'issue_reopened' },
  { name: 'Issue Labeled', value: 'issue_labeled' },
  { name: 'Issue Unlabeled', value: 'issue_unlabeled' },
  { name: 'Pull Request Opened', value: 'pull_request_opened' },
  { name: 'Pull Request Updated', value: 'pull_request_updated' },
  { name: 'Pull Request Closed', value: 'pull_request_closed' },
  { name: 'Pull Request Merged', value: 'pull_request_merged' },
  { name: 'Pull Request Reopened', value: 'pull_request_reopened' },
  { name: 'Comment Created', value: 'comment_created' },
  { name: 'Comment Updated', value: 'comment_updated' },
  { name: 'Push to Repository', value: 'push' },
  { name: 'Release Published', value: 'release' },
  { name: 'Deployment', value: 'deployment' },
  { name: 'Check Run', value: 'check_run' },
  { name: 'Check Suite', value: 'check_suite' },
  { name: 'Workflow Run', value: 'workflow_run' },
];

export const GITLAB_TRACKER_EVENTS: TrackerEventOption[] = [
  { name: 'Issue Opened', value: 'issue_opened' },
  { name: 'Issue Updated', value: 'issue_updated' },
  { name: 'Issue Closed', value: 'issue_closed' },
  { name: 'Issue Reopened', value: 'issue_reopened' },
  { name: 'Issue Labeled', value: 'issue_labeled' },
  { name: 'Issue Unlabeled', value: 'issue_unlabeled' },
  { name: 'Merge Request Opened', value: 'merge_request_opened' },
  { name: 'Merge Request Updated', value: 'merge_request_updated' },
  { name: 'Merge Request Closed', value: 'merge_request_closed' },
  { name: 'Merge Request Merged', value: 'merge_request_merged' },
  { name: 'Merge Request Approved', value: 'merge_request_approved' },
  { name: 'Merge Request Reopened', value: 'merge_request_reopened' },
  { name: 'Comment Created', value: 'comment_created' },
  { name: 'Comment Updated', value: 'comment_updated' },
  { name: 'Push to Repository', value: 'push' },
  { name: 'Tag Push', value: 'tag_push' },
  { name: 'Pipeline Event', value: 'pipeline' },
  { name: 'Job Event', value: 'job' },
  { name: 'Deployment', value: 'deployment' },
  { name: 'Release Published', value: 'release' },
];

export const JIRA_TRACKER_EVENTS: TrackerEventOption[] = [
  { name: 'Issue Opened', value: 'issue_opened' },
  { name: 'Issue Updated', value: 'issue_updated' },
  { name: 'Issue Deleted', value: 'issue_deleted' },
  { name: 'Comment Created', value: 'comment_created' },
  { name: 'Comment Updated', value: 'comment_updated' },
  { name: 'Comment Deleted', value: 'comment_deleted' },
];

export function getTrackerEventOptions(
  trackerType: string | undefined
): TrackerEventOption[] {
  switch (trackerType?.toLowerCase()) {
    case 'github':
      return GITHUB_TRACKER_EVENTS;
    case 'gitlab':
      return GITLAB_TRACKER_EVENTS;
    case 'jira':
      return JIRA_TRACKER_EVENTS;
    default:
      return [];
  }
}
