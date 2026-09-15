import { expect } from '@open-wc/testing';

import {
  markerFailureTimestamp,
  markerSinceLabel,
  modelAttentionState,
} from './model-attention';
import { deriveAttentionItems, modelAttentionItemId } from './attention';
import type { AttentionDismissal } from '../api';

/**
 * The rule these tests pin is agreement: a summary row and the inbox must name
 * the same item with the same fingerprint, or a dismissal made on one page is
 * invisible on the other, which is the bug this module exists to prevent.
 */
describe('modelAttentionState', () => {
  const NOW = new Date('2026-09-15T12:00:00Z');
  const FAILED_AT = '2026-09-14T09:00:00Z';

  const dismissal = (
    overrides: Partial<AttentionDismissal> = {}
  ): AttentionDismissal => ({
    id: 'dismissal-1',
    item_id: 'model:example/reviewer',
    fingerprint: `last:${FAILED_AT}`,
    reason: 'fixed',
    snooze_until: null,
    dismissed_by_user_id: 'user-1',
    dismissed_by_username: 'Jane Doe',
    created_at: '2026-09-14T09:30:00Z',
    ...overrides,
  });

  const summary = (overrides: Record<string, unknown> = {}) => ({
    failureAlias: 'example/reviewer',
    providerName: 'example-provider',
    failedRequests: 9,
    lastFailureAt: FAILED_AT,
    ...overrides,
  });

  it('names the item the way the inbox names it', () => {
    const state = modelAttentionState(summary(), [], NOW);

    expect(state.itemId).to.equal(
      modelAttentionItemId('example/reviewer', null)
    );
    expect(state.fingerprint).to.equal(`last:${FAILED_AT}`);
    expect(state.status).to.equal('failing');
    expect(state.dismissable).to.equal(true);
  });

  it('falls back to the provider when no alias was recorded', () => {
    const state = modelAttentionState(summary({ failureAlias: null }), [], NOW);

    expect(state.itemId).to.equal('model:example-provider');
  });

  it('holds a model quiet while a matching marker is in force', () => {
    const state = modelAttentionState(summary(), [dismissal()], NOW);

    expect(state.status).to.equal('marked');
    expect(state.marked).to.equal(true);
    // Nothing to dismiss twice, and nothing has happened "since" yet.
    expect(state.dismissable).to.equal(false);
    expect(state.failuresSinceMarker).to.equal(null);
    expect(state.markerLabel).to.contain('Marked fixed');
  });

  it('flags the model again once a newer failure arrives', () => {
    const state = modelAttentionState(
      summary({
        lastFailureAt: '2026-09-15T08:00:00Z',
        failedRequestsSince: 2,
      }),
      [dismissal()],
      NOW
    );

    expect(state.status).to.equal('failing');
    expect(state.markerFailureAt).to.equal(FAILED_AT);
    expect(state.failuresSinceMarker).to.equal(2);
  });

  it('lets an expired snooze bring the model back', () => {
    const expired = dismissal({
      reason: 'snoozed',
      snooze_until: '2026-09-15T06:00:00Z',
    });
    const live = dismissal({
      reason: 'snoozed',
      snooze_until: '2026-09-16T06:00:00Z',
    });

    expect(modelAttentionState(summary(), [expired], NOW).status).to.equal(
      'failing'
    );
    expect(modelAttentionState(summary(), [live], NOW).status).to.equal(
      'marked'
    );
    expect(modelAttentionState(summary(), [live], NOW).markerLabel).to.contain(
      'Snoozed'
    );
  });

  it('says nothing about a model with no failures', () => {
    const state = modelAttentionState(
      summary({ failedRequests: 0, lastFailureAt: null }),
      [],
      NOW
    );

    expect(state.status).to.equal('quiet');
    expect(state.dismissable).to.equal(false);
  });

  it('reads the acknowledged failure back out of a fingerprint', () => {
    expect(markerFailureTimestamp(dismissal())).to.equal(FAILED_AT);
    // A fingerprint of another shape belongs to another kind of item.
    expect(
      markerFailureTimestamp(dismissal({ fingerprint: 'run:execution-1' }))
    ).to.equal(null);
    expect(markerFailureTimestamp(null)).to.equal(null);
  });

  it('names the marker the count is measured from', () => {
    expect(markerSinceLabel('fixed')).to.equal('since fix');
    expect(markerSinceLabel('snoozed')).to.equal('since snooze');
    expect(markerSinceLabel('expected')).to.equal('since marked expected');
    expect(markerSinceLabel(undefined)).to.equal('since fix');
  });

  it('keeps a two-alias row flagged until every alias item is dismissed', () => {
    const newestAt = FAILED_AT;
    const olderAt = '2026-09-13T08:00:00Z';
    const twoAliases = summary({
      aliasFailures: [
        {
          failureAlias: 'example/reviewer',
          lastFailureAt: newestAt,
          failedRequests: 2,
        },
        {
          failureAlias: 'example/reviewer-old',
          lastFailureAt: olderAt,
          failedRequests: 3,
        },
      ],
    });
    const newestDismissal = dismissal();
    const olderDismissal = dismissal({
      id: 'dismissal-2',
      item_id: 'model:example/reviewer-old',
      fingerprint: `last:${olderAt}`,
    });
    const failures = [
      {
        api_usage_id: 'u-new',
        timestamp: newestAt,
        status_code: 500,
        outcome: 'error',
        model_alias: 'example/reviewer',
        provider_name: 'example-provider',
      },
      {
        api_usage_id: 'u-old',
        timestamp: olderAt,
        status_code: 502,
        outcome: 'error',
        model_alias: 'example/reviewer-old',
        provider_name: 'example-provider',
      },
    ];

    const newestOnly = modelAttentionState(twoAliases, [newestDismissal], NOW);
    expect(newestOnly.status).to.equal('failing');
    expect(newestOnly.itemId).to.equal('model:example/reviewer-old');
    const inboxNewestOnly = deriveAttentionItems({
      now: NOW,
      gatewayFailures: failures as any,
      dismissals: [newestDismissal],
    });
    expect(inboxNewestOnly.items.map((item) => item.id)).to.include(
      'model:example/reviewer-old'
    );
    expect(inboxNewestOnly.items.map((item) => item.id)).to.not.include(
      'model:example/reviewer'
    );

    const both = modelAttentionState(
      twoAliases,
      [newestDismissal, olderDismissal],
      NOW
    );
    expect(both.status).to.equal('marked');
    expect(both.dismissable).to.equal(false);
    const inboxBoth = deriveAttentionItems({
      now: NOW,
      gatewayFailures: failures as any,
      dismissals: [newestDismissal, olderDismissal],
    });
    expect(
      inboxBoth.items.filter((item) => item.kind === 'model')
    ).to.have.length(0);
  });
});
