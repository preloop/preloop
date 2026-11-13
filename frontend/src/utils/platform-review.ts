/**
 * Review and status posted by Preloop (`git_clone_config.platform_review`).
 *
 * Pure helpers shared by the flow card that turns the posting on and the
 * execution section that shows what was posted. The rules mirror the
 * server's save-time check (`validate_platform_review_flow` and
 * `validate_platform_review_trackers`), so the console states the reason a
 * save would be refused before the operator presses Save.
 */

export type CommitState = 'success' | 'failure' | 'error';
export type ReviewAction = 'comment' | 'approve' | 'request_changes';

export interface PlatformReviewStatusConfig {
  context?: string;
  verdict_states: Record<string, CommitState>;
  incomplete_state?: 'error' | 'failure' | null;
}

export interface PlatformReviewConfig {
  enabled: boolean;
  action?: 'comment';
  verdict_actions?: Record<string, ReviewAction> | null;
  status?: PlatformReviewStatusConfig | null;
}

/** The record the API returns on an execution (`platform_review`). */
export interface PlatformReviewRecord {
  status: 'pending' | 'publishing' | 'posted' | 'skipped' | 'failed' | string;
  reason: string | null;
  head_sha: string | null;
  review: 'posted' | 'skipped' | null | string;
  review_id: string | null;
  review_action: string | null;
  review_reason: string | null;
  commit_status: CommitState | null | string;
  status_context: string | null;
  status_reason: string | null;
  posted_at: string | null;
  attempts: number;
  retry_scheduled: boolean;
  pull_request_url?: string | null;
  review_url?: string | null;
  commit_url?: string | null;
}

export const DEFAULT_STATUS_CONTEXT = 'preloop/review';
export const STATUS_CONTEXT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9 ._/:-]{0,99}$/;
export const COMMIT_STATES: CommitState[] = ['success', 'failure', 'error'];

/**
 * The verdicts a result schema admits, or null when it declares no required
 * string `verdict` with an enum (the server's `verdict_enum`).
 */
export function verdictEnum(schema: unknown): string[] | null {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) {
    return null;
  }
  const doc = schema as Record<string, any>;
  const required = Array.isArray(doc.required) ? doc.required : [];
  const verdict = doc.properties?.verdict;
  if (!required.includes('verdict') || !verdict || typeof verdict !== 'object')
    return null;
  if (verdict.type !== 'string' || !Array.isArray(verdict.enum)) return null;
  const values = verdict.enum.filter(
    (value: unknown): value is string => typeof value === 'string' && !!value
  );
  if (!values.length || values.length !== verdict.enum.length) return null;
  return values;
}

/** A sensible first state for a verdict, which the operator can change. */
export function suggestedState(verdict: string): CommitState {
  const word = verdict.toLowerCase();
  if (/^(pass|passed|ok|approve|approved|lgtm|success|clean)$/.test(word)) {
    return 'success';
  }
  return 'failure';
}

export interface PrerequisiteCheck {
  id:
    | 'read_only'
    | 'validate_result'
    | 'verdict_enum'
    | 'repositories'
    | 'github_app'
    | 'app_permissions';
  label: string;
  /** True, false, or null when Preloop cannot check it from here. */
  ok: boolean | null;
  /** Why it fails, in words, or what to confirm when it cannot be checked. */
  reason: string;
}

interface TrackerLike {
  id: string;
  name?: string;
  tracker_type?: string;
  auth_type?: string;
}

interface FlowLike {
  git_clone_config?: {
    publication_mode?: string;
    repositories?: { tracker_id?: string; identifier?: string }[];
  } | null;
  result_schema?: unknown;
  validate_result?: boolean | null;
}

/**
 * What the posting needs, as checks the operator can read.
 *
 * `trackers` is the account's tracker list, or null when it could not be
 * loaded (the tracker checks then say so rather than guess).
 */
export function platformReviewPrerequisites(
  flow: FlowLike,
  trackers: TrackerLike[] | null
): PrerequisiteCheck[] {
  const config = flow.git_clone_config || {};
  const schema = flow.result_schema;
  const hasSchema =
    !!schema && typeof schema === 'object' && Object.keys(schema).length > 0;
  const checks: PrerequisiteCheck[] = [];
  checks.push({
    id: 'read_only',
    label: 'The flow is a read-only review',
    ok: config.publication_mode === 'read_only',
    reason:
      config.publication_mode === 'read_only'
        ? ''
        : 'Posting is only available for read-only review flows.',
  });
  checks.push({
    id: 'validate_result',
    label: 'Result validation is on',
    ok: hasSchema && flow.validate_result !== false,
    reason: !hasSchema
      ? 'The flow has no result schema. Add one under Result schema in Edit flow.'
      : flow.validate_result === false
        ? 'Validate result is off. Only a validated result is posted; turn it on in Edit flow.'
        : '',
  });
  const verdicts = hasSchema ? verdictEnum(schema) : null;
  checks.push({
    id: 'verdict_enum',
    label: 'The result schema has a required verdict with fixed values',
    ok: verdicts !== null,
    reason:
      verdicts !== null
        ? ''
        : 'The result schema must list "verdict" under required and declare it as a string with an enum, for example ["pass", "fail"].',
  });
  const repositories = config.repositories || [];
  checks.push({
    id: 'repositories',
    label: 'The flow names its repositories',
    ok: repositories.length > 0,
    reason: repositories.length
      ? ''
      : 'Add at least one repository under Git workspace in Edit flow.',
  });
  if (trackers === null) {
    checks.push({
      id: 'github_app',
      label: 'Every repository is connected through a GitHub App',
      ok: null,
      reason: 'The connections could not be loaded, so this was not checked.',
    });
  } else if (repositories.length) {
    const byId = new Map(
      trackers.map((tracker) => [String(tracker.id), tracker])
    );
    const offending = repositories.find((repository) => {
      const tracker = byId.get(String(repository.tracker_id || ''));
      return (
        !tracker ||
        String(tracker.tracker_type || '').toLowerCase() !== 'github' ||
        String(tracker.auth_type || '').toLowerCase() !== 'github_app'
      );
    });
    const tracker = offending
      ? byId.get(String(offending.tracker_id || ''))
      : undefined;
    checks.push({
      id: 'github_app',
      label: 'Every repository is connected through a GitHub App',
      ok: !offending,
      reason: !offending
        ? ''
        : !tracker
          ? `${offending.identifier || 'A repository'} has no connection Preloop can use.`
          : String(tracker.tracker_type || '').toLowerCase() !== 'github'
            ? `${offending.identifier || 'A repository'} is on ${tracker.tracker_type}. Posting supports GitHub only.`
            : `${offending.identifier || 'A repository'} uses a personal access token. A token cannot be limited to one repository and two permissions; connect it through the GitHub App.`,
    });
  }
  checks.push({
    id: 'app_permissions',
    label: 'The GitHub App may write pull requests and commit statuses',
    ok: null,
    reason:
      'Confirm in GitHub, under the App installation: Pull requests: Read and write, and Commit statuses: Read and write (statuses only when a commit status is set). Preloop sees a missing permission only when it posts.',
  });
  return checks;
}

/** True when every check that can be made passed. */
export function prerequisitesMet(checks: PrerequisiteCheck[]): boolean {
  return checks.every((check) => check.ok !== false);
}

/** Plain-language reasons for the stable codes the server records. */
const REASONS: Record<string, string> = {
  head_moved:
    'The pull request moved to a newer commit after the review, so no review was posted on it. The status is on the reviewed commit.',
  pull_request_closed: 'The pull request was closed or merged before posting.',
  statuses_permission_missing:
    'The GitHub App may not write commit statuses. Grant Commit statuses: Read and write on the App installation, then publish again.',
  not_configured: 'The flow sets no commit status.',
  provider_unavailable:
    'GitHub was unavailable or rate limited. Preloop retries on its own.',
  provider_rejected: 'GitHub refused the request.',
  provider_denied:
    'GitHub denied the request. Check the App installation and its permissions.',
  readback_mismatch:
    'What GitHub holds differs from what was sent. Someone has to look at the pull request; this is not retried.',
  lease_unavailable:
    'Preloop could not get a GitHub App token for the repository.',
  lease_refused:
    'GitHub refused a token for the repository with the permissions posting needs.',
  platform_review_disabled: 'Posting was turned off on the flow.',
  verdict_unmapped: 'The verdict has no commit status state on the flow.',
  run_not_succeeded: 'The run did not succeed, so no verdict was posted.',
  result_not_valid:
    'The result did not pass the result schema, so nothing was posted.',
  admission_revoked:
    "The run's CI identity or its project is no longer admitted to the flow.",
  binding_changed:
    'The pull request no longer matches the one the run was started for.',
  binding_invalid: 'The run has no usable pull request binding.',
  no_review_binding: 'The run was not started by a restricted CI review.',
  tracker_unsupported:
    'The repository is not connected through a GitHub App, so nothing can be posted.',
  configuration_invalid: "The flow's posting settings are not valid.",
  attempt_interrupted:
    'The attempt was interrupted. Preloop retries on its own.',
  execution_not_finished: 'The run has not finished yet.',
  request_not_allowed: 'Preloop refused a request outside what posting may do.',
  internal_error: 'Posting failed inside Preloop.',
};

export function reasonText(code: string | null | undefined): string {
  if (!code) return '';
  return REASONS[code] ?? code.replace(/_/g, ' ');
}

export interface PublicationSummary {
  label: string;
  variant: 'neutral' | 'success' | 'warning' | 'danger';
  detail: string;
}

/** One chip and one sentence for a publication record. */
export function publicationSummary(
  record: PlatformReviewRecord
): PublicationSummary {
  switch (record.status) {
    case 'pending':
      return {
        label: 'Not posted yet',
        variant: 'neutral',
        detail: 'Preloop posts when the run finishes.',
      };
    case 'publishing':
      return {
        label: 'Posting',
        variant: 'neutral',
        detail: 'Preloop is posting to GitHub now.',
      };
    case 'posted':
      return {
        label: 'Posted',
        variant: 'success',
        detail:
          record.review === 'skipped' && record.review_reason
            ? reasonText(record.review_reason)
            : '',
      };
    case 'skipped':
      return {
        label:
          record.reason === 'head_moved' ? 'Skipped: head moved' : 'Skipped',
        variant: 'neutral',
        detail: reasonText(record.reason),
      };
    case 'failed':
      return {
        label: 'Failed',
        variant: 'danger',
        detail: reasonText(record.reason),
      };
    default:
      return {
        label: String(record.status || 'Unknown'),
        variant: 'neutral',
        detail: reasonText(record.reason),
      };
  }
}

/** True when a publish call could change something. */
export function canPublishAgain(record: PlatformReviewRecord): boolean {
  return record.status !== 'publishing' && record.status !== 'posted';
}
