import { expect } from '@open-wc/testing';
import {
  canPublishAgain,
  platformReviewPrerequisites,
  prerequisitesMet,
  publicationSummary,
  reasonText,
  verdictEnum,
  type PlatformReviewRecord,
} from './platform-review';

const SCHEMA = {
  type: 'object',
  required: ['verdict'],
  properties: { verdict: { type: 'string', enum: ['pass', 'fail'] } },
};

const APP = { id: 't1', tracker_type: 'github', auth_type: 'github_app' };

const flow = (extra: Record<string, unknown> = {}) => ({
  git_clone_config: {
    publication_mode: 'read_only',
    repositories: [{ tracker_id: 't1', identifier: 'acme/api' }],
  },
  result_schema: SCHEMA,
  validate_result: true,
  ...extra,
});

const record = (
  extra: Partial<PlatformReviewRecord> = {}
): PlatformReviewRecord => ({
  status: 'posted',
  reason: null,
  head_sha: 'a'.repeat(40),
  review: 'posted',
  review_id: '11',
  review_action: 'comment',
  review_reason: null,
  commit_status: 'failure',
  status_context: 'preloop/review',
  status_reason: null,
  posted_at: null,
  attempts: 1,
  retry_scheduled: false,
  ...extra,
});

describe('platform review helpers', () => {
  it('reads the verdict enum only when it is required and closed', () => {
    expect(verdictEnum(SCHEMA)).to.deep.equal(['pass', 'fail']);
    expect(verdictEnum({ ...SCHEMA, required: [] })).to.equal(null);
    expect(
      verdictEnum({ ...SCHEMA, properties: { verdict: { type: 'string' } } })
    ).to.equal(null);
    expect(verdictEnum(null)).to.equal(null);
    // Same value rule as the server: a value it would refuse is no enum.
    expect(
      verdictEnum({
        ...SCHEMA,
        properties: { verdict: { type: 'string', enum: ['pass', 'pass!'] } },
      })
    ).to.equal(null);
    expect(verdictEnum([])).to.equal(null);
  });

  it('passes every check it can make for a ready flow', () => {
    const checks = platformReviewPrerequisites(flow(), [APP]);
    expect(prerequisitesMet(checks)).to.equal(true);
    // The App permissions cannot be read from Preloop: stated, not guessed.
    const permissions = checks.find((c) => c.id === 'app_permissions')!;
    expect(permissions.ok).to.equal(null);
    expect(permissions.reason).to.contain('Pull requests: Read and write');
    expect(permissions.reason).to.contain('Commit statuses: Read and write');
  });

  it('names the exact reason for each missing prerequisite', () => {
    const off = platformReviewPrerequisites(flow({ validate_result: false }), [
      APP,
    ]);
    expect(off.find((c) => c.id === 'validate_result')!.reason).to.contain(
      'Validate result is off'
    );
    const noSchema = platformReviewPrerequisites(
      flow({ result_schema: null }),
      [APP]
    );
    expect(noSchema.find((c) => c.id === 'verdict_enum')!.ok).to.equal(false);
    const pat = platformReviewPrerequisites(flow(), [
      { ...APP, auth_type: 'api_token' },
    ]);
    const app = pat.find((c) => c.id === 'github_app')!;
    expect(app.ok).to.equal(false);
    expect(app.reason).to.contain('acme/api uses a personal access token');
    const gitlab = platformReviewPrerequisites(flow(), [
      { ...APP, tracker_type: 'gitlab' },
    ]);
    expect(gitlab.find((c) => c.id === 'github_app')!.reason).to.contain(
      'GitHub only'
    );
    expect(prerequisitesMet(pat)).to.equal(false);
  });

  it('says it could not check connections when trackers failed to load', () => {
    const checks = platformReviewPrerequisites(flow(), null);
    expect(checks.find((c) => c.id === 'github_app')!.ok).to.equal(null);
  });

  it('summarises posted, head moved and a missing status permission', () => {
    expect(publicationSummary(record()).label).to.equal('Posted');
    const moved = publicationSummary(
      record({ status: 'skipped', reason: 'head_moved' })
    );
    expect(moved.label).to.equal('Skipped: head moved');
    expect(moved.detail).to.contain('newer commit');
    const failed = publicationSummary(
      record({ status: 'failed', reason: 'statuses_permission_missing' })
    );
    expect(failed.variant).to.equal('danger');
    expect(failed.detail).to.contain('Commit statuses: Read and write');
    expect(reasonText('some_new_code')).to.equal('some new code');
  });

  it('offers to publish again unless posted or in progress', () => {
    expect(canPublishAgain(record())).to.equal(false);
    expect(canPublishAgain(record({ status: 'publishing' }))).to.equal(false);
    expect(canPublishAgain(record({ status: 'failed' }))).to.equal(true);
    expect(canPublishAgain(record({ status: 'pending' }))).to.equal(true);
  });
});
