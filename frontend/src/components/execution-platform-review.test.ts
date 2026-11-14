import { expect, fixture, fixtureCleanup, html } from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './execution-platform-review';
import type { ExecutionPlatformReview } from './execution-platform-review';
import type { PlatformReviewRecord } from '../utils/platform-review';

const SHA = 'a'.repeat(40);

const record = (
  extra: Partial<PlatformReviewRecord> = {}
): PlatformReviewRecord => ({
  status: 'posted',
  reason: null,
  head_sha: SHA,
  review: 'posted',
  review_id: '11',
  review_action: 'comment',
  review_reason: null,
  commit_status: 'failure',
  status_context: 'preloop/review',
  status_reason: null,
  posted_at: '2026-11-13T10:00:00Z',
  attempts: 1,
  retry_scheduled: false,
  pull_request_url: 'https://github.com/acme/api/pull/7',
  review_url: 'https://github.com/acme/api/pull/7#pullrequestreview-11',
  commit_url: `https://github.com/acme/api/commit/${SHA}`,
  ...extra,
});

describe('execution-platform-review', () => {
  let sandbox: SinonSandbox;
  let respond: () => Response;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    respond = () =>
      new Response(
        JSON.stringify({ execution_id: 'e1', platform_review: record() })
      );
    sandbox.stub(window, 'fetch').callsFake(async () => respond());
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
  });

  const mount = (
    value: PlatformReviewRecord | null,
    canPublish = true,
    finished = true
  ) =>
    fixture<ExecutionPlatformReview>(
      html`<execution-platform-review
        .executionId=${'e1'}
        .record=${value}
        .canPublish=${canPublish}
        .finished=${finished}
      ></execution-platform-review>`
    );

  const q = (element: Element, selector: string): any =>
    element.shadowRoot!.querySelector(selector);

  it('renders nothing without a record', async () => {
    const element = await mount(null);
    expect(q(element, '[data-platform-review]')).to.equal(null);
  });

  it('links the posted review, the status and the pull request', async () => {
    const element = await mount(record());
    expect(q(element, '[data-state]').textContent.trim()).to.equal('Posted');
    expect(q(element, '[data-link="review"]').getAttribute('href')).to.equal(
      record().review_url
    );
    expect(q(element, '[data-link="status"]').textContent).to.contain(
      'preloop/review: failure'
    );
    expect(
      q(element, '[data-link="pull_request"]').getAttribute('rel')
    ).to.contain('noopener');
    // Nothing to publish once everything is posted.
    expect(q(element, '[data-action="publish"]')).to.equal(null);
  });

  it('explains a review skipped because the head moved', async () => {
    const element = await mount(
      record({
        status: 'skipped',
        reason: 'head_moved',
        review: 'skipped',
        review_reason: 'head_moved',
        review_url: null,
      })
    );
    expect(q(element, '[data-state]').textContent).to.contain('head moved');
    expect(q(element, '[data-reason]').textContent).to.contain('newer commit');
    expect(q(element, '[data-link="review"]')).to.equal(null);
  });

  it('states a missing status permission and publishes again', async () => {
    const failed = record({
      status: 'failed',
      reason: 'statuses_permission_missing',
      commit_status: null,
      status_reason: 'statuses_permission_missing',
      commit_url: null,
    });
    const element = await mount(failed);
    expect(q(element, '[data-reason]').textContent).to.contain(
      'Commit statuses: Read and write'
    );
    let updated: any = null;
    element.addEventListener('platform-review-updated', (e: Event) => {
      updated = (e as CustomEvent).detail.record;
    });
    q(element, '[data-action="publish"]').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await element.updateComplete;
    const call = (window.fetch as sinon.SinonStub).lastCall;
    expect(String(call.args[0])).to.contain(
      '/flows/executions/e1/platform-review'
    );
    expect(call.args[1].method).to.equal('POST');
    expect(updated.status).to.equal('posted');
    expect(q(element, '[data-state]').textContent.trim()).to.equal('Posted');
  });

  it('shows the refusal reason when publishing is refused', async () => {
    respond = () =>
      new Response(
        JSON.stringify({
          detail: { code: 'platform_review_disabled', message: 'x' },
        }),
        { status: 409 }
      );
    const element = await mount(record({ status: 'failed' }));
    q(element, '[data-action="publish"]').click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    await element.updateComplete;
    expect(q(element, '[data-error]').textContent).to.contain(
      'Posting was turned off on the flow'
    );
  });

  it('hides Publish again from viewers who may not edit flows', async () => {
    const element = await mount(record({ status: 'failed' }), false);
    expect(q(element, '[data-action="publish"]')).to.equal(null);
  });

  it('hides Publish again while the run is still going', async () => {
    const element = await mount(record({ status: 'pending' }), true, false);
    expect(q(element, '[data-state]').textContent.trim()).to.equal(
      'Not posted yet'
    );
    expect(q(element, '[data-action="publish"]')).to.equal(null);
  });
});
