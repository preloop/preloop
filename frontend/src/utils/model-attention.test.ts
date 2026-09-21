import { expect } from '@open-wc/testing';

import {
  markerFailureTimestamp,
  markerSinceLabel,
  modelAttentionState,
  unpricedAttentionState,
} from './model-attention';
import {
  deriveAttentionItems,
  modelAttentionItemId,
  unpricedModelAttentionItemId,
} from './attention';
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
  it('flags a model as failing when credentials refresh fails even with zero failed requests', () => {
    const state = modelAttentionState(
      summary({
        failedRequests: 0,
        lastFailureAt: null,
        credentialsStatus: 'error',
        credentialsLastError:
          'openai refresh failed (status=401, code=invalid_grant)',
        credentialsLastFailedAt: '2026-09-15T10:00:00Z',
        credentialType: 'oauth_openai_codex',
      }),
      [],
      NOW
    );

    expect(state.status).to.equal('failing');
    expect(state.dismissable).to.equal(true);
    expect(state.reasonText).to.equal(
      'openai refresh failed (status=401, code=invalid_grant) (last 2h ago)'
    );
    expect(state.reasonText).to.not.contain('2026-09-15T10:00:00Z');
    expect(state.credentialsLastFailedAt).to.equal('2026-09-15T10:00:00Z');
    expect(state.remediationText).to.contain('Codex CLI');
  });

  it('provides tailored remediation text based on credential type', () => {
    const claudeState = modelAttentionState(
      summary({
        credentialsStatus: 'error',
        credentialType: 'oauth_anthropic_claude_code',
      }),
      [],
      NOW
    );
    expect(claudeState.remediationText).to.contain('Claude Code');

    const genericState = modelAttentionState(
      summary({
        credentialsStatus: 'error',
        credentialType: 'api_key',
      }),
      [],
      NOW
    );
    expect(genericState.remediationText).to.contain('Rotate the API key');
  });

  it('does not flag active credentials as failing when request count is zero', () => {
    const state = modelAttentionState(
      summary({
        failedRequests: 0,
        lastFailureAt: null,
        credentialsStatus: 'active',
        credentialsLastVerifiedAt: '2026-09-15T10:00:00Z',
      }),
      [],
      NOW
    );

    expect(state.status).to.equal('quiet');
    expect(state.reasonText).to.be.null;
    expect(state.remediationText).to.be.null;
  });
});

/**
 * The unpriced half of the same rule (#848). A model that is unpriced on
 * purpose (a local model, a flat rate, a bill settled outside Preloop) is
 * marked once and stays quiet, which is why its fingerprint carries no
 * timestamp: one more unpriced request is not news.
 */
describe('unpricedAttentionState', () => {
  const NOW = new Date('2026-09-21T12:00:00Z');

  const unpricedDismissal = (
    overrides: Partial<AttentionDismissal> = {}
  ): AttentionDismissal => ({
    id: 'dismissal-2',
    item_id: 'model-unpriced:local/qwen-3-coder',
    fingerprint: 'unpriced:local/qwen-3-coder',
    reason: 'expected',
    snooze_until: null,
    dismissed_by_user_id: 'user-1',
    dismissed_by_username: 'Jane Doe',
    created_at: '2026-09-20T09:30:00Z',
    ...overrides,
  });

  const unpricedSummary = (overrides: Record<string, unknown> = {}) => ({
    modelAlias: 'local/qwen-3-coder',
    providerName: 'ollama',
    unpricedRequests: 12,
    ...overrides,
  });

  it('names the item and fingerprint the way every surface does', () => {
    const state = unpricedAttentionState(unpricedSummary(), [], NOW);

    expect(state.itemId).to.equal(
      unpricedModelAttentionItemId('local/qwen-3-coder', 'ollama')
    );
    expect(state.itemId).to.equal('model-unpriced:local/qwen-3-coder');
    expect(state.fingerprint).to.equal('unpriced:local/qwen-3-coder');
    expect(state.status).to.equal('unpriced');
    expect(state.dismissable).to.equal(true);
    expect(state.restorable).to.equal(false);
  });

  it('derives the alias the way the failure item does', () => {
    const failureItemId = modelAttentionItemId('local/qwen-3-coder', 'ollama');

    expect(unpricedAttentionState(unpricedSummary(), [], NOW).itemId).to.equal(
      failureItemId.replace('model:', 'model-unpriced:')
    );
  });

  it('falls back to the provider when no alias was recorded', () => {
    const state = unpricedAttentionState(
      unpricedSummary({ modelAlias: null }),
      [],
      NOW
    );

    expect(state.itemId).to.equal('model-unpriced:ollama');
    expect(state.fingerprint).to.equal('unpriced:ollama');
  });

  it('holds a model quiet while an active marker covers it', () => {
    const state = unpricedAttentionState(
      unpricedSummary(),
      [unpricedDismissal()],
      NOW
    );

    expect(state.status).to.equal('marked');
    expect(state.marked).to.equal(true);
    expect(state.dismissable).to.equal(false);
    expect(state.restorable).to.equal(true);
    expect(state.markerLabel).to.contain('marked expected');
  });

  // The point of a stable fingerprint: "unpriced by design" does not stop
  // being true because the model served one more unpriced request.
  it('stays marked however many more unpriced requests arrive', () => {
    const state = unpricedAttentionState(
      unpricedSummary({ unpricedRequests: 4000 }),
      [unpricedDismissal()],
      NOW
    );

    expect(state.status).to.equal('marked');
  });

  it('ignores a marker taken against another model', () => {
    const state = unpricedAttentionState(
      unpricedSummary(),
      [unpricedDismissal({ item_id: 'model-unpriced:openrouter/x/y' })],
      NOW
    );

    expect(state.status).to.equal('unpriced');
    expect(state.dismissal).to.equal(null);
  });

  // A failure marker on the same model is a claim about a different fact.
  it('ignores this model failure marker', () => {
    const state = unpricedAttentionState(
      unpricedSummary(),
      [
        unpricedDismissal({
          item_id: 'model:local/qwen-3-coder',
          fingerprint: 'last:2026-09-20T08:00:00Z',
          reason: 'fixed',
        }),
      ],
      NOW
    );

    expect(state.status).to.equal('unpriced');
  });

  it('flags the model again once a snooze has run out', () => {
    const snoozed = unpricedDismissal({
      reason: 'snoozed',
      snooze_until: '2026-09-21T11:00:00Z',
    });

    expect(
      unpricedAttentionState(unpricedSummary(), [snoozed], NOW).status
    ).to.equal('unpriced');
    expect(
      unpricedAttentionState(
        unpricedSummary(),
        [snoozed],
        new Date('2026-09-21T10:00:00Z')
      ).status
    ).to.equal('marked');
  });

  it('says nothing about a model whose requests are all priced', () => {
    const state = unpricedAttentionState(
      unpricedSummary({ unpricedRequests: 0 }),
      [],
      NOW
    );

    expect(state.status).to.equal('quiet');
    expect(state.dismissable).to.equal(false);
    expect(state.restorable).to.equal(false);
  });
});
